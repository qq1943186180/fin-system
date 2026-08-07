# -*- coding: utf-8 -*-
"""Accounting-core tests. Every test uses an isolated temporary SQLite file."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db
from modules import arap, expense, ledger


class AccountingCoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._old_db_path = db.DB_PATH
        self._tmp = tempfile.TemporaryDirectory(prefix="finance-core-")
        self.db_path = Path(self._tmp.name) / "finance.db"
        db.configure_db_path(self.db_path)
        db.init_db(self.db_path)

    def tearDown(self) -> None:
        db.configure_db_path(self._old_db_path)
        self._tmp.cleanup()

    def _post(self, voucher_id: int, prefix: str = "role") -> None:
        self.assertTrue(ledger.submit_voucher(voucher_id, f"{prefix}-submitter")[0])
        self.assertTrue(ledger.audit_voucher(voucher_id, f"{prefix}-reviewer")[0])
        self.assertTrue(ledger.post_voucher(voucher_id, f"{prefix}-poster")[0])

    def test_voucher_state_machine_idempotency_immutability_and_period(self) -> None:
        lines = [
            {"account_code": "1002", "debit_cents": 10001, "credit_cents": 0},
            {"account_code": "3001", "debit_cents": 0, "credit_cents": 10001},
        ]
        first = ledger.create_voucher(
            "2026-07-10", "capital", lines, creator="maker", idempotency_key="capital-1"
        )
        second = ledger.create_voucher(
            "2026-07-10", "capital", lines, creator="maker", idempotency_key="capital-1"
        )
        self.assertTrue(first[0])
        self.assertEqual(first[2], second[2])
        self.assertEqual(len(ledger.list_vouchers()), 1)

        voucher_id = int(first[2])
        self.assertTrue(ledger.submit_voucher(voucher_id, "maker")[0])
        self.assertFalse(ledger.audit_voucher(voucher_id, "maker")[0])
        self.assertTrue(ledger.audit_voucher(voucher_id, "reviewer")[0])
        self.assertFalse(ledger.post_voucher(voucher_id, "reviewer")[0])
        self.assertTrue(ledger.post_voucher(voucher_id, "poster")[0])

        conn = db.get_conn()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE voucher_line SET debit_cents=1 WHERE id=1")
            conn.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO voucher_line(voucher_id,seq,account_code,debit_cents,credit_cents) "
                    "VALUES (1,3,'1002',0,0)"
                )
            conn.rollback()
        finally:
            conn.close()

        reversal = ledger.reverse_voucher(voucher_id, "2026-07-11", "maker", "correction")
        self.assertTrue(reversal[0])
        self.assertFalse(ledger.close_period("2026-07", "controller")[0])
        self._post(int(reversal[2]), "reversal")
        self.assertTrue(ledger.close_period("2026-07", "controller")[0])
        self.assertFalse(
            ledger.create_voucher("2026-07-12", "closed", lines, creator="maker")[0]
        )
        self.assertFalse(ledger.reopen_period("2026-07", "controller", "")[0])
        self.assertTrue(ledger.reopen_period("2026-07", "controller", "approved correction")[0])

    def test_strong_line_validation_and_exact_cents(self) -> None:
        bad_cases = [
            [
                {"account_code": "", "debit_cents": 100, "credit_cents": 0},
                {"account_code": "3001", "debit_cents": 0, "credit_cents": 100},
            ],
            [
                {"account_code": "1002", "debit_cents": 100, "credit_cents": 1},
                {"account_code": "3001", "debit_cents": 0, "credit_cents": 99},
            ],
            [
                {"account_code": "1002", "debit": 1.001, "credit": 0},
                {"account_code": "3001", "debit": 0, "credit": 1.001},
            ],
            [
                {"account_code": "UNRESOLVED", "debit_cents": 100, "credit_cents": 0},
                {"account_code": "3001", "debit_cents": 0, "credit_cents": 100},
            ],
        ]
        for lines in bad_cases:
            with self.subTest(lines=lines):
                self.assertFalse(ledger.create_voucher("2026-07-10", "bad", lines)[0])
        self.assertEqual(ledger.list_vouchers(), [])

    def test_delete_only_unsubmitted_draft_and_keep_audit_trail(self) -> None:
        lines = [
            {"account_code": "5602", "debit_cents": 100, "credit_cents": 0},
            {"account_code": "2241", "debit_cents": 0, "credit_cents": 100},
        ]
        created = ledger.create_voucher("2026-07-10", "test draft", lines, creator="tester")
        self.assertTrue(created[0])
        voucher_id = int(created[2])
        deleted = ledger.delete_draft_voucher(voucher_id, "tester", "test cleanup")
        self.assertTrue(deleted[0])
        self.assertEqual(ledger.list_vouchers(), [])
        conn = db.get_conn()
        try:
            event = conn.execute(
                "SELECT * FROM audit_event WHERE entity_type='voucher' AND entity_id=? "
                "AND event_type='voucher_deleted'",
                (voucher_id,),
            ).fetchone()
            self.assertIsNotNone(event)
        finally:
            conn.close()

        posted = ledger.create_voucher("2026-07-10", "posted test", lines, creator="tester")
        self.assertTrue(posted[0])
        self._post(int(posted[2]))
        refused = ledger.delete_draft_voucher(int(posted[2]), "tester", "test cleanup")
        self.assertFalse(refused[0])
        self.assertIn("冲销", refused[1])

    def test_expense_approval_accounting_and_payment_are_separate(self) -> None:
        employee_id = expense.add_employee("Alice")
        created = expense.create_expense(
            employee_id,
            "2026-07-10",
            [{"account_code": "5602", "summary": "travel", "amount_cents": 12345}],
            "trip",
            idempotency_key="expense-1",
            submitted_by="Alice",
        )
        self.assertTrue(created[0])
        expense_id = int(created[2])
        self.assertEqual(expense.list_expense()[0]["status"], "pending_approval")
        self.assertTrue(expense.approve(expense_id, "manager")[0])
        approved = expense.list_expense()[0]
        self.assertEqual(approved["status"], "approved")
        self.assertIsNone(approved["voucher_id"])

        accounted = expense.account_expense(expense_id, "accountant")
        self.assertTrue(accounted[0])
        accounting_voucher_id = int(accounted[2])
        self.assertEqual(expense.list_expense()[0]["status"], "accounting_pending")
        self.assertTrue(ledger.audit_voucher(accounting_voucher_id, "reviewer")[0])
        self.assertTrue(ledger.post_voucher(accounting_voucher_id, "poster")[0])
        self.assertEqual(expense.list_expense()[0]["status"], "accounted")

        payment = expense.pay_expense(expense_id, "2026-07-11", actor="cashier")
        self.assertTrue(payment[0])
        cash_doc_id = int(payment[2])
        conn = db.get_conn()
        try:
            cash_doc = conn.execute("SELECT * FROM cash_doc WHERE id=?", (cash_doc_id,)).fetchone()
            payment_voucher_id = int(cash_doc["voucher_id"])
            self.assertEqual(cash_doc["amount_cents"], 12345)
        finally:
            conn.close()
        self.assertTrue(ledger.audit_voucher(payment_voucher_id, "cash-reviewer")[0])
        self.assertTrue(ledger.post_voucher(payment_voucher_id, "cash-poster")[0])
        self.assertEqual(expense.list_expense()[0]["status"], "paid")

    def test_reject_expense_has_audit_event(self) -> None:
        employee_id = expense.add_employee("Bob")
        expense_id = int(
            expense.create_expense(
                employee_id,
                "2026-07-10",
                [{"account_code": "5602", "amount_cents": 100}],
                submitted_by="Bob",
            )[2]
        )
        self.assertTrue(expense.reject_expense(expense_id, "manager", "not supported")[0])
        self.assertFalse(expense.account_expense(expense_id, "accountant")[0])
        conn = db.get_conn()
        try:
            event = conn.execute(
                "SELECT * FROM audit_event WHERE entity_type='expense' AND entity_id=? "
                "AND event_type='expense_rejected'",
                (expense_id,),
            ).fetchone()
            self.assertIsNotNone(event)
        finally:
            conn.close()

    def test_arap_project_and_atomic_cash_allocation(self) -> None:
        partner_id = arap.add_partner("供应商", "Vendor")
        item = arap.create_arap_item(
            partner_id, "应付", amount_cents=5000, reference_no="PO-1", actor="accountant"
        )
        self.assertTrue(item[0])
        cash = ledger.create_cash_doc(
            "付款",
            "2026-07-10",
            "供应商",
            partner_id,
            account_code="1002",
            summary="settle PO-1",
            creator="cashier",
            amount_cents=5000,
            arap_item_id=int(item[2]),
            idempotency_key="cash-po-1",
        )
        duplicate = ledger.create_cash_doc(
            "付款",
            "2026-07-10",
            "供应商",
            partner_id,
            account_code="1002",
            summary="settle PO-1",
            creator="cashier",
            amount_cents=5000,
            arap_item_id=int(item[2]),
            idempotency_key="cash-po-1",
        )
        self.assertTrue(cash[0])
        self.assertEqual(cash[2], duplicate[2])
        project = arap.list_arap_items(partner_id)[0]
        self.assertEqual(project["status"], "settled")
        self.assertEqual(project["outstanding_cents"], 0)


class LegacyMigrationTest(unittest.TestCase):
    def test_migration_backs_up_and_records_unresolved_accounts(self) -> None:
        old_path = db.DB_PATH
        with tempfile.TemporaryDirectory(prefix="finance-legacy-") as temp_dir:
            path = Path(temp_dir) / "legacy.db"
            conn = sqlite3.connect(str(path))
            conn.row_factory = sqlite3.Row
            db._migration_1(conn)
            conn.execute(
                "INSERT INTO employee(id,name) VALUES (1,'Legacy User')"
            )
            conn.execute(
                "INSERT INTO voucher(id,vno,date,period,summary,creator,status,source,created_at) "
                "VALUES (1,'2026-07-0001','2026-07-10','2026-07','legacy','maker','已记账','手工','2026-07-10')"
            )
            conn.execute(
                "INSERT INTO voucher_line(id,voucher_id,seq,account_code,debit,credit) "
                "VALUES (1,1,1,NULL,199,0)"
            )
            conn.execute(
                "INSERT INTO voucher_line(id,voucher_id,seq,account_code,debit,credit) "
                "VALUES (2,1,2,'2241',0,199)"
            )
            conn.execute(
                "INSERT INTO expense(id,no,employee_id,date,amount,status,voucher_id) "
                "VALUES (1,'2026-07-0001',1,'2026-07-10',199,'已记账',1)"
            )
            conn.execute(
                "INSERT INTO expense_item(id,expense_id,account_code,amount) VALUES (1,1,NULL,199)"
            )
            conn.commit()
            conn.close()
            try:
                db.configure_db_path(path)
                db.init_db(path)
                backup = Path(db.LAST_PRE_MIGRATION_BACKUP or "")
                self.assertTrue(backup.is_file())
                self.assertEqual(backup.parent, path.parent / "backups")
                version_conn = sqlite3.connect(str(path))
                try:
                    self.assertEqual(
                        version_conn.execute("PRAGMA user_version").fetchone()[0],
                        db.SCHEMA_VERSION,
                    )
                finally:
                    version_conn.close()
                issues = db.get_open_data_issues("blocking")
                self.assertEqual(len(issues), 2)
                self.assertEqual({row["amount_cents"] for row in issues}, {19900})
                voucher_issue = next(row for row in issues if row["entity_type"] == "voucher_line")
                self.assertEqual(json.loads(voucher_issue["original_value"]), None)
                self.assertTrue(
                    db.resolve_data_issue(
                        voucher_issue["id"], "5602", "accountant", "human confirmed"
                    )[0]
                )
                self.assertEqual(db.get_open_data_issues("blocking"), [])
                check = db.run_integrity_checks()
                self.assertTrue(check["ok"])
            finally:
                db.configure_db_path(old_path)


if __name__ == "__main__":
    unittest.main()
