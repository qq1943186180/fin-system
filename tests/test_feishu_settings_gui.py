# -*- coding: utf-8 -*-
"""真实 Tk 冒烟：设置页的「保存集成设置」不得打坏正在工作的飞书配置。

与 ``tests/test_feishu_settings.py`` 的分工：
- 那个文件测的是**配置语义**（``effective_config`` / ``build_settings_section`` / 源码守卫）；
- 本文件**真的建一个 Tk root、真的构建设置页、真的点那个按钮**，再回读 settings.json。

为什么需要它：静态断言只能证明「代码这么写了」，只有把按钮点下去才能证明
「勾选框初始值 = 生效值」「保存写回的是勾选框的值」这条链路真的通。

稳定性：Windows 上反复 ``tk.Tk()`` 偶发 TclError / 找不到 tcl 库（同 test_ui_theme
记录的坑），故改为 **module 级单实例 + 最多 3 次重试**；确实起不来则 skip。
"""

from __future__ import annotations

import importlib
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


INITIATOR = "ou_cccccccccccccccccccccccccccccc"

DEDICATED = {
    "app_id": "cli_gui_test",
    "approval_code": "APPROVAL-CODE-GUI",
    "initiator_open_id": INITIATOR,
    "approver_open_id": INITIATOR,  # 单人公司：发起人 == 审批人
    "approval_node_id": "node-gui-0001",
    "app_secret_ref": "feishu.app_secret",
    "allow_self_approval": True,
}


