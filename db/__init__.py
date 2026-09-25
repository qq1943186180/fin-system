# -*- coding: utf-8 -*-
"""SQLite persistence, migrations, backup, and data-quality controls."""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any, Iterator, Optional


SCHEMA_VERSION = 6
_maintenance = threading.local()


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _path_from_app_paths(helper: str) -> Optional[Path]:
    """Use the shared path module when present, without making it mandatory."""
    try:
        import app_paths  # type: ignore

        fn = getattr(app_paths, helper, None)
        if callable(fn):
            value = fn()
            if value:
                return Path(value).expanduser().resolve()
    except (ImportError, OSError, TypeError, ValueError):
        pass
    return None


def _default_db_path() -> Path:
    override = os.environ.get("FINANCE_SYSTEM_DB_PATH")
    if override:
        return Path(override).expanduser().resolve()
    shared = _path_from_app_paths("get_db_path")
    if shared:
        return shared
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return (Path(local) / "FinanceSystem" / "data" / "finance.db").resolve()


DB_PATH = str(_default_db_path())
LAST_PRE_MIGRATION_BACKUP: str | None = None


def configure_db_path(path: os.PathLike[str] | str) -> str:
    """Override the process database path (primarily for tests and recovery tools)."""
    global DB_PATH
    DB_PATH = str(Path(path).expanduser().resolve())
    return DB_PATH


def _legacy_candidates() -> list[Path]:
    candidates = [_project_root() / "db" / "finance.db"]
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.extend((exe_dir / "db" / "finance.db", exe_dir / "finance.db"))
    return candidates


def _allow_maintenance() -> int:
    return int(bool(getattr(_maintenance, "allow_posted_mutation", False)))


@contextlib.contextmanager
def _posted_mutation_allowed() -> Iterator[None]:
    previous = bool(getattr(_maintenance, "allow_posted_mutation", False))
    _maintenance.allow_posted_mutation = True
    try:
        yield
    finally:
        _maintenance.allow_posted_mutation = previous


def get_conn(db_path: os.PathLike[str] | str | None = None) -> sqlite3.Connection:
    path = Path(db_path or DB_PATH).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.create_function("finance_allow_posted_mutation", 0, _allow_maintenance)
    return conn


@contextlib.contextmanager
def transaction(
    conn: sqlite3.Connection | None = None,
    *,
    immediate: bool = True,
) -> Iterator[sqlite3.Connection]:
    """Run a transaction and roll it back on every failure."""
    owns_connection = conn is None
    connection = conn or get_conn()
    try:
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        if owns_connection:
            connection.close()


