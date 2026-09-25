# -*- coding: utf-8 -*-
"""飞书审批同步的两个回归：状态字面量错配 + instance_code 列缺失。

背景（2026-09-26 核查，起因：哥哥在飞书里测了审批，本地库查不到）：

**Bug A —— ``sync_approval_status()`` 的行取条件用的是中文状态。**
``modules/feishu.py`` 里原本写的是::

    WHERE status IN ('待审批','pending_review') AND instance_code 非空

而 ``modules/expense.py::create_expense()`` 落库写的是英文 ``'pending_approval'``，
``EXPENSE_STATUSES`` 也全是英文 —— 两个集合**交集为空**。
于是「新建单 → 提交飞书审批 → 飞书里批准 → 同步飞书状态」这条正路
**永远同步不到**：``sync_approval_status()`` 返回 SUCCESS、changed 为空，
单子一直卡在 ``pending_approval``，且**不报任何错**（静默失败）。

**Bug B —— ``expense.instance_code`` 列从未出现在建表/迁移代码里。**
``db/__init__.py`` 建表写的是 ``feishu_instance_id``（已废弃），
``_migration_1..5`` 也没 ALTER 出 ``instance_code``，但 ``modules/feishu.py``
全程在用它。结果**全新安装的库上 ``submit_expense_approval()`` 第一步就**
``no such column: instance_code``。生产库只是碰巧被手工补过这一列才没炸。
本文件锁住修好后的契约。
"""

from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def fin_env(tmp_path, monkeypatch):
    """隔离的 FINANCE_SYSTEM_HOME + 重新加载的 db / expense / feishu。"""

    monkeypatch.setenv("FINANCE_SYSTEM_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FINANCE_SYSTEM_TEST_MODE", "1")

    import app_paths

    importlib.reload(app_paths)

    import db

    importlib.reload(db)
    db.configure_db_path(tmp_path / "finance.db")
    db.init_db(tmp_path / "finance.db")

    from modules import expense as expense_mod
    from modules import feishu

    importlib.reload(expense_mod)
    importlib.reload(feishu)

    # 不碰真实 DPAPI 凭据库；token 直接给假值。
    monkeypatch.setattr(feishu, "get_secret", lambda _name: "test-secret")
    monkeypatch.setattr(feishu, "_legacy_config", lambda: {})

    return db, expense_mod, feishu


def _make_pending_expense(db, expense_mod, *, instance_code: str | None = None) -> int:
    """建一个处于 ``pending_approval`` 的报销单，可选地塞上 instance_code。"""

    emp_id = expense_mod.add_employee("测试员工")
    ok, message, expense_id = expense_mod.create_expense(
        emp_id=emp_id,
        date_str="2026-09-26",
        items=[
            {"account_code": "5602", "summary": "测试明细", "amount_cents": 100, "invoice_ref": ""}
        ],
        summary="回归用单",
    )
    assert ok, message

    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT status FROM expense WHERE id=?", (expense_id,)
        ).fetchone()
        assert row["status"] == "pending_approval", (
            "前置条件：create_expense() 应当落库英文状态 pending_approval"
        )
        if instance_code is not None:
            conn.execute(
                "UPDATE expense SET instance_code=? WHERE id=?",
                (instance_code, expense_id),
            )
            conn.commit()
    finally:
        conn.close()
    return expense_id


def _patch_feishu_ok(feishu, monkeypatch, remote_status: str) -> None:
    """把 token 与实例状态查询都打桩，隔离网络。"""

    monkeypatch.setattr(
        feishu,
        "get_token",
        lambda: feishu.IntegrationResult(
            feishu.IntegrationStatus.SUCCESS, "ok", "fake-token"
        ),
    )
    monkeypatch.setattr(
        feishu,
        "_get_instance_status",
        lambda instance_code, token: feishu.IntegrationResult(
            feishu.IntegrationStatus.SUCCESS, "ok", remote_status
        ),
    )


def _status_of(db, expense_id: int) -> str:
    conn = db.get_conn()
    try:
        return conn.execute(
            "SELECT status FROM expense WHERE id=?", (expense_id,)
        ).fetchone()["status"]
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Bug A：状态字面量
# --------------------------------------------------------------------------


def test_sync_picks_up_pending_approval_rows(fin_env, monkeypatch):
    """核心回归：pending_approval + instance_code 非空 的单必须被扫到。"""

    db, expense_mod, feishu = fin_env
    expense_id = _make_pending_expense(db, expense_mod, instance_code="INST-APPROVED-001")
    _patch_feishu_ok(feishu, monkeypatch, "APPROVED")

    result = feishu.sync_approval_status()

    assert result.ok, result.message
    assert result.data == [{"expense_id": expense_id, "status": "approved"}], (
        "sync_approval_status() 必须扫到 status='pending_approval' 且 instance_code 非空的行；"
        "否则「提交飞书审批 → 飞书批准 → 同步飞书状态」这条正路永远同步不到，且不报错。"
    )
    assert _status_of(db, expense_id) == "approved"


