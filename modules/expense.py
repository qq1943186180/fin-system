# -*- coding: utf-8 -*-
"""Expense workflow: submit, approve/reject, account, then pay."""

from __future__ import annotations

import os
import sqlite3
import sys
from typing import Any, Iterable


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import db
from modules import ledger


EXPENSE_STATUSES = (
    "pending_approval",
    "approved",
    "rejected",
    "accounting_draft",
    "accounting_pending",
    "accounting_audited",
    "accounted",
    "paid",
)


def add_employee(name: str, dept: str = "", phone: str = "", note: str = "") -> int:
    name = str(name or "").strip()
    if not name:
        raise ValueError("员工姓名不能为空")
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO employee(name,dept,phone,note) VALUES (?,?,?,?)",
            (name, str(dept or "").strip(), str(phone or "").strip(), str(note or "").strip()),
        )
        employee_id = int(cur.lastrowid)
        db.append_audit_event(
            conn, "employee", employee_id, "employee_created", "系统", {"name": name}
        )
    return employee_id


def list_employees() -> list[dict[str, Any]]:
    conn = db.get_conn()
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM employee ORDER BY name,id")]
    finally:
        conn.close()


def _next_expense_number(conn: sqlite3.Connection, period: str) -> str:
    row = conn.execute(
        "SELECT last_seq FROM document_sequence WHERE document_type='expense' AND period=?", (period,)
    ).fetchone()
    if row:
        sequence = int(row["last_seq"]) + 1
        conn.execute(
            "UPDATE document_sequence SET last_seq=? WHERE document_type='expense' AND period=?",
            (sequence, period),
        )
    else:
        sequence = 1
        conn.execute(
            "INSERT INTO document_sequence(document_type,period,last_seq) VALUES ('expense',?,?)",
            (period, sequence),
        )
    return f"{period}-{sequence:04d}"


def next_expense_no(date_str: str) -> str:
    period = ledger._period_of(date_str)
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT last_seq FROM document_sequence WHERE document_type='expense' AND period=?", (period,)
        ).fetchone()
        return f"{period}-{(int(row['last_seq']) + 1 if row else 1):04d}"
    finally:
        conn.close()