# Small Enterprise Accounting Standards chart of accounts (simplified).
# (code, name, category, normal direction, parent, is_leaf)
ACCOUNTS = [
    ("1001", "库存现金", "资产", "借", "", 1),
    ("1002", "银行存款", "资产", "借", "", 1),
    ("1012", "其他货币资金", "资产", "借", "", 1),
    ("1121", "应收票据", "资产", "借", "", 1),
    ("1122", "应收账款", "资产", "借", "", 1),
    ("1123", "预付账款", "资产", "借", "", 1),
    ("1221", "其他应收款", "资产", "借", "", 1),
    ("1231", "坏账准备", "资产", "贷", "", 1),
    ("1401", "材料采购", "资产", "借", "", 1),
    ("1403", "原材料", "资产", "借", "", 1),
    ("1405", "库存商品", "资产", "借", "", 1),
    ("1511", "长期股权投资", "资产", "借", "", 1),
    ("1601", "固定资产", "资产", "借", "", 1),
    ("1602", "累计折旧", "资产", "贷", "", 1),
    ("1701", "无形资产", "资产", "借", "", 1),
    ("1702", "累计摊销", "资产", "贷", "", 1),
    ("1901", "待处理财产损溢", "资产", "借", "", 1),
    ("2001", "短期借款", "负债", "贷", "", 1),
    ("2201", "应付票据", "负债", "贷", "", 1),
    ("2202", "应付账款", "负债", "贷", "", 1),
    ("2203", "预收账款", "负债", "贷", "", 1),
    ("2211", "应付职工薪酬", "负债", "贷", "", 1),
    ("2221", "应交税费", "负债", "贷", "", 1),
    ("2231", "应付利息", "负债", "贷", "", 1),
    ("2241", "其他应付款", "负债", "贷", "", 1),
    ("2501", "长期借款", "负债", "贷", "", 1),
    ("3001", "实收资本", "权益", "贷", "", 1),
    ("3002", "资本公积", "权益", "贷", "", 1),
    ("3101", "盈余公积", "权益", "贷", "", 1),
    ("3103", "本年利润", "权益", "贷", "", 1),
    ("3104", "利润分配", "权益", "贷", "", 1),
    ("4001", "生产成本", "成本", "借", "", 1),
    ("4101", "制造费用", "成本", "借", "", 1),
    ("4301", "研发支出", "成本", "借", "", 1),
    ("5001", "主营业务收入", "损益", "贷", "", 1),
    ("5051", "其他业务收入", "损益", "贷", "", 1),
    ("5111", "投资收益", "损益", "贷", "", 1),
    ("5301", "营业外收入", "损益", "贷", "", 1),
    ("5401", "主营业务成本", "损益", "借", "", 1),
    ("5402", "其他业务成本", "损益", "借", "", 1),
    ("5601", "销售费用", "损益", "借", "", 1),
    ("5602", "管理费用", "损益", "借", "", 1),
    ("5603", "财务费用", "损益", "借", "", 1),
    ("5711", "营业外支出", "损益", "借", "", 1),
    ("5801", "所得税费用", "损益", "借", "", 1),
]


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, definition: str) -> None:
    name = definition.split()[0]
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def _migration_1(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS company ("
        "id INTEGER PRIMARY KEY CHECK(id=1), name TEXT, tax_no TEXT, addr TEXT, note TEXT)"
    )
    conn.execute("INSERT OR IGNORE INTO company(id) VALUES (1)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS account ("
        "code TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT, direction TEXT, "
        "parent TEXT, is_leaf INTEGER DEFAULT 1)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS period ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, year INTEGER, month INTEGER, "
        "closed INTEGER DEFAULT 0, UNIQUE(year, month))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS voucher ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, vno TEXT UNIQUE, date TEXT, period TEXT, "
        "summary TEXT, creator TEXT, status TEXT DEFAULT '草稿', source TEXT DEFAULT '手工', "
        "ref_id INTEGER, created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS voucher_line ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, voucher_id INTEGER, seq INTEGER, "
        "account_code TEXT, summary TEXT, debit REAL DEFAULT 0, credit REAL DEFAULT 0, "
        "aux_type TEXT, aux_id INTEGER, "
        "FOREIGN KEY(voucher_id) REFERENCES voucher(id) ON DELETE CASCADE, "
        "FOREIGN KEY(account_code) REFERENCES account(code))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS bank_account ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, account_no TEXT, bank TEXT, "
        "opening_balance REAL DEFAULT 0, account_code TEXT DEFAULT '1002', note TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS partner ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT, name TEXT, tax_no TEXT, "
        "contact TEXT, phone TEXT, addr TEXT, note TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS employee ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, dept TEXT, phone TEXT, note TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cash_doc ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, doc_type TEXT, date TEXT, partner_type TEXT, "
        "partner_id INTEGER, amount REAL, account_code TEXT, summary TEXT, voucher_id INTEGER, "
        "status TEXT DEFAULT '未记账')"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS expense ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, no TEXT, employee_id INTEGER, date TEXT, "
        "amount REAL, summary TEXT, status TEXT DEFAULT '待审批', approver TEXT, "
        "voucher_id INTEGER, feishu_instance_id TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS expense_item ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, expense_id INTEGER, account_code TEXT, "
        "summary TEXT, amount REAL, invoice_ref TEXT, "
        "FOREIGN KEY(expense_id) REFERENCES expense(id) ON DELETE CASCADE)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS invoice_doc ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_no TEXT, date TEXT, seller TEXT, "
        "seller_tax_no TEXT, buyer TEXT, buyer_tax_no TEXT, amount REAL, tax REAL, total REAL, "
        "file_path TEXT, voucher_id INTEGER, status TEXT DEFAULT '未入账')"
    )
    conn.executemany(
        "INSERT OR IGNORE INTO account(code,name,category,direction,parent,is_leaf) "
        "VALUES (?,?,?,?,?,?)",
        ACCOUNTS,
    )


def _migration_2(conn: sqlite3.Connection) -> None:
    now = _dt.datetime.now().isoformat(timespec="seconds")
    additions = {
        "voucher": [
            "submitted_by TEXT", "submitted_at TEXT", "reviewer TEXT", "audited_at TEXT",
            "poster TEXT", "posted_at TEXT", "reversal_of_id INTEGER", "idempotency_key TEXT",
            "exception_reason TEXT", "metadata_json TEXT", "updated_at TEXT",
        ],
        "voucher_line": ["debit_cents INTEGER NOT NULL DEFAULT 0", "credit_cents INTEGER NOT NULL DEFAULT 0"],
        "period": [
            "closed_by TEXT", "closed_at TEXT", "reopened_by TEXT", "reopened_at TEXT",
            "reopen_reason TEXT",
        ],
        "bank_account": ["opening_balance_cents INTEGER NOT NULL DEFAULT 0"],
        "cash_doc": [
            "doc_no TEXT", "amount_cents INTEGER NOT NULL DEFAULT 0", "counterparty_account_code TEXT",
            "bank_account_id INTEGER", "arap_item_id INTEGER", "idempotency_key TEXT",
            "related_type TEXT", "related_id INTEGER", "created_at TEXT", "updated_at TEXT",
        ],
        "expense": [
            "amount_cents INTEGER NOT NULL DEFAULT 0", "idempotency_key TEXT", "submitted_by TEXT",
            "submitted_at TEXT", "approved_at TEXT", "rejected_by TEXT", "rejected_at TEXT",
            "rejection_reason TEXT", "accounted_at TEXT", "payment_cash_doc_id INTEGER",
            "paid_at TEXT", "created_at TEXT", "updated_at TEXT",
        ],
        "expense_item": ["amount_cents INTEGER NOT NULL DEFAULT 0"],
        "invoice_doc": [
            "net_amount_cents INTEGER NOT NULL DEFAULT 0", "tax_cents INTEGER NOT NULL DEFAULT 0",
            "total_cents INTEGER NOT NULL DEFAULT 0", "file_hash TEXT", "ocr_version TEXT",
            "ocr_confidence REAL", "partner_id INTEGER", "expense_account TEXT",
            "payment_status TEXT", "idempotency_key TEXT", "created_at TEXT", "updated_at TEXT",
        ],
    }
    for table, definitions in additions.items():
        for definition in definitions:
            _add_column(conn, table, definition)

    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migration ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS data_issue ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, issue_type TEXT NOT NULL, severity TEXT NOT NULL DEFAULT 'error', "
        "entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL, field_name TEXT, original_value TEXT, "
        "amount_cents INTEGER, status TEXT NOT NULL DEFAULT 'open', resolution_value TEXT, "
        "resolution_reason TEXT, resolved_by TEXT, resolved_at TEXT, details_json TEXT, created_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS audit_event ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL, entity_id INTEGER, "
        "event_type TEXT NOT NULL, actor TEXT NOT NULL, details_json TEXT, created_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS voucher_sequence ("
        "period TEXT PRIMARY KEY, last_seq INTEGER NOT NULL CHECK(last_seq >= 0))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS document_sequence ("
        "document_type TEXT NOT NULL, period TEXT NOT NULL, last_seq INTEGER NOT NULL CHECK(last_seq >= 0), "
        "PRIMARY KEY(document_type, period))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS arap_item ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, partner_id INTEGER NOT NULL, item_type TEXT NOT NULL, "
        "reference_no TEXT, description TEXT, amount_cents INTEGER NOT NULL CHECK(amount_cents > 0), "
        "settled_cents INTEGER NOT NULL DEFAULT 0 CHECK(settled_cents >= 0), due_date TEXT, "
        "status TEXT NOT NULL DEFAULT 'open', source_type TEXT, source_id INTEGER, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(partner_id) REFERENCES partner(id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS settlement_allocation ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, cash_doc_id INTEGER NOT NULL, arap_item_id INTEGER NOT NULL, "
        "amount_cents INTEGER NOT NULL CHECK(amount_cents > 0), created_at TEXT NOT NULL, "
        "UNIQUE(cash_doc_id, arap_item_id), FOREIGN KEY(cash_doc_id) REFERENCES cash_doc(id), "
        "FOREIGN KEY(arap_item_id) REFERENCES arap_item(id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS integration_job ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, integration TEXT NOT NULL, operation TEXT NOT NULL, "
        "entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL, request_id TEXT, idempotency_key TEXT, "
        "status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, "
        "last_error_type TEXT, last_error_message TEXT, next_attempt_at TEXT, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.execute("UPDATE voucher SET updated_at=COALESCE(updated_at, created_at, ?)", (now,))
    conn.execute("UPDATE cash_doc SET created_at=COALESCE(created_at, ?), updated_at=COALESCE(updated_at, ?)", (now, now))
    conn.execute("UPDATE expense SET created_at=COALESCE(created_at, ?), updated_at=COALESCE(updated_at, ?)", (now, now))
    conn.execute("UPDATE invoice_doc SET created_at=COALESCE(created_at, ?), updated_at=COALESCE(updated_at, ?)", (now, now))


def _audit(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: int | None,
    event_type: str,
    actor: str,
    details: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,details_json,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            entity_type,
            entity_id,
            event_type,
            actor,
            json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
            _dt.datetime.now().isoformat(timespec="seconds"),
        ),
    )


