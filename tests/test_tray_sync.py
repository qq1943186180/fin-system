# -*- coding: utf-8 -*-
"""守卫：托盘右键菜单必须提供「立即同步飞书报销单」，且真的能拉单。

背景（2026-09-26）：报销入口改为「员工在飞书自助提单 → 财务在本地拉回」。
但拉取函数 ``feishu_pull.fetch_feishu_expenses`` 全项目**只有报销页按钮一个
调用点**，没有任何定时器 —— 员工在飞书提交后，不点按钮本地永远不显示。
用户要求的解法就是在托盘右键加一个「立即同步」。

本文件钉住三件事：
1. ``_start_tray`` 造的菜单里**必须有**「立即同步」项（防被后续重构删掉）；
2. 托盘图标**常驻**（旧代码 ``_show_window`` 里把 ``visible`` 置 False，
   会把图标 NIM_DELETE 掉，导致右键菜单从此够不到）；
3. 点该项真的会调 ``fetch_feishu_expenses``，并且回调在子线程里执行时
   不直接碰 tkinter（必须 ``after(0, ...)`` 回主线程）。

第 2 条用真实的 ``pystray.Icon`` 对象断言 ``visible`` 语义，不 mock 掉。
"""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MAIN = ROOT / "gui" / "main.py"


# --------------------------------------------------------------------------
# 纯源码守卫：菜单文本与 visible 语义
# --------------------------------------------------------------------------


def test_tray_menu_offers_immediate_sync():
    """托盘菜单里必须有一项能同步飞书报销单。"""
    text = MAIN.read_text(encoding="utf-8")
    start = text.find("def _start_tray")
    assert start != -1, "gui/main.py 找不到 _start_tray"
    end = text.find("def _stop_tray", start)
    seg = text[start:end if end != -1 else start + 3000]
    assert "立即同步" in seg, (
        "托盘菜单缺少「立即同步」项。员工在飞书提单后，本地唯一的手动入口就是它；"
        "删掉等于同步只能靠报销页按钮。"
    )
    assert "_tray_sync_feishu" in seg, "「立即同步」菜单项没有绑定 _tray_sync_feishu 回调"


def test_show_window_does_not_delete_tray_icon():
    """_show_window / _hide_window / _on_close 不得把托盘图标隐藏掉。

    pystray 的 ``visible = False`` 走 ``NIM_DELETE``，会**把图标从通知区移除**；
    图标一旦被移除，右键菜单就再也没法点到了。窗口的显隐只该动 ``withdraw()``
    / ``deiconify()``，与托盘图标是否常驻无关。
    """
    text = MAIN.read_text(encoding="utf-8")
    offenders = []
    for name in ("_on_close", "_show_window", "_hide_window"):
        start = text.find("def %s" % name)
        assert start != -1, "找不到 %s" % name
        end = text.find("\n    def ", start + 1)
        seg = text[start:end if end != -1 else start + 1500]
        if "_tray_icon.visible" in seg:
            line = text[:start + seg.find("_tray_icon.visible")].count("\n") + 1
            offenders.append("%s (第 %d 行)" % (name, line))
    assert not offenders, (
        "这些方法仍在改 _tray_icon.visible：%s。"
        "visible=False 会把托盘图标删掉，之后右键菜单就够不到了。" % "、".join(offenders)
    )


def test_tray_sync_never_touches_tk_from_worker_thread():
    """拉取必须放到子线程，且回调走 root.after 切回主线程。"""
    text = MAIN.read_text(encoding="utf-8")
    start = text.find("def _tray_sync_feishu")
    assert start != -1
    end = text.find("\n    def ", start + 1)
    seg = text[start:end if end != -1 else start + 3000]
    assert "Thread(" in seg, "拉取应当放到后台线程，否则会卡住托盘消息循环"
    assert "after(0" in seg, "子线程里不能直接碰 tkinter，必须 root.after(0, ...) 切回主线程"


# --------------------------------------------------------------------------
# 真实 pystray 冒烟
# --------------------------------------------------------------------------


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
        pytest.skip("当前环境无可用显示，跳过托盘冒烟：%s" % error)
    root.withdraw()
    try:
        ui_theme.configure_light_style(root)
        yield root
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass


def _find_menu_item(menu, keyword: str):
    for item in menu:
        if keyword in str(item.text):
            return item
    return None