def _normalise_items(
    conn: sqlite3.Connection,
    items: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    source = list(items or [])
    if not source:
        raise ValueError("报销明细不能为空")
    normalized: list[dict[str, Any]] = []
    total_cents = 0
    for index, item in enumerate(source, 1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 条报销明细格式无效")
        account_code = str(item.get("account_code") or "").strip()
        account = conn.execute(
            "SELECT code,is_leaf FROM account WHERE code=?", (account_code,)
        ).fetchone()
        if not account or int(account["is_leaf"] or 0) != 1 or account_code == "UNRESOLVED":
            raise ValueError(f"第 {index} 条报销明细必须选择有效末级费用科目")
        if item.get("amount_cents") is not None:
            cents = ledger._strict_cents(item["amount_cents"], "amount_cents")
            if item.get("amount") not in (None, "") and ledger._money_to_cents(item["amount"]) != cents:
                raise ValueError(f"第 {index} 条报销明细金额元/分不一致")
        else:
            cents = ledger._money_to_cents(item.get("amount"), f"第 {index} 条金额")
        if cents <= 0:
            raise ValueError(f"第 {index} 条报销金额必须大于零")
        total_cents += cents
        normalized.append(
            {
                "account_code": account_code,
                "summary": str(item.get("summary") or "").strip(),
                "amount_cents": cents,
                "invoice_ref": str(item.get("invoice_ref") or "").strip(),
            }
        )
    return normalized, total_cents


def create_expense(
    emp_id: int,
    date_str: str,
    items: Iterable[dict[str, Any]],
    summary: str = "",
    approver: str = "",
    idempotency_key: str | None = None,
    submitted_by: str = "系统",
) -> tuple[bool, str, int | None]:
    """Submit an expense for approval; approval is always a separate action."""
    del approver  # Kept only for source compatibility; pre-approval is intentionally not supported.
    try:
        date_str = ledger._validate_date(date_str)
        period = date_str[:7]
        submitted_by = str(submitted_by or "").strip()
        if not submitted_by:
            raise ValueError("提交人不能为空")
        key = ledger._normalise_idempotency_key(idempotency_key)
        with db.transaction() as conn:
            if key:
                existing = conn.execute(
                    "SELECT id,no FROM expense WHERE idempotency_key=?", (key,)
                ).fetchone()
                if existing:
                    return True, f"相同报销业务已存在：{existing['no']}", int(existing["id"])
            if ledger._is_period_closed_conn(conn, period):
                raise ValueError(f"会计期间 {period} 已结账")
            employee = conn.execute("SELECT id,name FROM employee WHERE id=?", (emp_id,)).fetchone()
            if not employee:
                raise ValueError("员工不存在")
            normalized, total_cents = _normalise_items(conn, items)
            number = _next_expense_number(conn, period)
            now = ledger._now()
            cur = conn.execute(
                "INSERT INTO expense(no,employee_id,date,amount,amount_cents,summary,status,idempotency_key,"
                "submitted_by,submitted_at,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,'pending_approval',?,?,?,?,?)",
                (
                    number,
                    emp_id,
                    date_str,
                    total_cents / 100,
                    total_cents,
                    str(summary or "").strip(),
                    key,
                    submitted_by,
                    now,
                    now,
                    now,
                ),
            )
            expense_id = int(cur.lastrowid)
            for item in normalized:
                conn.execute(
                    "INSERT INTO expense_item(expense_id,account_code,summary,amount,amount_cents,invoice_ref) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        expense_id,
                        item["account_code"],
                        item["summary"],
                        item["amount_cents"] / 100,
                        item["amount_cents"],
                        item["invoice_ref"],
                    ),
                )
            db.append_audit_event(
                conn,
                "expense",
                expense_id,
                "expense_submitted",
                submitted_by,
                {"number": number, "employee_id": emp_id, "amount_cents": total_cents},
            )
        return True, f"报销单 {number} 已提交审批，金额 {total_cents / 100:.2f}", expense_id
    except (ValueError, sqlite3.Error) as exc:
        return False, str(exc), None


def approve(eid: int, approver: str) -> tuple[bool, str, None]:
    """Approve only; accounting and payment remain explicit later steps."""
    try:
        approver = str(approver or "").strip()
        if not approver:
            raise ValueError("审批人不能为空")
        with db.transaction() as conn:
            expense = conn.execute("SELECT * FROM expense WHERE id=?", (eid,)).fetchone()
            if not expense:
                raise ValueError("报销单不存在")
            if expense["status"] == "approved":
                return True, "报销单已经批准", None
            if expense["status"] != "pending_approval":
                raise ValueError(f"当前状态 {expense['status']} 不允许审批")
            now = ledger._now()
            conn.execute(
                "UPDATE expense SET status='approved',approver=?,approved_at=?,updated_at=? WHERE id=?",
                (approver, now, now, eid),
            )
            db.append_audit_event(
                conn, "expense", eid, "expense_approved", approver, {"from": "pending_approval", "to": "approved"}
            )
        return True, "报销单已批准，尚未记账或付款", None
    except (ValueError, sqlite3.Error) as exc:
        return False, str(exc), None


def reject_expense(eid: int, actor: str, reason: str) -> tuple[bool, str]:
    try:
        actor = str(actor or "").strip()
        reason = str(reason or "").strip()
        if not actor or not reason:
            raise ValueError("驳回人和驳回原因不能为空")
        with db.transaction() as conn:
            expense = conn.execute("SELECT * FROM expense WHERE id=?", (eid,)).fetchone()
            if not expense:
                raise ValueError("报销单不存在")
            if expense["status"] == "rejected":
                return True, "报销单已经驳回"
            if expense["status"] != "pending_approval":
                raise ValueError(f"当前状态 {expense['status']} 不允许驳回")
            now = ledger._now()
            conn.execute(
                "UPDATE expense SET status='rejected',rejected_by=?,rejected_at=?,rejection_reason=?,"
                "updated_at=? WHERE id=?",
                (actor, now, reason, now, eid),
            )
            db.append_audit_event(
                conn,
                "expense",
                eid,
                "expense_rejected",
                actor,
                {"from": "pending_approval", "to": "rejected", "reason": reason},
            )
        return True, "报销单已驳回"
    except (ValueError, sqlite3.Error) as exc:
        return False, str(exc)


def account_expense(
    eid: int,
    actor: str,
    idempotency_key: str | None = None,
    submit: bool = True,
) -> tuple[bool, str, int | None]:
    """Atomically attach a balanced voucher to an approved expense."""
    try:
        actor = str(actor or "").strip()
        if not actor:
            raise ValueError("制单人不能为空")
        with db.transaction() as conn:
            expense = conn.execute("SELECT * FROM expense WHERE id=?", (eid,)).fetchone()
            if not expense:
                raise ValueError("报销单不存在")
            if expense["voucher_id"]:
                voucher = conn.execute(
                    "SELECT vno FROM voucher WHERE id=?", (expense["voucher_id"],)
                ).fetchone()
                return True, f"报销单已有凭证 {voucher['vno'] if voucher else expense['voucher_id']}", int(expense["voucher_id"])
            if expense["status"] != "approved":
                raise ValueError(f"报销单必须先批准，当前状态为 {expense['status']}")
            employee = conn.execute(
                "SELECT name FROM employee WHERE id=?", (expense["employee_id"],)
            ).fetchone()
            items = conn.execute(
                "SELECT * FROM expense_item WHERE expense_id=? ORDER BY id", (eid,)
            ).fetchall()
            if not items:
                raise ValueError("报销单没有明细")
            lines = [
                {
                    "account_code": item["account_code"],
                    "summary": item["summary"] or expense["summary"],
                    "debit_cents": int(item["amount_cents"]),
                    "credit_cents": 0,
                }
                for item in items
            ]
            lines.append(
                {
                    "account_code": "2241",
                    "summary": f"报销-{employee['name'] if employee else ''}",
                    "debit_cents": 0,
                    "credit_cents": int(expense["amount_cents"]),
                    "aux_type": "员工",
                    "aux_id": expense["employee_id"],
                }
            )
            key = ledger._normalise_idempotency_key(idempotency_key) or f"expense:{eid}:accounting"
            voucher_id, vno, _ = ledger._create_voucher_in_transaction(
                conn,
                expense["date"],
                f"报销-{employee['name'] if employee else ''}",
                lines,
                actor,
                "expense",
                eid,
                key,
            )
            now = ledger._now()
            conn.execute(
                "UPDATE expense SET voucher_id=?,status='accounting_draft',updated_at=? WHERE id=?",
                (voucher_id, now, eid),
            )
            db.append_audit_event(
                conn,
                "expense",
                eid,
                "expense_voucher_created",
                actor,
                {"voucher_id": voucher_id, "vno": vno},
            )
            if submit:
                ledger._transition_in_transaction(conn, voucher_id, "pending_review", actor, None)
        return True, f"报销单已生成凭证 {vno}{'并提交审核' if submit else '（草稿）'}", voucher_id
    except (ValueError, sqlite3.Error) as exc:
        return False, str(exc), None


def pay_expense(
    eid: int,
    date_str: str,
    account_code: str = "1002",
    actor: str = "系统",
    *,
    bank_account_id: int | None = None,
    idempotency_key: str | None = None,
    submit: bool = True,
) -> tuple[bool, str, int | None]:
    """Create the payment cash document; expense becomes paid only after that voucher posts."""
    conn = db.get_conn()
    try:
        expense = conn.execute("SELECT * FROM expense WHERE id=?", (eid,)).fetchone()
        if not expense:
            return False, "报销单不存在", None
        if expense["status"] == "paid":
            return True, "报销单已经付款", expense["payment_cash_doc_id"]
        if expense["status"] != "accounted":
            return False, f"报销单必须先记账，当前状态为 {expense['status']}", None
        employee = conn.execute(
            "SELECT name FROM employee WHERE id=?", (expense["employee_id"],)
        ).fetchone()
        amount_cents = int(expense["amount_cents"])
        employee_id = int(expense["employee_id"])
        employee_name = employee["name"] if employee else ""
    finally:
        conn.close()
    key = ledger._normalise_idempotency_key(idempotency_key) or f"expense:{eid}:payment"
    return ledger.create_cash_doc(
        "payment",
        date_str,
        "员工",
        employee_id,
        account_code=account_code,
        summary=f"支付报销款-{employee_name}",
        creator=actor,
        amount_cents=amount_cents,
        counterparty_account_code="2241",
        bank_account_id=bank_account_id,
        idempotency_key=key,
        submit=submit,
        related_type="expense_payment",
        related_id=eid,
    )


def list_expense(status: str | None = None) -> list[dict[str, Any]]:
    conn = db.get_conn()
    try:
        sql = (
            "SELECT e.*,m.name emp_name,v.vno FROM expense e "
            "LEFT JOIN employee m ON m.id=e.employee_id LEFT JOIN voucher v ON v.id=e.voucher_id"
        )
        args: list[Any] = []
        if status:
            if status not in EXPENSE_STATUSES:
                raise ValueError("报销状态无效")
            sql += " WHERE e.status=?"
            args.append(status)
        sql += " ORDER BY e.date DESC,e.id DESC"
        output = []
        for row in conn.execute(sql, args):
            item = dict(row)
            item["amount"] = int(item["amount_cents"] or 0) / 100
            output.append(item)
        return output
    finally:
        conn.close()


if __name__ == "__main__":
    db.init_db()
    print(list_expense())