def append_audit_event(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: int | None,
    event_type: str,
    actor: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Append an audit record inside the caller's transaction."""
    _audit(conn, entity_type, entity_id, event_type, actor, details)


def _record_issue(
    conn: sqlite3.Connection,
    *,
    issue_type: str,
    severity: str,
    entity_type: str,
    entity_id: int,
    field_name: str,
    original_value: Any,
    amount_cents: int | None = None,
    details: dict[str, Any] | None = None,
) -> int:
    existing = conn.execute(
        "SELECT id FROM data_issue WHERE issue_type=? AND entity_type=? AND entity_id=? "
        "AND COALESCE(field_name,'')=? AND status='open'",
        (issue_type, entity_type, entity_id, field_name or ""),
    ).fetchone()
    if existing:
        return int(existing["id"])
    cur = conn.execute(
        "INSERT INTO data_issue(issue_type,severity,entity_type,entity_id,field_name,original_value,"
        "amount_cents,status,details_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            issue_type,
            severity,
            entity_type,
            entity_id,
            field_name,
            json.dumps(original_value, ensure_ascii=False),
            amount_cents,
            "open",
            json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
            _dt.datetime.now().isoformat(timespec="seconds"),
        ),
    )
    return int(cur.lastrowid)


def _migration_3(conn: sqlite3.Connection) -> None:
    # Integer cents become authoritative while legacy REAL columns remain as read compatibility fields.
    conn.execute(
        "UPDATE voucher_line SET debit_cents=CAST(ROUND(COALESCE(debit,0)*100) AS INTEGER), "
        "credit_cents=CAST(ROUND(COALESCE(credit,0)*100) AS INTEGER)"
    )
    conn.execute("UPDATE bank_account SET opening_balance_cents=CAST(ROUND(COALESCE(opening_balance,0)*100) AS INTEGER)")
    conn.execute("UPDATE cash_doc SET amount_cents=CAST(ROUND(COALESCE(amount,0)*100) AS INTEGER)")
    conn.execute("UPDATE expense SET amount_cents=CAST(ROUND(COALESCE(amount,0)*100) AS INTEGER)")
    conn.execute("UPDATE expense_item SET amount_cents=CAST(ROUND(COALESCE(amount,0)*100) AS INTEGER)")
    conn.execute(
        "UPDATE invoice_doc SET net_amount_cents=CAST(ROUND(COALESCE(amount,0)*100) AS INTEGER), "
        "tax_cents=CAST(ROUND(COALESCE(tax,0)*100) AS INTEGER), "
        "total_cents=CAST(ROUND(COALESCE(total,0)*100) AS INTEGER)"
    )

    voucher_statuses = {
        "草稿": "draft", "待审核": "pending_review", "已审核": "audited", "已记账": "posted",
        "draft": "draft", "pending_review": "pending_review", "audited": "audited", "posted": "posted",
    }
    for old, new in voucher_statuses.items():
        conn.execute("UPDATE voucher SET status=? WHERE status=?", (new, old))
    source_names = {"手工": "manual", "收付款": "cash", "报销": "expense", "发票": "invoice", "冲销": "reversal"}
    for old, new in source_names.items():
        conn.execute("UPDATE voucher SET source=? WHERE source=?", (new, old))
    expense_statuses = {
        "待审批": "pending_approval", "已批准": "approved", "已驳回": "rejected",
        "已记账": "accounted", "已付款": "paid",
    }
    for old, new in expense_statuses.items():
        conn.execute("UPDATE expense SET status=? WHERE status=?", (new, old))
    cash_statuses = {"未记账": "draft", "待审核": "pending_review", "已记账": "posted"}
    for old, new in cash_statuses.items():
        conn.execute("UPDATE cash_doc SET status=? WHERE status=?", (new, old))
    doc_types = {"收款": "receipt", "付款": "payment"}
    for old, new in doc_types.items():
        conn.execute("UPDATE cash_doc SET doc_type=? WHERE doc_type=?", (new, old))

    conn.execute(
        "INSERT OR IGNORE INTO account(code,name,category,direction,parent,is_leaf) "
        "VALUES ('UNRESOLVED','待确认历史科目','待处理','借','',1)"
    )
    invalid = conn.execute(
        "SELECT vl.id,vl.voucher_id,vl.account_code,vl.debit_cents,vl.credit_cents,v.vno,v.period "
        "FROM voucher_line vl JOIN voucher v ON v.id=vl.voucher_id "
        "LEFT JOIN account a ON a.code=vl.account_code "
        "WHERE a.code IS NULL OR TRIM(COALESCE(vl.account_code,''))=''"
    ).fetchall()
    for row in invalid:
        original = row["account_code"]
        conn.execute("UPDATE voucher_line SET account_code='UNRESOLVED' WHERE id=?", (row["id"],))
        issue_id = _record_issue(
            conn,
            issue_type="invalid_account",
            severity="blocking",
            entity_type="voucher_line",
            entity_id=int(row["id"]),
            field_name="account_code",
            original_value=original,
            amount_cents=max(int(row["debit_cents"] or 0), int(row["credit_cents"] or 0)),
            details={"voucher_id": row["voucher_id"], "vno": row["vno"], "period": row["period"]},
        )
        _audit(
            conn,
            "voucher_line",
            int(row["id"]),
            "historical_account_normalized",
            "migration",
            {"issue_id": issue_id, "original_value": original, "replacement": "UNRESOLVED"},
        )

    invalid_expense_items = conn.execute(
        "SELECT ei.id,ei.expense_id,ei.account_code,ei.amount_cents,e.voucher_id,e.no "
        "FROM expense_item ei JOIN expense e ON e.id=ei.expense_id "
        "LEFT JOIN account a ON a.code=ei.account_code "
        "WHERE a.code IS NULL OR TRIM(COALESCE(ei.account_code,''))=''"
    ).fetchall()
    for row in invalid_expense_items:
        original = row["account_code"]
        conn.execute("UPDATE expense_item SET account_code='UNRESOLVED' WHERE id=?", (row["id"],))
        issue_id = _record_issue(
            conn,
            issue_type="invalid_account",
            severity="blocking",
            entity_type="expense_item",
            entity_id=int(row["id"]),
            field_name="account_code",
            original_value=original,
            amount_cents=int(row["amount_cents"] or 0),
            details={
                "expense_id": row["expense_id"],
                "expense_no": row["no"],
                "voucher_id": row["voucher_id"],
            },
        )
        _audit(
            conn,
            "expense_item",
            int(row["id"]),
            "historical_account_normalized",
            "migration",
            {"issue_id": issue_id, "original_value": original, "replacement": "UNRESOLVED"},
        )

    # Unknown legacy states are preserved as issues, then moved to the safest editable state.
    unknown = conn.execute(
        "SELECT id,status FROM voucher WHERE status NOT IN ('draft','pending_review','audited','posted')"
    ).fetchall()
    for row in unknown:
        _record_issue(
            conn,
            issue_type="invalid_voucher_status",
            severity="blocking",
            entity_type="voucher",
            entity_id=int(row["id"]),
            field_name="status",
            original_value=row["status"],
        )
        conn.execute("UPDATE voucher SET status='draft' WHERE id=?", (row["id"],))

    # Seed transaction-safe sequence counters from all legacy numbers.
    periods = conn.execute("SELECT DISTINCT period FROM voucher WHERE period IS NOT NULL").fetchall()
    for period_row in periods:
        period = period_row["period"]
        last = 0
        for vno_row in conn.execute("SELECT vno FROM voucher WHERE period=?", (period,)):
            try:
                last = max(last, int(str(vno_row["vno"]).rsplit("-", 1)[-1]))
            except (TypeError, ValueError):
                continue
        conn.execute(
            "INSERT INTO voucher_sequence(period,last_seq) VALUES (?,?) "
            "ON CONFLICT(period) DO UPDATE SET last_seq=MAX(last_seq,excluded.last_seq)",
            (period, last),
        )

    expense_periods = conn.execute(
        "SELECT DISTINCT SUBSTR(date,1,7) period FROM expense WHERE date IS NOT NULL"
    ).fetchall()
    for period_row in expense_periods:
        period = period_row["period"]
        last = 0
        for number_row in conn.execute(
            "SELECT no FROM expense WHERE SUBSTR(date,1,7)=?", (period,)
        ):
            try:
                last = max(last, int(str(number_row["no"]).rsplit("-", 1)[-1]))
            except (TypeError, ValueError):
                continue
        conn.execute(
            "INSERT INTO document_sequence(document_type,period,last_seq) VALUES ('expense',?,?) "
            "ON CONFLICT(document_type,period) DO UPDATE SET last_seq=MAX(last_seq,excluded.last_seq)",
            (period, last),
        )


