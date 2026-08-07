from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest


@pytest.fixture()
def finance_modules(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_SYSTEM_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FINANCE_SYSTEM_TEST_MODE", "1")

    import app_paths
    import db
    from modules import ledger, reports

    importlib.reload(app_paths)
    importlib.reload(db)
    importlib.reload(ledger)
    importlib.reload(reports)
    db.configure_db_path(tmp_path / "finance.db")
    db.init_db(tmp_path / "finance.db")
    yield db, ledger, reports


def _post(ledger, summary, lines, source="manual", key=None):
    ok, message, voucher_id = ledger.create_voucher(
        "2026-07-10",
        summary,
        lines,
        creator="制单员",
        source=source,
        idempotency_key=key or "test:" + summary,
        submit=True,
    )
    assert ok, message
    ok, message = ledger.audit_voucher(voucher_id, "审核员")
    assert ok, message
    ok, message = ledger.post_voucher(voucher_id, "记账员")
    assert ok, message
    return voucher_id


def test_gold_book_reconciles_all_statements(finance_modules):
    db, ledger, reports = finance_modules
    _post(
        ledger,
        "资本投入",
        [
            {"account_code": "1002", "debit_cents": 20_000_000, "credit_cents": 0},
            {"account_code": "3001", "debit_cents": 0, "credit_cents": 20_000_000},
        ],
    )
    _post(
        ledger,
        "销售收款",
        [
            {"account_code": "1002", "debit_cents": 5_000_000, "credit_cents": 0},
            {"account_code": "5001", "debit_cents": 0, "credit_cents": 5_000_000},
        ],
    )
    _post(
        ledger,
        "日常费用",
        [
            {"account_code": "5602", "debit_cents": 1_000_000, "credit_cents": 0},
            {"account_code": "1002", "debit_cents": 0, "credit_cents": 1_000_000},
        ],
    )
    _post(
        ledger,
        "购置固定资产",
        [
            {"account_code": "1601", "debit_cents": 2_000_000, "credit_cents": 0},
            {"account_code": "1002", "debit_cents": 0, "credit_cents": 2_000_000},
        ],
    )
    _post(
        ledger,
        "计提折旧",
        [
            {"account_code": "5602", "debit_cents": 100_000, "credit_cents": 0},
            {"account_code": "1602", "debit_cents": 0, "credit_cents": 100_000},
        ],
    )

    balance = reports.balance_sheet("2026-07")
    income = reports.income_statement("2026-07")
    cash = reports.cash_flow_statement("2026-07")

    assert balance["balanced"] is True
    assert balance["total_assets_cents"] == 23_900_000
    assert balance["total_liabilities_cents"] == 0
    assert balance["total_equity_cents"] == 23_900_000
    assert income["total_revenue_cents"] == 5_000_000
    assert income["total_expense_cents"] == 1_100_000
    assert income["net_profit_cents"] == 3_900_000
    assert cash["operating_net_cents"] == 4_000_000
    assert cash["investing_cents"] == -2_000_000
    assert cash["financing_cents"] == 20_000_000
    assert cash["net_cash_cents"] == 22_000_000
    assert cash["ending_cash_cents"] == 22_000_000
    assert cash["reconciled"] is True

    balances = reports.account_balance_table("2026-07")
    balance_by_code = {row["code"]: row for row in balances["rows"]}
    assert balance_by_code["1002"]["ending_debit_cents"] == 22_000_000
    assert balance_by_code["1002"]["period_debit_cents"] == 25_000_000
    assert balance_by_code["1002"]["period_credit_cents"] == 3_000_000

    journal = reports.chronological_ledger("2026-07")
    assert journal["balanced"] is True
    assert journal["total_debit_cents"] == journal["total_credit_cents"]
    assert len(journal["rows"]) == 10
    assert journal["rows"][0]["vno"] < journal["rows"][-1]["vno"]


def test_income_reversal_is_not_silently_dropped(finance_modules):
    _db, ledger, reports = finance_modules
    _post(
        ledger,
        "销售",
        [
            {"account_code": "1002", "debit_cents": 1_000_000, "credit_cents": 0},
            {"account_code": "5001", "debit_cents": 0, "credit_cents": 1_000_000},
        ],
    )
    _post(
        ledger,
        "销售冲回",
        [
            {"account_code": "5001", "debit_cents": 200_000, "credit_cents": 0},
            {"account_code": "1002", "debit_cents": 0, "credit_cents": 200_000},
        ],
    )
    statement = reports.income_statement("2026-07")
    assert statement["total_revenue_cents"] == 800_000


def test_blocking_issue_stops_formal_reports(finance_modules):
    db, _ledger, reports = finance_modules
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO data_issue(issue_type,severity,entity_type,entity_id,field_name,status,created_at) "
            "VALUES ('test_issue','blocking','voucher_line',999,'account_code','open','2026-07-10')"
        )
    with pytest.raises(reports.ReportBlockedError):
        reports.balance_sheet("2026-07")


def test_invoice_normalization_uses_chinese_date_and_total(monkeypatch, tmp_path):
    monkeypatch.setenv("FINANCE_SYSTEM_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FINANCE_SYSTEM_TEST_MODE", "1")
    from modules.invoice_gui import normalize_invoice

    invoice = normalize_invoice(
        {
            "发票号码": "12345678",
            "开票日期": "2026年7月9日",
            "销售方税号": "SELLER123",
            "金额合计": "100.00",
            "税额合计": "6.00",
            "价税合计": "106.00",
        }
    )
    assert invoice["date"] == "2026-07-09"
    assert invoice["net_amount_cents"] == 10_000
    assert invoice["tax_cents"] == 600
    assert invoice["total_cents"] == 10_600


def test_feishu_uuid_is_stable_and_unique():
    from modules.feishu import _approval_uuid

    assert _approval_uuid(7) == _approval_uuid(7)
    assert _approval_uuid(7) != _approval_uuid(8)
