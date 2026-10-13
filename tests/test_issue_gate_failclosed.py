# -*- coding: utf-8 -*-
"""守卫：阻断性异常「检查失败」必须 fail-closed，不能解锁报表。

背景（2026-10-14 实测）：``_open_issues()`` 原来写 ``except Exception: return []``。
DB 一出错（磁盘 I/O / 库被锁 / schema 漂移）就返回空列表，而
``_refresh_issue_state()`` 拿到空列表会做两件事：
  1. ``issue_bar.grid_remove()`` —— 提示条消失
  2. 报表按钮 ``state="normal"`` —— **本应被禁用的按钮解锁**
这个 issue 机制存在的唯一目的就是阻断报表，于是「一个查询错误」就放行了
本该被阻断的报表，且用户看不到任何提示。

正确语义：**检查失败 ≠ 没有异常**。失败时要保持阻断，并把原因显示出来。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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
        except Exception as exc:  # noqa: BLE001
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


def _make_host(gm, tk_root, monkeypatch):
    """构造一个只带 _refresh_issue_state 所需成员的宿主，并绑上真实方法。

    必须**继承** ``FinanceApp`` 而不是自造同名属性 —— ``_reports_blocked`` 是
    ``@property``，挂在普通类上拿不到描述符协议（实测踩过）。
    ``__init__`` 完全覆盖、不调 super，避免真去建整个界面。
    """
    import tkinter as tk

    import db

    class _Host(gm.FinanceApp):
        def __init__(self):
            self.issue_text = tk.Label(tk_root, text="")
            self.issue_bar = tk.Frame(tk_root)
            self.issue_bar.grid()
            self._issues = []
            self._issue_check_failed = False
            self._report_action_buttons = [
                tk.Button(tk_root, text="导出报表", state="disabled")
            ]
            self._report_action_buttons[0].grid()

    host = _Host()
    # 屏蔽日志（否则测试输出一堆 traceback）
    monkeypatch.setattr(gm.LOG, "exception", lambda *a, **k: None, raising=False)
    return host, db


def test_check_failure_keeps_reports_blocked(tk_root, monkeypatch):
    """🔴 核心用例：DB 查询抛异常时，报表必须保持暂停。"""
    import gui.main as gm

    host, db = _make_host(gm, tk_root, monkeypatch)

    # 前置条件：有异常时确实阻断
    monkeypatch.setattr(
        db, "get_open_data_issues",
        lambda severity=None: [{"id": 1, "severity": "blocking", "message": "借贷不平"}],
        raising=False,
    )
    host._refresh_issue_state()
    assert host.issue_bar.winfo_manager() != "", "有异常时应显示提示条"
    assert host._report_action_buttons[0]["state"] == "disabled", "有异常时应禁用报表"
    assert host._issue_check_failed is False

    # 关键场景：查询失败
    def boom(severity=None):
        raise OSError("disk I/O error / database is locked")

    monkeypatch.setattr(db, "get_open_data_issues", boom, raising=False)
    host._refresh_issue_state()

    assert host._issue_check_failed is True, "查询失败必须置 _issue_check_failed"
    assert host._reports_blocked is True, "查询失败时 _reports_blocked 必须为 True"
    assert host.issue_bar.winfo_manager() != "", (
        "查询失败时提示条不能消失 —— 否则用户以为没有问题"
    )
    assert host._report_action_buttons[0]["state"] == "disabled", (
        "🔴 查询失败时报表按钮被解锁了：一个 DB 错误就能放行本该阻断的报表"
    )
    assert "检查失败" in host.issue_text["text"], (
        "提示文案必须说明是检查失败，不能复用/残留旧的异常文案：%r" % host.issue_text["text"]
    )


def test_check_failure_then_recovery_unblocks(tk_root, monkeypatch):
    """检查失败后恢复（查询重新成功且无异常），应解除阻断。"""
    import gui.main as gm

    host, db = _make_host(gm, tk_root, monkeypatch)

    def boom(severity=None):
        raise OSError("locked")

    monkeypatch.setattr(db, "get_open_data_issues", boom, raising=False)
    host._refresh_issue_state()
    assert host._reports_blocked is True

    # 恢复
    monkeypatch.setattr(db, "get_open_data_issues", lambda severity=None: [], raising=False)
    host._refresh_issue_state()
    assert host._issue_check_failed is False
    assert host._reports_blocked is False
    assert host.issue_bar.winfo_manager() == "", "无异常时应隐藏提示条"
    assert host._report_action_buttons[0]["state"] == "normal"
    assert host.issue_text["text"] == "", "解除阻断时要清空文案，不能残留"


def test_stale_issue_text_is_cleared_on_recovery(tk_root, monkeypatch):
    """二阶谎言：旧实现在场景切换后不清空 issue_text，条子没了文案还在。"""
    import gui.main as gm

    host, db = _make_host(gm, tk_root, monkeypatch)

    monkeypatch.setattr(
        db, "get_open_data_issues",
        lambda severity=None: [{"id": 1, "message": "借贷不平"}],
        raising=False,
    )
    host._refresh_issue_state()
    assert "1 项" in host.issue_text["text"]

    monkeypatch.setattr(db, "get_open_data_issues", lambda severity=None: [], raising=False)
    host._refresh_issue_state()
    assert host.issue_bar.winfo_manager() == ""
    assert host.issue_text["text"] == "", (
        "提示条已隐藏但文案仍残留：%r" % host.issue_text["text"]
    )


def test_source_guard_no_silent_empty_on_failure(tk_root, monkeypatch):
    """源码守卫：_open_issues 的 except 分支不许再出现裸 return []。"""
    text = (ROOT / "gui" / "main.py").read_text(encoding="utf-8")
    start = text.find("def _open_issues")
    assert start != -1
    end = text.find("\n    @property", start)
    seg = text[start:end if end != -1 else start + 2000]
    # except 分支里必须出现 _issue_check_failed 的置位
    except_at = seg.rfind("except Exception:")
    assert except_at != -1, "_open_issues 应保留对查询失败的兜底"
    tail = seg[except_at:]
    assert "_issue_check_failed" in tail, (
        "_open_issues 的 except 分支没有置 _issue_check_failed —— "
        "这意味着查询失败会被当成「没有异常」，报表闸门会被静默解除"
    )
    assert "LOG.exception" in tail, "查询失败必须记日志（否则无法排查）"