@pytest.fixture(scope="module")
def tk_root():
    tk = pytest.importorskip("tkinter")

    from gui import ui_theme

    root = None
    error: Exception | None = None
    for _ in range(3):
        try:
            root = tk.Tk()
            break
        except Exception as exc:  # noqa: BLE001 - 显示/资源竞争，重试即可
            error = exc
            time.sleep(0.3)
    if root is None:
        pytest.skip("当前环境无可用显示，跳过真实 Tk 冒烟：%s" % error)
    root.withdraw()
    try:
        ui_theme.configure_light_style(root)
        yield root
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture()
def gui_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("FINANCE_SYSTEM_HOME", str(home))
    monkeypatch.setenv("FINANCE_SYSTEM_TEST_MODE", "1")

    import app_paths
    import db

    importlib.reload(app_paths)
    importlib.reload(db)
    db.configure_db_path(tmp_path / "finance.db")
    db.init_db(tmp_path / "finance.db")

    from modules import feishu

    importlib.reload(feishu)
    monkeypatch.setattr(feishu, "_legacy_config", lambda: {})
    monkeypatch.setattr(feishu, "get_secret", lambda _name: "test-secret")

    config_dir = Path(app_paths.CONFIG_DIR)
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "feishu.json").write_text(
        json.dumps(DEDICATED, ensure_ascii=False), encoding="utf-8"
    )
    # settings.json 只存设置页能编辑的字段 —— 与生产现状一致（没有 allow_self_approval）
    (config_dir / "settings.json").write_text(
        json.dumps(
            {
                "feishu": {
                    "app_id": "cli_gui_test",
                    "approval_code": "APPROVAL-CODE-GUI",
                    "initiator_open_id": INITIATOR,
                    "approver_open_id": INITIATOR,
                    "approval_node_id": "node-gui-0001",
                    "department_id": "",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    import gui.main as gm

    def _no_popup(*_a, **_k):
        return "ok"

    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(gm.messagebox, name, _no_popup)

    return gm, config_dir


class _SettingsPageHost:
    """把 ``FinanceApp._page_settings`` 依赖的成员接上。

    ``_panel`` / ``_load_settings_json`` / ``_save_settings_json`` / ``_settings_path``
    直接复用真实实现（保证写盘行为与生产一致），其余只做空实现。
    """

    _issues: list = []
    # _page_settings 读这两个字段（报表阻断态）；桩宿主必须给全，
    # 否则新增任何状态字段都会让这个 GUI 冒烟炸掉。
    # 注意：真 FinanceApp 上 _reports_blocked 是 @property，桩上用普通类属性即可。
    _issue_check_failed = False
    _reports_blocked = False
    _backup_error = ""
    _period_values = staticmethod(lambda: ["2026-09"])

    def _panel(self, parent, **kwargs):
        import tkinter as tk

        options = {"bg": "#ffffff", "highlightthickness": 1}
        options.update(kwargs)
        return tk.Frame(parent, **options)

    def _backup_status_text(self):
        return ""

    def _period_action(self, *_a, **_k):
        pass

    def _create_backup(self):
        pass

    def _integrity_check(self):
        pass

    def _mark_dirty(self, *_a, **_k):
        pass

    def _clear_dirty(self):
        pass

    def _open_issue_dialog(self):
        pass


def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


def _build_settings_page(gm, tk_root, config_dir):
    import tkinter as tk
    from tkinter import ttk

    parent = tk.Frame(tk_root)
    # 必须挂在 class 上才是绑定方法；直接赋给实例拿不到 self。
    host_cls = type(
        "_Host",
        (_SettingsPageHost,),
        {
            "_load_settings_json": gm.FinanceApp._load_settings_json,
            "_save_settings_json": gm.FinanceApp._save_settings_json,
            "_settings_path": gm.FinanceApp._settings_path,
        },
    )
    host = host_cls()
    host.root = tk_root
    host.header_status = tk.Label(tk_root, text="")

    gm.FinanceApp._page_settings(host, parent)
    tk_root.update_idletasks()

    checkboxes = [
        w for w in _walk(parent)
        if isinstance(w, ttk.Checkbutton) and "允许发起人自审" in str(w.cget("text"))
    ]
    save_buttons = [
        w for w in _walk(parent)
        if isinstance(w, ttk.Button) and str(w.cget("text")) == "保存集成设置"
    ]
    return parent, checkboxes, save_buttons


def test_settings_checkbox_reflects_effective_allow_self_approval(gui_env, tk_root):
    gm, config_dir = gui_env
    parent, checkboxes, save_buttons = _build_settings_page(gm, tk_root, config_dir)
    try:
        assert checkboxes, "设置页缺少「允许发起人自审」勾选框"
        assert save_buttons, "设置页缺少「保存集成设置」按钮"
        assert checkboxes[0].instate(["selected"]), (
            "勾选框初始值应为生效值 True（它只存在于 feishu.json），否则一保存就会把配置打坏"
        )
    finally:
        parent.destroy()


def test_clicking_save_keeps_feishu_config_working(gui_env, tk_root):
    """真正的端到端：点「保存集成设置」，然后配置仍然可用。"""

    gm, config_dir = gui_env
    from modules import feishu

    assert feishu.validate_config().ok, "前置条件：保存前应是可通过的"

    parent, _checkboxes, save_buttons = _build_settings_page(gm, tk_root, config_dir)
    try:
        save_buttons[0].invoke()
        tk_root.update_idletasks()
    finally:
        parent.destroy()

    written = json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))
    assert written["feishu"]["allow_self_approval"] is True, (
        "点「保存集成设置」把 allow_self_approval 写成了 %r —— 这会打坏发起人==审批人的可用配置"
        % written["feishu"].get("allow_self_approval")
    )
    assert feishu.validate_config().ok, "保存后飞书配置失效了"


def test_unchecking_self_approval_is_respected(gui_env, tk_root):
    """反向：用户主动取消勾选时，必须真的写 False（保留逻辑不能反过来无视用户）。"""

    gm, config_dir = gui_env
    parent, checkboxes, save_buttons = _build_settings_page(gm, tk_root, config_dir)
    try:
        checkboxes[0].invoke()  # 取消勾选
        tk_root.update_idletasks()
        save_buttons[0].invoke()
        tk_root.update_idletasks()
    finally:
        parent.destroy()

    written = json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))
    assert written["feishu"]["allow_self_approval"] is False