def _migration_4(conn: sqlite3.Connection) -> None:
    indexes = [
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_voucher_source_idempotency ON voucher(source,idempotency_key) "
        "WHERE idempotency_key IS NOT NULL AND TRIM(idempotency_key)<>''",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_cash_doc_idempotency ON cash_doc(idempotency_key) "
        "WHERE idempotency_key IS NOT NULL AND TRIM(idempotency_key)<>''",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_cash_doc_voucher ON cash_doc(voucher_id) WHERE voucher_id IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_expense_idempotency ON expense(idempotency_key) "
        "WHERE idempotency_key IS NOT NULL AND TRIM(idempotency_key)<>''",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_expense_no ON expense(no) WHERE no IS NOT NULL AND TRIM(no)<>''",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_expense_voucher ON expense(voucher_id) WHERE voucher_id IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_invoice_voucher ON invoice_doc(voucher_id) WHERE voucher_id IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_invoice_idempotency ON invoice_doc(idempotency_key) "
        "WHERE idempotency_key IS NOT NULL AND TRIM(idempotency_key)<>''",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_invoice_file_hash ON invoice_doc(file_hash) "
        "WHERE file_hash IS NOT NULL AND TRIM(file_hash)<>''",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_invoice_number_seller ON invoice_doc(invoice_no,seller_tax_no) "
        "WHERE invoice_no IS NOT NULL AND TRIM(invoice_no)<>'' AND seller_tax_no IS NOT NULL AND TRIM(seller_tax_no)<>''",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_integration_job_idempotency ON integration_job(integration,idempotency_key) "
        "WHERE idempotency_key IS NOT NULL AND TRIM(idempotency_key)<>''",
        "CREATE INDEX IF NOT EXISTS ix_voucher_period_status ON voucher(period,status)",
        "CREATE INDEX IF NOT EXISTS ix_voucher_line_account ON voucher_line(account_code,voucher_id)",
        "CREATE INDEX IF NOT EXISTS ix_data_issue_status_severity ON data_issue(status,severity)",
        "CREATE INDEX IF NOT EXISTS ix_audit_entity ON audit_event(entity_type,entity_id,id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_voucher_line_sequence ON voucher_line(voucher_id,seq)",
    ]
    for statement in indexes:
        conn.execute(statement)

    trigger_names = [
        "audit_event_no_update", "audit_event_no_delete", "posted_voucher_no_update",
        "posted_voucher_no_delete", "posted_line_no_insert", "posted_line_no_update",
        "posted_line_no_delete", "voucher_status_insert", "voucher_status_transition",
        "voucher_posting_guard", "voucher_closed_period_insert", "voucher_closed_period_update",
        "voucher_line_validate_insert", "voucher_line_validate_update",
        "expense_item_validate_insert", "expense_item_validate_update",
    ]
    for name in trigger_names:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    conn.execute(
        "CREATE TRIGGER audit_event_no_update BEFORE UPDATE ON audit_event "
        "BEGIN SELECT RAISE(ABORT,'audit_event is append-only'); END"
    )
    conn.execute(
        "CREATE TRIGGER audit_event_no_delete BEFORE DELETE ON audit_event "
        "BEGIN SELECT RAISE(ABORT,'audit_event is append-only'); END"
    )
    conn.execute(
        "CREATE TRIGGER posted_voucher_no_update BEFORE UPDATE ON voucher "
        "WHEN OLD.status='posted' AND finance_allow_posted_mutation()=0 "
        "BEGIN SELECT RAISE(ABORT,'posted voucher is immutable'); END"
    )
    conn.execute(
        "CREATE TRIGGER posted_voucher_no_delete BEFORE DELETE ON voucher "
        "WHEN OLD.status='posted' AND finance_allow_posted_mutation()=0 "
        "BEGIN SELECT RAISE(ABORT,'posted voucher is immutable'); END"
    )
    conn.execute(
        "CREATE TRIGGER posted_line_no_insert BEFORE INSERT ON voucher_line "
        "WHEN EXISTS(SELECT 1 FROM voucher WHERE id=NEW.voucher_id AND status='posted') "
        "AND finance_allow_posted_mutation()=0 "
        "BEGIN SELECT RAISE(ABORT,'posted voucher lines are immutable'); END"
    )
    conn.execute(
        "CREATE TRIGGER posted_line_no_update BEFORE UPDATE ON voucher_line "
        "WHEN EXISTS(SELECT 1 FROM voucher WHERE id=OLD.voucher_id AND status='posted') "
        "AND finance_allow_posted_mutation()=0 "
        "BEGIN SELECT RAISE(ABORT,'posted voucher lines are immutable'); END"
    )
    conn.execute(
        "CREATE TRIGGER posted_line_no_delete BEFORE DELETE ON voucher_line "
        "WHEN EXISTS(SELECT 1 FROM voucher WHERE id=OLD.voucher_id AND status='posted') "
        "AND finance_allow_posted_mutation()=0 "
        "BEGIN SELECT RAISE(ABORT,'posted voucher lines are immutable'); END"
    )
    conn.execute(
        "CREATE TRIGGER voucher_status_insert BEFORE INSERT ON voucher "
        "WHEN NEW.status NOT IN ('draft','pending_review','audited','posted') "
        "BEGIN SELECT RAISE(ABORT,'invalid voucher status'); END"
    )
    conn.execute(
        "CREATE TRIGGER voucher_status_transition BEFORE UPDATE OF status ON voucher "
        "WHEN NOT ((OLD.status='draft' AND NEW.status='pending_review') OR "
        "(OLD.status='pending_review' AND NEW.status='audited') OR "
        "(OLD.status='audited' AND NEW.status='posted')) "
        "BEGIN SELECT RAISE(ABORT,'invalid voucher status transition'); END"
    )
    conn.execute(
        "CREATE TRIGGER voucher_posting_guard BEFORE UPDATE OF status ON voucher "
        "WHEN NEW.status='posted' AND ("
        "(SELECT COUNT(*) FROM voucher_line WHERE voucher_id=NEW.id)<2 OR "
        "(SELECT COALESCE(SUM(debit_cents),0) FROM voucher_line WHERE voucher_id=NEW.id)<=0 OR "
        "(SELECT COALESCE(SUM(debit_cents-credit_cents),0) FROM voucher_line WHERE voucher_id=NEW.id)<>0 OR "
        "EXISTS(SELECT 1 FROM voucher_line WHERE voucher_id=NEW.id AND account_code='UNRESOLVED') OR "
        "EXISTS(SELECT 1 FROM period WHERE year=CAST(SUBSTR(NEW.period,1,4) AS INTEGER) "
        "AND month=CAST(SUBSTR(NEW.period,6,2) AS INTEGER) AND closed=1)) "
        "BEGIN SELECT RAISE(ABORT,'voucher cannot be posted'); END"
    )
    conn.execute(
        "CREATE TRIGGER voucher_closed_period_insert BEFORE INSERT ON voucher "
        "WHEN EXISTS(SELECT 1 FROM period WHERE year=CAST(SUBSTR(NEW.period,1,4) AS INTEGER) "
        "AND month=CAST(SUBSTR(NEW.period,6,2) AS INTEGER) AND closed=1) "
        "BEGIN SELECT RAISE(ABORT,'accounting period is closed'); END"
    )
    conn.execute(
        "CREATE TRIGGER voucher_closed_period_update BEFORE UPDATE OF date,period ON voucher "
        "WHEN EXISTS(SELECT 1 FROM period WHERE year=CAST(SUBSTR(NEW.period,1,4) AS INTEGER) "
        "AND month=CAST(SUBSTR(NEW.period,6,2) AS INTEGER) AND closed=1) "
        "BEGIN SELECT RAISE(ABORT,'accounting period is closed'); END"
    )
    conn.execute(
        "CREATE TRIGGER voucher_line_validate_insert BEFORE INSERT ON voucher_line "
        "WHEN NEW.voucher_id IS NULL OR NEW.seq IS NULL OR NEW.seq<1 OR "
        "TRIM(COALESCE(NEW.account_code,''))='' OR NEW.account_code='UNRESOLVED' OR "
        "NOT EXISTS(SELECT 1 FROM account WHERE code=NEW.account_code AND is_leaf=1) OR "
        "NEW.debit_cents<0 OR NEW.credit_cents<0 OR "
        "((NEW.debit_cents>0)=(NEW.credit_cents>0)) "
        "BEGIN SELECT RAISE(ABORT,'invalid voucher line'); END"
    )
    conn.execute(
        "CREATE TRIGGER voucher_line_validate_update BEFORE UPDATE ON voucher_line "
        "WHEN NEW.voucher_id IS NULL OR NEW.seq IS NULL OR NEW.seq<1 OR "
        "TRIM(COALESCE(NEW.account_code,''))='' OR NEW.account_code='UNRESOLVED' OR "
        "NOT EXISTS(SELECT 1 FROM account WHERE code=NEW.account_code AND is_leaf=1) OR "
        "NEW.debit_cents<0 OR NEW.credit_cents<0 OR "
        "((NEW.debit_cents>0)=(NEW.credit_cents>0)) "
        "BEGIN SELECT RAISE(ABORT,'invalid voucher line'); END"
    )
    conn.execute(
        "CREATE TRIGGER expense_item_validate_insert BEFORE INSERT ON expense_item "
        "WHEN TRIM(COALESCE(NEW.account_code,''))='' OR NEW.account_code='UNRESOLVED' OR "
        "NOT EXISTS(SELECT 1 FROM account WHERE code=NEW.account_code AND is_leaf=1) OR "
        "NEW.amount_cents<=0 "
        "BEGIN SELECT RAISE(ABORT,'invalid expense item'); END"
    )
    conn.execute(
        "CREATE TRIGGER expense_item_validate_update BEFORE UPDATE ON expense_item "
        "WHEN TRIM(COALESCE(NEW.account_code,''))='' OR NEW.account_code='UNRESOLVED' OR "
        "NOT EXISTS(SELECT 1 FROM account WHERE code=NEW.account_code AND is_leaf=1) OR "
        "NEW.amount_cents<=0 "
        "BEGIN SELECT RAISE(ABORT,'invalid expense item'); END"
    )


