# -*- coding: utf-8 -*-
"""守卫：主操作按钮必须用 C_ACCENT 系列，不能用 C_NAV_ACTIVE。

背景（2026-09-26）：报销页「从飞书拉取报销单」按钮被写成
``bg=C_NAV_ACTIVE`` + ``fg="white"``。C_NAV_ACTIVE 是**侧边栏当前项背景**
（#eff6ff，近白），白字压在上面 → 按钮看起来像禁用的空白块。
同页另有 2 处（新增往来方 / 运行发票识别）同病。

这类"颜色语义用错但代码能跑"的 bug 不会报错、测试也抓不到，
所以用源码守卫把它钉死。
"""
from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAIN = PROJECT_ROOT / "gui" / "main.py"
THEME = PROJECT_ROOT / "gui" / "ui_theme.py"


def test_nav_active_is_not_used_as_button_background():
    """C_NAV_ACTIVE 只能当侧边栏/导航态底色，不能出现在 tk.Button 的 bg=。"""
    text = MAIN.read_text(encoding="utf-8")
    offenders = []
    for m in re.finditer(r"tk\.Button\(", text):
        start = m.start()
        end = text.find(")", start)
        seg = text[start:min(end + 1, len(text))]
        if "C_NAV_ACTIVE" in seg:
            line = text[:start].count("\n") + 1
            offenders.append(line)
    assert not offenders, (
        "gui/main.py 第 %s 行：tk.Button 用了 C_NAV_ACTIVE 当底色。"
        "主操作按钮请用 C_ACCENT（#2563eb），否则白字压在近白底上、"
        "按钮会看起来像禁用状态。" % offenders
    )


def test_primary_buttons_use_accent_and_white_text():
    """凡是 bg=C_ACCENT 的按钮，前景必须是白字（保证对比度）。"""
    text = MAIN.read_text(encoding="utf-8")
    for m in re.finditer(r"bg=C_ACCENT\b", text):
        # 往后找同一调用里的 fg=
        seg = text[m.start():m.start() + 400]
        line_end = seg.find("\n") if "(" not in seg[:20] else len(seg)
        assert 'fg="white"' in seg or "fg='white'" in seg, (
            "gui/main.py 第 %d 行附近：bg=C_ACCENT 的按钮没有配白色前景"
            % (text[:m.start()].count("\n") + 1)
        )


def test_theme_colors_are_distinct_enough_from_text():
    """C_NAV_ACTIVE 与 C_ACCENT 必须是两个明显不同的颜色。

    如果哪天有人把 C_NAV_ACTIVE 改深了、或把 C_ACCENT 改浅了，
    这个测试会先炸，提醒重新检查按钮语义。
    """
    src = THEME.read_text(encoding="utf-8")

    def grab(name: str) -> tuple[int, int, int]:
        m = re.search(r'^%s\s*=\s*"#([0-9a-fA-F]{6})"' % name, src, re.M)
        assert m, "ui_theme.py 找不到 %s 的十六进制定义" % name
        h = m.group(1)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))

    nav = grab("C_NAV_ACTIVE")
    acc = grab("C_ACCENT")
    lum = lambda c: 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]  # noqa: E731
    assert lum(nav) > 200, (
        "C_NAV_ACTIVE=%s 应当是很浅的导航底色（亮度 >200），实测 %.0f"
        % (nav, lum(nav))
    )
    assert lum(acc) < 200, (
        "C_ACCENT=%s 应当是有饱和度的主操作色（亮度 <200），实测 %.0f"
        % (acc, lum(acc))
    )
    assert lum(nav) - lum(acc) > 60, (
        "C_NAV_ACTIVE(%.0f) 与 C_ACCENT(%.0f) 亮度差太小，按钮语义会混淆"
        % (lum(nav), lum(acc))
    )