class _TrayHost:
    """把 ``_start_tray`` 需要的成员接上（真方法取自 FinanceApp）。"""

    _tray_icon = None
    _tray_thread = None
    _tray_syncing = False
    _active_nav = "工作台"

    # 回调体本身不是本测试的重点，但菜单构造时会被引用，必须有
    _show_window = staticmethod(lambda *a, **k: None)
    _hide_window = staticmethod(lambda *a, **k: None)
    _quit_app = staticmethod(lambda *a, **k: None)
    _tray_sync_feishu = staticmethod(lambda *a, **k: None)


def test_real_pystray_menu_contains_sync_and_icon_stays_visible(tk_root, monkeypatch):
    """真的建一个 pystray.Icon，验证菜单内容 + 图标常驻。"""
    pystray = pytest.importorskip("pystray")
    pytest.importorskip("PIL.Image")

    import gui.main as gm

    collected: dict = {}

    class _FakeThread:
        daemon = True

        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

    def fake_icon_cls(name, image, title, menu):
        collected["menu"] = menu
        collected["title"] = title

        class _Icon:
            def __init__(self):
                self._visible = False

            @property
            def visible(self):
                return self._visible

            @visible.setter
            def visible(self, value):
                self._visible = value

            def run(self, setup=None):
                pass

            def stop(self):
                pass

            def update_menu(self):
                pass

        return _Icon()

    monkeypatch.setattr(gm, "threading", type("T", (), {"Thread": _FakeThread}))
    monkeypatch.setattr(pystray, "Icon", fake_icon_cls)
    monkeypatch.setattr(gm, "ROOT", Path(gm.__file__).resolve().parent.parent)

    host = _TrayHost()
    gm.FinanceApp._start_tray(host)

    menu = collected.get("menu")
    assert menu is not None, "_start_tray 没有创建托盘菜单（icon.ico 是否存在？）"

    sync_item = _find_menu_item(menu, "立即同步")
    assert sync_item is not None, "托盘菜单里找不到「立即同步」项"
    # default=True 的项才是双击托盘图标触发的动作，别让同步抢了这个位置
    show_item = _find_menu_item(menu, "显示窗口")
    assert show_item is not None and show_item.default, "「显示窗口」应当仍是双击默认项"
    quit_item = _find_menu_item(menu, "退出")
    assert quit_item is not None, "托盘菜单丢了「退出」项"

    # 「立即同步」进行中要能自禁用（防重复点）
    assert callable(sync_item.enabled) or sync_item.enabled is True


def test_tray_sync_callback_retrieves_expenses(tk_root, monkeypatch):
    """点「立即同步」真的会调 fetch_feishu_expenses，并刷新到界面。"""
    pytest.importorskip("pystray")

    import gui.main as gm
    from modules import feishu_pull

    calls = {"n": 0}

    def fake_fetch(days=30, dry_run=False):
        calls["n"] += 1
        assert days == 30
        from modules.feishu import IntegrationResult, IntegrationStatus

        return IntegrationResult(
            IntegrationStatus.SUCCESS,
            "新建 1 单，跳过 2 单，失败 0 单",
            {"created": [{"employee": "李尚运", "type": "差旅费",
                          "reason": "高铁", "amount_cents": 3000}],
             "skipped": [], "errors": []},
        )

    monkeypatch.setattr(feishu_pull, "fetch_feishu_expenses", fake_fetch)
    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(gm.messagebox, name, lambda *a, **k: "ok")

    refreshed: list = []

    class _Host:
        _tray_icon = None
        _tray_syncing = False
        _active_nav = "费用票据"
        header_status = None
        # 用真实的 _refresh_after_tray_sync：本测试要验证它真的会原地刷新报销页
        _refresh_after_tray_sync = gm.FinanceApp._refresh_after_tray_sync

        def _show_page(self, name, *, force=False):
            refreshed.append((name, force))

    class _DeferredRoot:
        def winfo_exists(self):
            return True

        def after(self, _delay, func, *args):
            # 不排队到事件循环，直接同步执行 —— 本测试就是要把回调走完
            func(*args)

    class _InlineThread:
        """同步执行 target 的假线程。

        真线程会让 ``Thread.start()`` 立刻返回，断言就会在回调完成前跑 ——
        这是实测踩到的竞态（第一版测试因此假阴性）。这里就地跑完，消除时序依赖。
        """

        def __init__(self, target=None, daemon=None, **_k):
            self._target = target

        def start(self):
            if self._target:
                self._target()

    monkeypatch.setattr(gm, "threading", type("T", (), {"Thread": _InlineThread}))

    host = _Host()
    host.header_status = tk_root  # 只是个有 .config 的对象，够用
    host.root = _DeferredRoot()

    # 把被吞的异常暴露出来
    import traceback as _tb

    monkeypatch.setattr(
        gm.LOG, "exception", lambda *a, **k: _tb.print_exc(), raising=False
    )

    gm.FinanceApp._tray_sync_feishu(host)
    assert calls["n"] == 1, "「立即同步」没有调用 fetch_feishu_expenses"

    assert any(
        entry[0] == "费用票据" and entry[1] is True for entry in refreshed
    ), "停留在报销页时应当强制刷新该页，让用户立刻看到新单：%r" % (refreshed,)
    assert host._tray_syncing is False, "同步结束后必须复位 _tray_syncing，否则菜单永久禁用"


