# -*- coding: utf-8 -*-
"""「从飞书拉取报销单」的回归测试。

背景（2026-09-26）：原先报销流程是「财务在桌面端建单 → 推飞书 → 手工点同步」，
员工根本用不了。改成**员工在手机飞书自助提单、财务系统反向拉取**后，
``modules/feishu_pull.py`` 负责「拉」的那一半。

本文件锁住的核心契约：

1. **幂等**：同一个飞书实例重复拉取不会重复建单（靠 ``expense.instance_code`` 唯一键）。
2. **字段解析**：飞书 form 的 fieldList 是「行 × 列」二维数组，要能正确取出
   内容/日期/金额，且金额要从字符串/数字/带千分位文本统一转成分。
3. **员工自动建档**：飞书 user_id 换不到姓名时退回 user_id 本身，不阻断拉取。
4. **状态映射**：飞书 APPROVED → 本地 approved（省掉再点一次同步）。
5. **无明细不建单**：拉不到明细的单要进 skipped 让人看见，不能静默丢掉。
6. **dry_run 不写库**。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


WIDGETS = {
    "expense_type": "w_type",
    "reason": "w_reason",
    "detail": "w_detail",
    "detail_summary": "w_content",
    "detail_date": "w_date",
    "detail_amount": "w_amount",
}


def _form(type_text: str, reason: str, rows: list[tuple[str, str, object]]) -> list[dict]:
    """构造一个和飞书实例详情同构的 form。"""

    return [
        {"id": "w_type", "name": "报销类型", "type": "radioV2", "value": type_text,
         "option": {"key": "k", "text": type_text}},
        {"id": "w_reason", "name": "报销事由", "type": "textarea", "value": reason},
        {
            "id": "w_detail",
            "name": "费用明细",
            "type": "fieldList",
            "value": [
                [
                    {"id": "w_content", "name": "内容", "type": "input", "value": content},
                    {"id": "w_date", "name": "日期", "type": "date",
                     "value": date + "T00:00:00+08:00", "timezoneOffset": -480},
                    {"id": "w_amount", "name": "金额", "type": "amount", "value": amount},
                ]
                for content, date, amount in rows
            ],
        },
    ]


@pytest.fixture()
def pull_env(tmp_path, monkeypatch):
    """隔离的 FINANCE_SYSTEM_HOME + 重新加载的 db / expense / feishu / feishu_pull。"""

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
    from modules import feishu_pull

    importlib.reload(expense_mod)
    importlib.reload(feishu)
    importlib.reload(feishu_pull)

    monkeypatch.setattr(feishu, "get_secret", lambda _name: "test-secret")
    monkeypatch.setattr(feishu, "_legacy_config", lambda: {})
    monkeypatch.setattr(
        feishu,
        "get_token",
        lambda: feishu.IntegrationResult(feishu.IntegrationStatus.SUCCESS, "ok", "fake-token"),
    )
    # 让 _cfg() 带上 widgets 与科目映射
    base_cfg = {"approval_code": "CODE-1", "widgets": dict(WIDGETS)}
    monkeypatch.setattr(feishu, "_cfg", lambda: dict(base_cfg))

    return db, expense_mod, feishu, feishu_pull


def _stub_feishu(monkeypatch, feishu, instances: dict):
    """把飞书的两个读接口打桩。

    ``instances``: {instance_code: {"status":..,"form":..,"user_id":..}}
    """

    def fake_request(method, path, *args, **kwargs):
        if path.startswith("/approval/v4/instances?"):
            return feishu.IntegrationResult(
                feishu.IntegrationStatus.SUCCESS, "ok", {"data": {"instance_code_list": list(instances)}}
            )
        if path.startswith("/approval/v4/instances/"):
            code = path.split("/approval/v4/instances/")[1].split("?")[0]
            from urllib.parse import unquote

            code = unquote(code)
            detail = instances.get(code)
            if detail is None:
                return feishu.IntegrationResult(
                    feishu.IntegrationStatus.BUSINESS_REJECTED, "not found"
                )
            return feishu.IntegrationResult(
                feishu.IntegrationStatus.SUCCESS, "ok", {"data": detail}
            )
        if path.startswith("/contact/v3/users/"):
            return feishu.IntegrationResult(
                feishu.IntegrationStatus.SUCCESS, "ok", {"data": {"user": {"name": "张三"}}}
            )
        raise AssertionError("未打桩的飞书请求: %s" % path)

    monkeypatch.setattr(feishu, "_request", fake_request)


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------


def test_parse_form_extracts_all_fields(pull_env):
    db, expense_mod, feishu, feishu_pull = pull_env
    parsed = feishu_pull._parse_form(
        _form("通讯费", "9月话费", [("话费", "2026-09-25", "128.50")])
    )

    assert parsed["type_text"] == "通讯费"
    assert parsed["reason"] == "9月话费"
    assert len(parsed["items"]) == 1
    assert parsed["items"][0]["summary"] == "话费"
    assert parsed["items"][0]["date"] == "2026-09-25"
    assert parsed["items"][0]["amount_cents"] == 12850


def test_parse_form_accepts_json_string(pull_env):
    """实例详情里 form 可能是 JSON 字符串。"""

    import json

    db, expense_mod, feishu, feishu_pull = pull_env
    parsed = feishu_pull._parse_form(
        json.dumps(_form("差旅费", "出差", [("高铁", "2026-09-20", "553.00")]), ensure_ascii=False)
    )

    assert parsed["type_text"] == "差旅费"
    assert parsed["items"][0]["amount_cents"] == 55300


def test_to_cents_handles_formats(pull_env):
    db, expense_mod, feishu, feishu_pull = pull_env

    assert feishu_pull._to_cents("128.50") == 12850
    assert feishu_pull._to_cents(128.5) == 12850
    assert feishu_pull._to_cents("1,234.56") == 123456
    assert feishu_pull._to_cents("¥99.90") == 9990
    assert feishu_pull._to_cents("￥0.10") == 10
    assert feishu_pull._to_cents(None) == 0
    assert feishu_pull._to_cents("abc") == 0


def test_parse_detail_rows_skips_zero_amount(pull_env):
    """金额为 0 或解析不出来的行不算明细。"""

    db, expense_mod, feishu, feishu_pull = pull_env
    parsed = feishu_pull._parse_form(
        _form("其他", "杂项", [("有金额", "2026-09-01", "10.00"), ("无金额", "2026-09-01", "abc")])
    )

    assert len(parsed["items"]) == 1
    assert parsed["items"][0]["summary"] == "有金额"


# --------------------------------------------------------------------------
# 建单
# --------------------------------------------------------------------------


def test_fetch_creates_local_expense(pull_env, monkeypatch):
    db, expense_mod, feishu, feishu_pull = pull_env
    _stub_feishu(
        monkeypatch,
        feishu,
        {
            "INST-1": {
                "status": "APPROVED",
                "user_id": "u_zhangsan",
                "form": _form("通讯费", "9月话费", [("话费", "2026-09-25", "128.50")]),
            }
        },
    )

    result = feishu_pull.fetch_feishu_expenses(days=30)

    assert result.ok, result.message
    created = result.data["created"]
    assert len(created) == 1
    assert created[0]["amount_cents"] == 12850
    assert created[0]["employee"] == "张三"

    conn = db.get_conn()
    try:
        row = conn.execute("SELECT * FROM expense WHERE instance_code='INST-1'").fetchone()
        assert row is not None
        assert row["status"] == "approved", "飞书已通过的单，拉回来应直接是 approved"
        assert row["amount_cents"] == 12850
        assert row["submitted_by"] == "飞书审批"
        items = conn.execute(
            "SELECT * FROM expense_item WHERE expense_id=?", (row["id"],)
        ).fetchall()
        assert len(items) == 1
        assert items[0]["account_code"] == "5602", "通讯费默认映射到管理费用"
    finally:
        conn.close()


def test_fetch_is_idempotent(pull_env, monkeypatch):
    """核心契约：重复拉取不重复建单。"""

    db, expense_mod, feishu, feishu_pull = pull_env
    _stub_feishu(
        monkeypatch,
        feishu,
        {
            "INST-DUP": {
                "status": "APPROVED",
                "user_id": "u1",
                "form": _form("通讯费", "重复测试", [("话费", "2026-09-25", "50.00")]),
            }
        },
    )

    first = feishu_pull.fetch_feishu_expenses(days=30)
    second = feishu_pull.fetch_feishu_expenses(days=30)

    assert len(first.data["created"]) == 1
    assert len(second.data["created"]) == 0
    assert second.data["skipped"][0]["reason"] == "already_imported"

    conn = db.get_conn()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM expense WHERE instance_code='INST-DUP'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1, "重复拉取建出了多张单"


def test_fetch_maps_pending_status(pull_env, monkeypatch):
    """飞书还在审批中的单，本地落 pending_approval。"""

    db, expense_mod, feishu, feishu_pull = pull_env
    _stub_feishu(
        monkeypatch,
        feishu,
        {
            "INST-P": {
                "status": "PENDING",
                "user_id": "u1",
                "form": _form("差旅费", "待批", [("高铁", "2026-09-20", "100.00")]),
            }
        },
    )

    feishu_pull.fetch_feishu_expenses(days=30)

    conn = db.get_conn()
    try:
        row = conn.execute("SELECT status,approved_at FROM expense WHERE instance_code='INST-P'").fetchone()
    finally:
        conn.close()
    assert row["status"] == "pending_approval"
    assert not row["approved_at"]


def test_fetch_skips_instance_without_detail_rows(pull_env, monkeypatch):
    """拉不到明细的单要进 skipped，不能静默丢掉、也不能建出 0 元单。"""

    db, expense_mod, feishu, feishu_pull = pull_env
    _stub_feishu(
        monkeypatch,
        feishu,
        {
            "INST-EMPTY": {
                "status": "APPROVED",
                "user_id": "u1",
                "form": _form("其他", "没有明细", []),
            }
        },
    )

    result = feishu_pull.fetch_feishu_expenses(days=30)

    assert result.data["created"] == []
    assert result.data["skipped"][0]["reason"] == "no_detail_rows"

    conn = db.get_conn()
    try:
        count = conn.execute("SELECT COUNT(*) FROM expense").fetchone()[0]
    finally:
        conn.close()
    assert count == 0, "没有明细却建了单"


def test_dry_run_does_not_write(pull_env, monkeypatch):
    db, expense_mod, feishu, feishu_pull = pull_env
    _stub_feishu(
        monkeypatch,
        feishu,
        {
            "INST-DRY": {
                "status": "APPROVED",
                "user_id": "u1",
                "form": _form("通讯费", "预览", [("话费", "2026-09-25", "10.00")]),
            }
        },
    )

    result = feishu_pull.fetch_feishu_expenses(days=30, dry_run=True)

    assert len(result.data["created"]) == 1
    assert result.data["created"][0]["dry_run"] is True

    conn = db.get_conn()
    try:
        count = conn.execute("SELECT COUNT(*) FROM expense").fetchone()[0]
    finally:
        conn.close()
    assert count == 0, "dry_run 不该写库"


def test_fetch_rejects_bad_days(pull_env):
    db, expense_mod, feishu, feishu_pull = pull_env

    assert not feishu_pull.fetch_feishu_expenses(days=0).ok
    assert not feishu_pull.fetch_feishu_expenses(days=-1).ok


def test_employee_created_when_unknown(pull_env, monkeypatch):
    """飞书里出现新员工时自动建档。"""

    db, expense_mod, feishu, feishu_pull = pull_env

    def fake_request(method, path, *args, **kwargs):
        if path.startswith("/approval/v4/instances?"):
            return feishu.IntegrationResult(
                feishu.IntegrationStatus.SUCCESS, "ok", {"data": {"instance_code_list": ["I-NEW"]}}
            )
        if path.startswith("/approval/v4/instances/"):
            return feishu.IntegrationResult(
                feishu.IntegrationStatus.SUCCESS,
                "ok",
                {"data": {"status": "APPROVED", "user_id": "u_new",
                          "form": _form("通讯费", "新人报销", [("话费", "2026-09-25", "20.00")])}},
            )
        if path.startswith("/contact/v3/users/"):
            return feishu.IntegrationResult(
                feishu.IntegrationStatus.SUCCESS, "ok", {"data": {"user": {"name": "李四"}}}
            )
        raise AssertionError(path)

    monkeypatch.setattr(feishu, "_request", fake_request)

    feishu_pull.fetch_feishu_expenses(days=30)

    conn = db.get_conn()
    try:
        names = [r["name"] for r in conn.execute("SELECT name FROM employee")]
        emp = conn.execute(
            "SELECT e.name FROM expense x JOIN employee e ON e.id=x.employee_id "
            "WHERE x.instance_code='I-NEW'"
        ).fetchone()
    finally:
        conn.close()
    assert "李四" in names
    assert emp["name"] == "李四"


def test_employee_name_falls_back_to_user_id(pull_env, monkeypatch):
    """通讯录查不到姓名时退回 user_id，不阻断拉取。"""

    db, expense_mod, feishu, feishu_pull = pull_env

    def fake_request(method, path, *args, **kwargs):
        if path.startswith("/approval/v4/instances?"):
            return feishu.IntegrationResult(
                feishu.IntegrationStatus.SUCCESS, "ok", {"data": {"instance_code_list": ["I-FB"]}}
            )
        if path.startswith("/approval/v4/instances/"):
            return feishu.IntegrationResult(
                feishu.IntegrationStatus.SUCCESS,
                "ok",
                {"data": {"status": "APPROVED", "user_id": "u_unknown",
                          "form": _form("其他", "兜底", [("杂项", "2026-09-25", "5.00")])}},
            )
        if path.startswith("/contact/v3/users/"):
            return feishu.IntegrationResult(
                feishu.IntegrationStatus.BUSINESS_REJECTED, "no permission"
            )
        raise AssertionError(path)

    monkeypatch.setattr(feishu, "_request", fake_request)

    result = feishu_pull.fetch_feishu_expenses(days=30)

    assert len(result.data["created"]) == 1
    assert result.data["created"][0]["employee"] == "u_unknown"


def test_type_account_map_can_be_configured(pull_env, monkeypatch):
    """配置 expense_type_accounts 可以覆盖默认科目映射。"""

    db, expense_mod, feishu, feishu_pull = pull_env
    monkeypatch.setattr(
        feishu,
        "_cfg",
        lambda: {
            "approval_code": "CODE-1",
            "widgets": dict(WIDGETS),
            "expense_type_accounts": {"通讯费": "5601"},
        },
    )

    assert feishu_pull.type_account_map()["通讯费"] == "5601"
    assert feishu_pull.type_account_map()["差旅费"] == "5602", "未配置的仍走默认"


def test_type_account_map_defaults(pull_env):
    db, expense_mod, feishu, feishu_pull = pull_env
    mapping = feishu_pull.type_account_map()

    for key in ("差旅费", "住宿费", "交通费", "招待费", "团建费", "通讯费", "其他"):
        assert key in mapping


def test_source_no_longer_pushes_to_feishu():
    """源码守卫：GUI 不该再提供「提交飞书审批」（流程已改为从飞书拉取）。"""

    source = (ROOT / "gui" / "main.py").read_text(encoding="utf-8")

    assert "submit_expense_approval" not in source, (
        "GUI 不应再调用 submit_expense_approval —— 报销入口已改为员工在飞书自助提单，"
        "保留推送会导致同一个报销两边各建一条。"
    )
    assert "从飞书拉取报销单" in source
    assert "feishu_pull.fetch_feishu_expenses(" in source
