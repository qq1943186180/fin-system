# -*- coding: utf-8 -*-
"""飞书「保存集成设置」必须保留生效配置，而不是把 feishu.json 独有的键抹掉。

背景（2026-09-11 核查，未改生产数据）：

飞书配置分散在三处，``modules/feishu.py::_cfg()`` 依次合并，**``settings.json`` 最后覆盖**：

    安装根 config/feishu.json  →  数据根 %LOCALAPPDATA%\\FinanceSystem\\config\\feishu.json
    →  settings.json 的 feishu 段

而 GUI 的设置页**只写 settings.json**，且历史上在保存时**硬写**
``data["feishu"]["allow_self_approval"] = False``。生产现状是
``initiator_open_id == approver_open_id``（单人公司自审），``validate_config()``
仅在 ``allow_self_approval=true`` 时放行 —— 而那个 ``true`` **只存在于数据根的
feishu.json 里**，settings.json 根本没有这个键。

于是：**点一次「保存集成设置」就把正在工作的飞书配置打坏**
（保存后 ``validate_config()`` 转为 CONFIG_ERROR「发起人与审批人相同」）。
更糟的是 ``save_integrations()`` **先落盘再校验**，用户看到告警时坏值已经写进 settings.json。

本文件锁住修好后的契约：保存必须从 ``effective_config()``（合并后的生效值）出发。
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


INITIATOR = "ou_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
APPROVER_SAME = INITIATOR
APPROVER_OTHER = "ou_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

DEDICATED_KEYS = {
    "app_id": "cli_test_app",
    "approval_code": "APPROVAL-CODE-TEST",
    "initiator_open_id": INITIATOR,
    "approver_open_id": APPROVER_SAME,
    "approval_node_id": "node-test-0001",
    "app_secret_ref": "feishu.app_secret",
}


@pytest.fixture()
def feishu_env(tmp_path, monkeypatch):
    """隔离的 FINANCE_SYSTEM_HOME + 重新加载的 modules.feishu。

    另外把 ``_legacy_config()`` 打成空 dict：它会去读**源码树**里的
    ``C:/Users/li1/fin-system/config/feishu.json``（真实生产值），
    不隔离的话断言会读到别人的配置。
    """

    home = tmp_path / "home"
    monkeypatch.setenv("FINANCE_SYSTEM_HOME", str(home))
    monkeypatch.setenv("FINANCE_SYSTEM_TEST_MODE", "1")

    import app_paths

    importlib.reload(app_paths)

    from modules import feishu

    importlib.reload(feishu)

    monkeypatch.setattr(feishu, "_legacy_config", lambda: {})
    # validate_config() 要求存在 DPAPI 密钥；测试里不碰真实凭据库。
    monkeypatch.setattr(feishu, "get_secret", lambda _name: "test-secret")

    config_dir = Path(app_paths.CONFIG_DIR)
    config_dir.mkdir(parents=True, exist_ok=True)
    return feishu, config_dir


def _write_dedicated(config_dir: Path, **overrides) -> Path:
    payload = dict(DEDICATED_KEYS)
    payload.update(overrides)
    path = config_dir / "feishu.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _write_settings(config_dir: Path, feishu_section=None, **extra) -> Path:
    payload: dict = dict(extra)
    if feishu_section is not None:
        payload["feishu"] = feishu_section
    path = config_dir / "settings.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# 合并顺序：settings.json 赢，但 feishu.json 独有的键不能被抹掉
# --------------------------------------------------------------------------


def test_effective_config_exposes_keys_only_present_in_feishu_json(feishu_env):
    feishu, config_dir = feishu_env
    _write_dedicated(config_dir, allow_self_approval=True)
    _write_settings(config_dir, feishu_section={"app_id": "cli_test_app"})

    effective = feishu.effective_config()

    # app_id 来自 settings.json；allow_self_approval 只存在于 feishu.json —— 两者都要在
    assert effective["app_id"] == "cli_test_app"
    assert effective["allow_self_approval"] is True, (
        "effective_config() 必须暴露只在 feishu.json 里的键；否则设置页读不到真实生效值，"
        "保存时就会把它抹掉。"
    )


def test_settings_json_section_overrides_feishu_json(feishu_env):
    """记录导致该 bug 的优先级：settings.json 覆盖 feishu.json。"""

    feishu, config_dir = feishu_env
    _write_dedicated(config_dir, allow_self_approval=True, app_id="cli_from_feishu_json")
    _write_settings(
        config_dir,
        feishu_section={"app_id": "cli_from_settings", "allow_self_approval": False},
    )

    effective = feishu.effective_config()

    assert effective["app_id"] == "cli_from_settings"
    assert effective["allow_self_approval"] is False


# --------------------------------------------------------------------------
# validate_config：发起人==审批人时，只看 allow_self_approval
# --------------------------------------------------------------------------


def test_validate_config_rejects_identical_initiator_and_approver(feishu_env):
    feishu, config_dir = feishu_env
    _write_dedicated(config_dir, allow_self_approval=False)
    _write_settings(config_dir, feishu_section={})

    result = feishu.validate_config()

    assert not result.ok
    assert "发起人与审批人相同" in str(result.message)


def test_validate_config_allows_self_approval_when_flagged(feishu_env):
    feishu, config_dir = feishu_env
    _write_dedicated(config_dir, allow_self_approval=True)
    _write_settings(config_dir, feishu_section={})

    assert feishu.validate_config().ok


# --------------------------------------------------------------------------
# 核心回归：模拟「保存集成设置」的往返，配置不得被打坏
# --------------------------------------------------------------------------


def _simulate_save(feishu, config_dir: Path, edited: dict, *, allow_self_approval) -> None:
    """复刻 gui/main.py::save_integrations() 的写盘动作（不含 Tk 部分）。"""

    settings_path = config_dir / "settings.json"
    data = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    data["feishu"] = feishu.build_settings_section(edited, allow_self_approval)
    settings_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


GUI_FIELDS = {
    "app_id": "cli_test_app",
    "approval_code": "APPROVAL-CODE-TEST",
    "initiator_open_id": INITIATOR,
    "approver_open_id": APPROVER_SAME,
    "approval_node_id": "node-test-0001",
    "department_id": "",
}


def test_save_round_trip_keeps_self_approval_working(feishu_env):
    """保存前能用 → 保存后必须还能用（这就是被修掉的那个陷阱）。"""

    feishu, config_dir = feishu_env
    _write_dedicated(config_dir, allow_self_approval=True)
    _write_settings(config_dir, feishu_section={})

    assert feishu.validate_config().ok, "前置条件：保存前应当是可通过的"

    # 设置页打开时读生效值 → 勾选框初始值 True → 用户直接点保存
    allow_self = bool(feishu.effective_config().get("allow_self_approval", False))
    _simulate_save(feishu, config_dir, GUI_FIELDS, allow_self_approval=allow_self)

    assert feishu.validate_config().ok, (
        "点一次「保存集成设置」后飞书配置失效了 —— 保存必须保留生效的 allow_self_approval，"
        "不得硬写 False。"
    )
    assert feishu.effective_config()["allow_self_approval"] is True


def test_hard_coded_false_would_break_a_working_setup(feishu_env):
    """反向对照：证明上面的测试不是空转 —— 硬写 False 确实会打坏配置。"""

    feishu, config_dir = feishu_env
    _write_dedicated(config_dir, allow_self_approval=True)
    _write_settings(config_dir, feishu_section={})

    _simulate_save(feishu, config_dir, GUI_FIELDS, allow_self_approval=False)

    result = feishu.validate_config()
    assert not result.ok and "发起人与审批人相同" in str(result.message)


def test_save_round_trip_with_distinct_approver(feishu_env):
    feishu, config_dir = feishu_env
    _write_dedicated(config_dir, approver_open_id=APPROVER_OTHER, allow_self_approval=False)
    _write_settings(config_dir, feishu_section={})

    fields = dict(GUI_FIELDS, approver_open_id=APPROVER_OTHER)
    _simulate_save(feishu, config_dir, fields, allow_self_approval=False)

    assert feishu.validate_config().ok


# --------------------------------------------------------------------------
# build_settings_section 的形状
# --------------------------------------------------------------------------


def test_build_settings_section_trims_and_always_sets_secret_ref():
    from modules import feishu

    section = feishu.build_settings_section(
        {"app_id": "  cli_x  ", "department_id": "", "approval_code": "C"},
        allow_self_approval=True,
    )

    assert section["app_id"] == "cli_x"
    assert section["department_id"] == ""
    assert section["app_secret_ref"] == "feishu.app_secret"
    assert section["allow_self_approval"] is True


def test_build_settings_section_coerces_self_approval_to_bool():
    from modules import feishu

    assert feishu.build_settings_section({}, 0)["allow_self_approval"] is False
    assert feishu.build_settings_section({}, "x")["allow_self_approval"] is True


# --------------------------------------------------------------------------
# 源码守卫：防止有人把硬编码的 False 加回来
# --------------------------------------------------------------------------


def test_gui_no_longer_hardcodes_allow_self_approval():
    source = (ROOT / "gui" / "main.py").read_text(encoding="utf-8")

    assert '["allow_self_approval"] = False' not in source, (
        "gui/main.py 不得再把 allow_self_approval 硬写成 False："
        "该键只在 feishu.json 里，硬写会让「保存集成设置」打坏单人公司的可用配置。"
    )
    assert "feishu.build_settings_section(" in source, (
        "保存飞书配置必须走 feishu.build_settings_section()，"
        "并把勾选框（由 effective_config() 初始化）的值传进去。"
    )
    assert "feishu.effective_config()" in source, (
        "设置页必须用 effective_config() 初始化「允许发起人自审」勾选框，"
        "否则勾选框显示与实际生效值不符。"
    )