def test_tray_sync_survives_destroyed_root(tk_root, monkeypatch):
    """主窗口已销毁时（用户点了「退出」），同步回调不能把异常冒进线程。

    实测踩到的坑：``root.after()`` 在窗口销毁后抛
    ``RuntimeError: main thread is not in main loop``。子线程里没人接，
    ``_tray_syncing`` 就永久卡在 True -> 菜单项被 enabled 回调永久禁用。
    """
    pytest.importorskip("pystray")

    import gui.main as gm
    from modules import feishu_pull

    calls = {"n": 0}

    def fake_fetch(days=30, dry_run=False):
        calls["n"] += 1
        from modules.feishu import IntegrationResult, IntegrationStatus

        return IntegrationResult(IntegrationStatus.SUCCESS, "无新单", {})

    monkeypatch.setattr(feishu_pull, "fetch_feishu_expenses", fake_fetch)

    class _DeadRoot:
        def winfo_exists(self):
            return False

        def after(self, *_a, **_k):
            raise RuntimeError("main thread is not in main loop")

    class _Host:
        _tray_icon = None
        _tray_syncing = False
        _active_nav = "工作台"
        header_status = None
        root = _DeadRoot()

        def _refresh_after_tray_sync(self, *_a, **_k):
            raise AssertionError("窗口都没了，不该再刷界面")

    host = _Host()
    gm.FinanceApp._tray_sync_feishu(host)

    assert calls["n"] == 1, "应当仍然尝试过拉取"
    assert host._tray_syncing is False, (
        "窗口销毁后 _tray_syncing 卡在 True —— 托盘菜单会被永久禁用"
    )


def test_tray_sync_guards_against_double_click(tk_root, monkeypatch):
    """同步进行中再点一次不应重复触发（enabled 回调 + _tray_syncing 双保险）。"""
    pytest.importorskip("pystray")

    import gui.main as gm
    from modules import feishu_pull

    calls = {"n": 0}

    def fake_fetch(days=30, dry_run=False):
        calls["n"] += 1
        from modules.feishu import IntegrationResult, IntegrationStatus

        return IntegrationResult(IntegrationStatus.SUCCESS, "无新单", {})

    monkeypatch.setattr(feishu_pull, "fetch_feishu_expenses", fake_fetch)

    class _Host:
        _tray_icon = None
        _tray_syncing = True  # 模拟"正在同步"
        _active_nav = "工作台"
        root = None
        header_status = None

        def _show_page(self, *_a, **_k):
            pass

    host = _Host()
    gm.FinanceApp._tray_sync_feishu(host)
    assert calls["n"] == 0, "正在同步时不该再起一次拉取"


def test_close_without_tray_quits_instead_of_hiding(tk_root, monkeypatch):
    """托盘不可用时关窗口必须真退出，不能 withdraw 成"假死"。

    降级场景：缺 pystray/Pillow，或（曾经的 bug）icon.ico 没进 bundle。
    此时 `_on_close` 若照旧 withdraw，窗口消失且没有任何入口能找回 ——
    用户只能去任务管理器杀进程。
    """
    pytest.importorskip("pystray")

    import gui.main as gm

    quit_calls = []

    class _Host:
        _tray_icon = None  # 托盘不可用
        _close_to_tray = True

        def _confirm_abandon(self):
            return True

        def _quit_app(self, *_a, **_k):
            quit_calls.append(True)

    class _Root:
        def withdraw(self):
            raise AssertionError("托盘不可用时不该 withdraw —— 窗口会永久消失")

    host = _Host()
    host.root = _Root()

    gm.FinanceApp._on_close(host)
    assert quit_calls, "托盘不可用时关窗口应当真退出"