def test_sync_marks_rejected(fin_env, monkeypatch):
    db, expense_mod, feishu = fin_env
    expense_id = _make_pending_expense(db, expense_mod, instance_code="INST-REJECTED-001")
    _patch_feishu_ok(feishu, monkeypatch, "REJECTED")

    result = feishu.sync_approval_status()

    assert result.ok, result.message
    assert [c["status"] for c in result.data] == ["rejected"]
    assert _status_of(db, expense_id) == "rejected"


def test_sync_ignores_rows_without_instance_code(fin_env, monkeypatch):
    """没有 instance_code 的单（还没提交飞书）不能被同步改动。"""

    db, expense_mod, feishu = fin_env
    expense_id = _make_pending_expense(db, expense_mod, instance_code=None)
    _patch_feishu_ok(feishu, monkeypatch, "APPROVED")

    result = feishu.sync_approval_status()

    assert result.ok, result.message
    assert result.data == []
    assert _status_of(db, expense_id) == "pending_approval"


def test_sync_ignores_already_approved_rows(fin_env, monkeypatch):
    """已 approved 的单不重复处理（幂等）。"""

    db, expense_mod, feishu = fin_env
    expense_id = _make_pending_expense(db, expense_mod, instance_code="INST-ALREADY-001")
    ok, message, *_ = expense_mod.approve(expense_id, "本地审核")
    assert ok, message
    _patch_feishu_ok(feishu, monkeypatch, "APPROVED")

    result = feishu.sync_approval_status()

    assert result.ok, result.message
    assert result.data == []


def test_source_uses_english_status_literals():
    """源码守卫：防止中文状态字面量被加回 SQL 过滤条件。"""

    source = (ROOT / "modules" / "feishu.py").read_text(encoding="utf-8")

    assert "'待审批'" not in source, (
        "modules/feishu.py 不得用中文 '待审批' 过滤 expense.status —— "
        "库里存的是英文 pending_approval，中文条件命中数恒为 0（静默失败）。"
    )
    assert "'pending_approval'" in source


# --------------------------------------------------------------------------
# Bug B：instance_code 列
# --------------------------------------------------------------------------


def test_fresh_database_has_instance_code_column(tmp_path, monkeypatch):
    """全新安装的库必须有 expense.instance_code。"""

    monkeypatch.setenv("FINANCE_SYSTEM_HOME", str(tmp_path / "home2"))
    monkeypatch.setenv("FINANCE_SYSTEM_TEST_MODE", "1")

    import app_paths

    importlib.reload(app_paths)

    import db

    importlib.reload(db)
    db.configure_db_path(tmp_path / "fresh.db")
    db.init_db(tmp_path / "fresh.db")

    conn = db.get_conn()
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(expense)")]
    finally:
        conn.close()

    assert "instance_code" in columns, (
        "全新库的 expense 表缺少 instance_code —— modules/feishu.py 全程在用它，"
        "缺列会让「提交飞书审批」第一步就 no such column。"
    )


def test_submit_path_can_write_instance_code_on_fresh_db(tmp_path, monkeypatch):
    """照 submit_expense_approval() 的写法更新 instance_code，新库不得报错。"""

    monkeypatch.setenv("FINANCE_SYSTEM_HOME", str(tmp_path / "home3"))
    monkeypatch.setenv("FINANCE_SYSTEM_TEST_MODE", "1")

    import app_paths

    importlib.reload(app_paths)

    import db

    importlib.reload(db)
    db.configure_db_path(tmp_path / "fresh2.db")
    db.init_db(tmp_path / "fresh2.db")

    conn = db.get_conn()
    try:
        conn.execute("INSERT INTO expense(no, status) VALUES('TEST-0001','pending_approval')")
        conn.execute("UPDATE expense SET instance_code=? WHERE no='TEST-0001'", ("X-1",))
        conn.commit()
        stored = conn.execute(
            "SELECT instance_code FROM expense WHERE no='TEST-0001'"
        ).fetchone()[0]
    except sqlite3.OperationalError as exc:  # pragma: no cover - 修复前会走到这里
        pytest.fail(f"新库无法写 instance_code（回归）：{exc}")
    finally:
        conn.close()

    assert stored == "X-1"


def test_migration_6_is_registered():
    """迁移表必须包含补列的 v6 条目，且 SCHEMA_VERSION 已跟进。"""

    import db as db_module

    versions = [version for version, _name, _fn in db_module.MIGRATIONS]
    assert 6 in versions, "缺少补 instance_code 列的 migration 6"
    assert db_module.SCHEMA_VERSION >= 6
