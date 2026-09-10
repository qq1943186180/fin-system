# -*- coding: utf-8 -*-
"""账簿页默认期间 = 最近一个有「已记账」凭证的期间。

锁住曾经的坑：默认用当月，而凭证都在历史月份 → 序时簿一打开就是空的。
"""
from __future__ import annotations

import importlib

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


def _post(ledger, date, summary, lines):
    ok, message, voucher_id = ledger.create_voucher(
        date, summary, lines, creator="制单员", source="manual",
        idempotency_key="test:" + summary, submit=True,
    )
    assert ok, message
    ok, message = ledger.audit_voucher(voucher_id, "审核员")
    assert ok, message
    ok, message = ledger.post_voucher(voucher_id, "记账员")
    assert ok, message
    return voucher_id


def test_latest_posted_period(finance_modules):
    db, ledger, reports = finance_modules
    from gui import main as gm

    # 空库：无已记账凭证 → None
    assert not gm._latest_posted_period()

    _post(ledger, "2026-07-10", "七月", [
        {"account_code": "1002", "debit_cents": 100, "credit_cents": 0},
        {"account_code": "3001", "debit_cents": 0, "credit_cents": 100},
    ])
    assert gm._latest_posted_period() == "2026-07"

    _post(ledger, "2026-08-05", "八月", [
        {"account_code": "1002", "debit_cents": 200, "credit_cents": 0},
        {"account_code": "5001", "debit_cents": 0, "credit_cents": 200},
    ])
    # 取「最近」的已记账期间
    assert gm._latest_posted_period() == "2026-08"
