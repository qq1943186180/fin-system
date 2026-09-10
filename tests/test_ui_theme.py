# -*- coding: utf-8 -*-
"""UI 主题契约测试：锁住「浅色商务风」这条用户已确认的硬约束。

背景（2026-09-10 修复）：

`gui/ui_theme.py` 里曾有一个 ``configure_dark_style = configure_light_style`` 的
"back-compat alias"。名字说「深色」，实现却是**浅色** —— 后续维护者（或 AI 助手）
看到 ``configure_dark_style(root)`` 会以为在配深色主题，很可能因此「修复」出一个
真正的深色界面。而用户在看过深色合并版后**明确表示不喜欢、要求整体改回浅色**，
且已连续否决 CustomTkinter / ttkbootstrap / 深色卡片风三类方案。

这个别名已删除（统一用 ``configure_light_style``）。本文件把结论固化为测试，
防止两种退化：① 别名被重新引入；② 配色 token 被改成深色。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gui import ui_theme  # noqa: E402


def _luminance(color: str) -> float:
    """返回 #rrggbb 的相对亮度（0=黑, 255=白）。"""
    value = color.lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        raise ValueError("不是 #rgb / #rrggbb 形式的颜色: %r" % color)
    r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    # 人眼对绿色最敏感 —— 用 Rec.601 加权，别用算术平均
    return 0.299 * r + 0.587 * g + 0.114 * b


class TestNoMisleadingDarkAlias:
    """别名必须彻底消失，避免误导后人去做用户已否决的深色风。"""

    def test_dark_alias_is_gone(self) -> None:
        assert not hasattr(ui_theme, "configure_dark_style"), (
            "`configure_dark_style` 不得重新引入：它名字是深色、实现是浅色，会误导维护者；"
            "用户已明确否决深色风。请使用 configure_light_style()。"
        )

    def test_light_style_entrypoint_exists(self) -> None:
        assert callable(ui_theme.configure_light_style)


class TestColorTokensAreLight:
    """配色 token 必须保持浅色商务风（白底 + 深字）。"""

    @pytest.mark.parametrize("token", ["C_BG", "C_CARD", "C_PANEL", "C_ROW_ALT"])
    def test_surface_colors_are_light(self, token: str) -> None:
        color = getattr(ui_theme, token)
        assert _luminance(color) > 200, (
            "%s=%s 太暗。用户要求浅色商务风（白底/浅灰面），深色面是被否决的方案。"
            % (token, color)
        )

    @pytest.mark.parametrize("token", ["C_TEXT", "C_TEXT_MUTED"])
    def test_text_colors_are_dark(self, token: str) -> None:
        color = getattr(ui_theme, token)
        assert _luminance(color) < 128, (
            "%s=%s 太亮，在浅色底上会看不清（这也是曾出现过的白字白底事故）。"
            % (token, color)
        )

    def test_text_contrasts_with_surface(self) -> None:
        """正文色与卡片底色的亮度差要够 —— 锁住可读性下限。"""
        gap = _luminance(ui_theme.C_CARD) - _luminance(ui_theme.C_TEXT)
        assert gap > 100, "卡片底与正文色对比不足（差 %.0f），可能又变成白底白字" % gap


class TestThemeSourceGuard:
    """不依赖 Tk 的静态兜底：即使无显示环境（CI）也守住主题选择顺序。"""

    def test_source_prefers_clam_over_vista(self) -> None:
        source = (ROOT / "gui" / "ui_theme.py").read_text(encoding="utf-8")
        assert '("clam", "alt", "default")' in source, (
            "ui_theme.py 的主题优先序列必须是 (\"clam\", \"alt\", \"default\")；"
            "'vista' 会忽略 ttk background 映射 → 选中页签白底白字。"
        )


class TestTtkThemeChoice:
    """`vista` 主题会忽略 ttk background 映射 → 选中页签白底白字，必须用 clam。

    这里不看源码文本，而是**真的建一个 Tk root 应用主题、再回读样式值** ——
    文本断言只能证明「写了 clam」，回读才能证明「clam 生效且映射没被吃掉」。

    稳定性说明：Windows 上短时间内反复 `tk.Tk()` 建销窗口偶发 TclError
    （实测 5 连跑里有 1~2 次失败），故改为 **module 级单实例 + 最多 3 次重试**，
    把建窗次数从「每用例一次」降到「整文件一次」。
    """

    @pytest.fixture(scope="module")
    def themed_root(self):
        tk = pytest.importorskip("tkinter")
        root = None
        error: Exception | None = None
        for _ in range(3):
            try:
                root = tk.Tk()
                break
            except tk.TclError as exc:  # 偶发的显示/资源竞争
                error = exc
                time.sleep(0.3)
        if root is None:
            pytest.skip("当前环境无可用显示，无法做真实主题回读：%s" % error)
        root.withdraw()  # 不弹窗
        try:
            ui_theme.configure_light_style(root)
            yield root
        finally:
            root.destroy()

    def test_clam_theme_is_actually_applied(self, themed_root) -> None:
        from tkinter import ttk

        applied = ttk.Style(themed_root).theme_use()
        assert applied == "clam", "实际生效的主题是 %r，应为 'clam'" % applied

    def test_selected_tab_stays_readable(self, themed_root) -> None:
        """历史事故：vista 下选中页签变成白底白字，完全看不见。"""
        from tkinter import ttk

        style = ttk.Style(themed_root)
        bg = str(style.lookup("TNotebook.Tab", "background", ["selected"]) or "")
        fg = str(style.lookup("TNotebook.Tab", "foreground", ["selected"]) or "")
        assert bg.lower() not in ("", "none"), "选中页签没有取到背景色（主题映射被忽略）"
        assert fg.lower() not in ("", "none"), "选中页签没有取到前景色"
        if bg.startswith("#") and fg.startswith("#"):
            assert _luminance(bg) < _luminance(fg), (
                "选中页签 %s/%s 对比不足，可能又是白底白字" % (bg, fg)
            )

    def test_treeview_background_is_light(self, themed_root) -> None:
        """Treeview 底色须为浅色 —— 深色表格是被否决的方案。"""
        from tkinter import ttk

        bg = str(ttk.Style(themed_root).lookup("Treeview", "background") or "")
        if bg.startswith("#"):
            assert _luminance(bg) > 200, "Treeview 底色 %s 偏暗" % bg