def _migration_5(conn: sqlite3.Connection) -> None:
    """进销存：商品、库存单据、库存余额。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS product ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, sku TEXT, name TEXT NOT NULL, spec TEXT, unit TEXT, "
        "category TEXT, cost_method TEXT DEFAULT 'weighted', note TEXT, created_at TEXT)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_product_sku ON product(sku) "
        "WHERE sku IS NOT NULL AND TRIM(sku)<>''"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS stock_doc ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, doc_no TEXT UNIQUE, doc_type TEXT, date TEXT, period TEXT, "
        "partner_id INTEGER, summary TEXT, status TEXT DEFAULT 'posted', voucher_id INTEGER, "
        "creator TEXT, created_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS stock_doc_line ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, stock_doc_id INTEGER, product_id INTEGER, qty REAL DEFAULT 0, "
        "unit_price_cents INTEGER DEFAULT 0, amount_cents INTEGER DEFAULT 0, "
        "sale_price_cents INTEGER DEFAULT 0, sale_amount_cents INTEGER DEFAULT 0, note TEXT, "
        "FOREIGN KEY(stock_doc_id) REFERENCES stock_doc(id) ON DELETE CASCADE, "
        "FOREIGN KEY(product_id) REFERENCES product(id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS stock_balance ("
        "product_id INTEGER PRIMARY KEY, qty REAL DEFAULT 0, amount_cents INTEGER DEFAULT 0, "
        "avg_cost_cents INTEGER DEFAULT 0, updated_at TEXT, "
        "FOREIGN KEY(product_id) REFERENCES product(id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS stock_doc_sequence ("
        "period TEXT PRIMARY KEY, last_seq INTEGER NOT NULL CHECK(last_seq >= 0))"
    )


def _migration_6(conn: sqlite3.Connection) -> None:
    """补上 expense.instance_code。

    该列一直被 modules/feishu.py 使用，却从未出现在建表或早期迁移中：
    建表写的是 feishu_instance_id（已废弃、生产库全为 NULL），
    _migration_2 的 expense additions 也没有它。结果全新库上
    submit_expense_approval() 一写就 no such column: instance_code。
    这里补偿性 ALTER，_add_column 自带幂等保护，已手工补过列的老库不受影响。
    """
    _add_column(conn, "expense", "instance_code TEXT")


MIGRATIONS = [
    (1, "legacy_baseline", _migration_1),
    (2, "accounting_controls", _migration_2),
    (3, "legacy_data_normalization", _migration_3),
    (4, "constraints_and_indexes", _migration_4),
    (5, "inventory", _migration_5),
    (6, "expense_feishu_instance_code", _migration_6),
]


def _copy_legacy_database_if_needed(target: Path) -> bool:
    if target.exists() or os.environ.get("FINANCE_SYSTEM_DB_PATH"):
        return False
    for candidate in _legacy_candidates():
        try:
            if candidate.resolve() == target.resolve() or not candidate.is_file():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = sqlite3.connect(str(candidate))
            destination = sqlite3.connect(str(target))
            try:
                source.backup(destination)
                destination.commit()
            finally:
                destination.close()
                source.close()
            return True
        except (OSError, sqlite3.Error):
            if target.exists():
                target.unlink(missing_ok=True)
    return False


def init_db(db_path: os.PathLike[str] | str | None = None) -> str:
    """Create/migrate a database. Each migration is independently atomic."""
    global LAST_PRE_MIGRATION_BACKUP
    path = Path(db_path or DB_PATH).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if db_path is None:
        _copy_legacy_database_if_needed(path)
    conn = get_conn(path)
    try:
        current = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if current > SCHEMA_VERSION:
            raise RuntimeError(f"数据库版本 {current} 高于程序支持版本 {SCHEMA_VERSION}")
        user_tables = int(
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
        )
        if current < SCHEMA_VERSION and user_tables:
            quick = [row[0] for row in conn.execute("PRAGMA quick_check")]
            if quick != ["ok"]:
                raise sqlite3.DatabaseError(f"迁移前数据库快速检查失败: {quick}")
            canonical_path = _path_from_app_paths("get_db_path")
            if canonical_path is not None and path == canonical_path:
                backup_dir = _backup_dir()
            else:
                backup_dir = path.parent / "backups"
            stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup_path = backup_dir / f"finance-pre-v{current}-{stamp}.db"
            _backup_connection(conn, backup_path)
            LAST_PRE_MIGRATION_BACKUP = str(backup_path)
        for version, name, migration in MIGRATIONS:
            if version <= current:
                continue
            try:
                conn.execute("BEGIN IMMEDIATE")
                migration(conn)
                # schema_migration exists from v2 onward.
                if version >= 2:
                    conn.execute(
                        "INSERT OR REPLACE INTO schema_migration(version,name,applied_at) VALUES (?,?,?)",
                        (version, name, _dt.datetime.now().isoformat(timespec="seconds")),
                    )
                conn.execute(f"PRAGMA user_version = {version}")
                conn.commit()
                current = version
            except Exception:
                conn.rollback()
                raise
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
    finally:
        conn.close()
    return str(path)


def _backup_dir() -> Path:
    shared = _path_from_app_paths("get_backup_dir")
    if shared:
        return shared
    return Path(DB_PATH).resolve().parent.parent / "backups"


def _backup_connection(source: sqlite3.Connection, target: Path) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    destination = sqlite3.connect(str(target))
    try:
        source.backup(destination)
        destination.commit()
        result = [row[0] for row in destination.execute("PRAGMA quick_check")]
        if result != ["ok"]:
            raise sqlite3.DatabaseError(f"备份快速检查失败: {result}")
    except Exception:
        destination.close()
        target.unlink(missing_ok=True)
        raise
    else:
        destination.close()
    return str(target)


def backup_database(destination: os.PathLike[str] | str | None = None) -> str:
    """Create a consistent SQLite backup and verify it before returning."""
    source_path = Path(DB_PATH).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"数据库不存在: {source_path}")
    if destination is None:
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        target = _backup_dir() / f"finance-{stamp}.db"
    else:
        target = Path(destination).expanduser().resolve()
        if target.exists() and target.is_dir():
            stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            target = target / f"finance-{stamp}.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    source = get_conn(source_path)
    try:
        _backup_connection(source, target)
        verification = sqlite3.connect(str(target))
        try:
            result = verification.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise sqlite3.DatabaseError(f"备份完整性检查失败: {result}")
        finally:
            verification.close()
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        source.close()
    return str(target)


def get_open_data_issues(severity: str | None = None) -> list[dict[str, Any]]:
    conn = get_conn()
    try:
        sql = "SELECT * FROM data_issue WHERE status='open'"
        args: list[Any] = []
        if severity:
            sql += " AND severity=?"
            args.append(severity)
        sql += " ORDER BY CASE severity WHEN 'blocking' THEN 0 WHEN 'error' THEN 1 ELSE 2 END, id"
        return [dict(row) for row in conn.execute(sql, args)]
    finally:
        conn.close()


def has_blocking_data_issues() -> bool:
    conn = get_conn()
    try:
        return bool(
            conn.execute(
                "SELECT 1 FROM data_issue WHERE status='open' AND severity='blocking' LIMIT 1"
            ).fetchone()
        )
    finally:
        conn.close()


def resolve_data_issue(
    issue_id: int,
    account_code: str,
    actor: str,
    reason: str,
) -> tuple[bool, str]:
    """Resolve an invalid historical account only after an explicit human choice."""
    account_code = str(account_code or "").strip()
    actor = str(actor or "").strip()
    reason = str(reason or "").strip()
    if not actor or not reason:
        return False, "处理人和修复原因不能为空"
    if not account_code or account_code == "UNRESOLVED":
        return False, "必须明确选择有效会计科目，不能继续使用待确认科目"
    try:
        with transaction() as conn:
            issue = conn.execute("SELECT * FROM data_issue WHERE id=?", (issue_id,)).fetchone()
            if not issue:
                return False, "数据问题不存在"
            if issue["status"] != "open":
                return False, "数据问题已经处理"
            if issue["issue_type"] != "invalid_account" or issue["entity_type"] not in (
                "voucher_line",
                "expense_item",
            ):
                return False, "该类数据问题不能通过科目确认接口处理"
            account = conn.execute(
                "SELECT code,is_leaf FROM account WHERE code=?", (account_code,)
            ).fetchone()
            if not account or int(account["is_leaf"] or 0) != 1:
                return False, "所选会计科目不存在或不是末级科目"
            now = _dt.datetime.now().isoformat(timespec="seconds")

            def mark_resolved(target_issue_id: int) -> None:
                conn.execute(
                    "UPDATE data_issue SET status='resolved',resolution_value=?,resolution_reason=?,"
                    "resolved_by=?,resolved_at=? WHERE id=? AND status='open'",
                    (account_code, reason, actor, now, target_issue_id),
                )

            synchronized: list[dict[str, Any]] = []
            if issue["entity_type"] == "voucher_line":
                line_id = int(issue["entity_id"])
                line = conn.execute(
                    "SELECT vl.*,v.vno FROM voucher_line vl JOIN voucher v ON v.id=vl.voucher_id WHERE vl.id=?",
                    (line_id,),
                ).fetchone()
                if not line:
                    return False, "关联凭证分录不存在"
                with _posted_mutation_allowed():
                    conn.execute("UPDATE voucher_line SET account_code=? WHERE id=?", (account_code, line_id))
                amount_cents = max(int(line["debit_cents"] or 0), int(line["credit_cents"] or 0))
                linked_items = conn.execute(
                    "SELECT ei.id FROM expense e JOIN expense_item ei ON ei.expense_id=e.id "
                    "WHERE e.voucher_id=? AND ei.account_code='UNRESOLVED' AND ei.amount_cents=?",
                    (line["voucher_id"], amount_cents),
                ).fetchall()
                if len(linked_items) == 1:
                    item_id = int(linked_items[0]["id"])
                    conn.execute("UPDATE expense_item SET account_code=? WHERE id=?", (account_code, item_id))
                    linked_issue = conn.execute(
                        "SELECT id FROM data_issue WHERE issue_type='invalid_account' "
                        "AND entity_type='expense_item' AND entity_id=? AND status='open'",
                        (item_id,),
                    ).fetchone()
                    if linked_issue:
                        mark_resolved(int(linked_issue["id"]))
                    synchronized.append({"entity_type": "expense_item", "entity_id": item_id})
                    _audit(
                        conn,
                        "expense_item",
                        item_id,
                        "data_issue_resolved_from_voucher",
                        actor,
                        {"voucher_line_id": line_id, "account_code": account_code, "reason": reason},
                    )
                entity_id = line_id
                audit_details = {
                    "voucher_id": line["voucher_id"],
                    "vno": line["vno"],
                    "account_code": account_code,
                    "reason": reason,
                    "synchronized": synchronized,
                }
            else:
                item_id = int(issue["entity_id"])
                item = conn.execute(
                    "SELECT ei.*,e.voucher_id,e.no FROM expense_item ei "
                    "JOIN expense e ON e.id=ei.expense_id WHERE ei.id=?",
                    (item_id,),
                ).fetchone()
                if not item:
                    return False, "关联报销明细不存在"
                conn.execute("UPDATE expense_item SET account_code=? WHERE id=?", (account_code, item_id))
                if item["voucher_id"]:
                    linked_lines = conn.execute(
                        "SELECT id FROM voucher_line WHERE voucher_id=? "
                        "AND debit_cents=? AND credit_cents=0 "
                        "AND account_code IN ('UNRESOLVED','5601','5602','5603')",
                        (item["voucher_id"], int(item["amount_cents"] or 0)),
                    ).fetchall()
                    if len(linked_lines) == 1:
                        line_id = int(linked_lines[0]["id"])
                        with _posted_mutation_allowed():
                            conn.execute(
                                "UPDATE voucher_line SET account_code=? WHERE id=?", (account_code, line_id)
                            )
                        linked_issue = conn.execute(
                            "SELECT id FROM data_issue WHERE issue_type='invalid_account' "
                            "AND entity_type='voucher_line' AND entity_id=? AND status='open'",
                            (line_id,),
                        ).fetchone()
                        if linked_issue:
                            mark_resolved(int(linked_issue["id"]))
                        synchronized.append({"entity_type": "voucher_line", "entity_id": line_id})
                        _audit(
                            conn,
                            "voucher_line",
                            line_id,
                            "data_issue_resolved_from_expense",
                            actor,
                            {"expense_item_id": item_id, "account_code": account_code, "reason": reason},
                        )
                entity_id = item_id
                audit_details = {
                    "expense_id": item["expense_id"],
                    "expense_no": item["no"],
                    "voucher_id": item["voucher_id"],
                    "account_code": account_code,
                    "reason": reason,
                    "synchronized": synchronized,
                }
            mark_resolved(issue_id)
            _audit(
                conn,
                issue["entity_type"],
                entity_id,
                "data_issue_resolved",
                actor,
                {"issue_id": issue_id, **audit_details},
            )
        return True, "历史科目已确认并留下审计记录"
    except (sqlite3.Error, ValueError) as exc:
        return False, f"处理失败: {exc}"


def run_integrity_checks() -> dict[str, Any]:
    """Return machine-readable database, accounting, and source-link checks."""
    conn = get_conn()
    try:
        integrity_rows = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        foreign_keys = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
        invalid_accounts = [
            dict(row)
            for row in conn.execute(
                "SELECT vl.id,vl.voucher_id,vl.account_code FROM voucher_line vl "
                "LEFT JOIN account a ON a.code=vl.account_code "
                "WHERE a.code IS NULL OR vl.account_code='UNRESOLVED'"
            )
        ]
        unbalanced = [
            dict(row)
            for row in conn.execute(
                "SELECT v.id,v.vno,v.status,COUNT(vl.id) line_count,"
                "COALESCE(SUM(vl.debit_cents),0) debit_cents,"
                "COALESCE(SUM(vl.credit_cents),0) credit_cents "
                "FROM voucher v LEFT JOIN voucher_line vl ON vl.voucher_id=v.id "
                "GROUP BY v.id HAVING COUNT(vl.id)<2 "
                "OR COALESCE(SUM(vl.debit_cents),0)<>COALESCE(SUM(vl.credit_cents),0) "
                "OR COALESCE(SUM(vl.debit_cents),0)<=0"
            )
        ]
        source_link_issues: list[dict[str, Any]] = []
        for table in ("cash_doc", "expense", "invoice_doc"):
            source_link_issues.extend(
                {"table": table, **dict(row)}
                for row in conn.execute(
                    f"SELECT s.id,s.voucher_id FROM {table} s LEFT JOIN voucher v ON v.id=s.voucher_id "
                    "WHERE s.voucher_id IS NOT NULL AND v.id IS NULL"
                )
            )
        blocking_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM data_issue WHERE status='open' AND severity='blocking'"
            ).fetchone()[0]
        )
        result = {
            "integrity": integrity_rows,
            "foreign_key_errors": foreign_keys,
            "invalid_accounts": invalid_accounts,
            "unbalanced_vouchers": unbalanced,
            "source_link_issues": source_link_issues,
            "blocking_data_issue_count": blocking_count,
        }
        result["ok"] = (
            integrity_rows == ["ok"]
            and not foreign_keys
            and not invalid_accounts
            and not unbalanced
            and not source_link_issues
            and blocking_count == 0
        )
        return result
    finally:
        conn.close()


if __name__ == "__main__":
    print("DB initialized at", init_db())
    print(json.dumps(run_integrity_checks(), ensure_ascii=False, indent=2))
