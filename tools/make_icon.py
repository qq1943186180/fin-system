#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""重新生成 icon.ico（蓝色圆角方块 + 白色 ¥）。

## 为什么不用字体渲染 ¥

试过三种字体方案（Arial Bold / 微软雅黑 Bold / 加大字号+粗描边），
**缩到 16×16 全部糊成一团**：字体在 ¥ 的三横交汇处笔画极细，
降采样后三条横线糊在一起，看起来像一团白斑。

最终改**手绘线段**：笔画宽度是显式参数（`width_ratio`），不随字号缩放，
所以每个尺寸都能单独调粗，16×16 下依然能辨认出 ¥ 的轮廓。
（实测对比图见 git 提交记录 / 本文件注释。）

## 用法

    <python> tools/make_icon.py

会直接覆盖仓库根的 `icon.ico`，并输出一张各尺寸预览图便于肉眼复核。
改完图标**必须重新 `build.ps1`**，否则 exe 里还是旧图标。

## 设计约束

- 配色取自 `gui/ui_theme.py`：`#3b82f6` → `#1d4ed8`（蓝色系，与浅色商务风一致）。
- 圆角半径 0.235×边长（Windows 11 磁贴风格的近似值）。
- 多尺寸：16/20/24/32/40/48/64/128/256，覆盖任务栏、桌面、Alt-Tab、文件属性。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw

S = 1024  # 超采样母版边长
WHITE = (255, 255, 255, 255)

# 圆角方块渐变（上浅下深）
GRAD_TOP = (59, 130, 246)      # #3b82f6
GRAD_BOTTOM = (29, 78, 216)    # #1d4ed8
CORNER_RATIO = 0.235

# 每个尺寸单独给 (整体缩放, 笔画宽度比)。
# 小尺寸笔画要略粗，否则降采样后线条发灰、看不出形状。
SIZE_PLAN: dict[int, tuple[float, float]] = {
    16: (1.00, 0.125),
    20: (1.00, 0.120),
    24: (1.00, 0.117),
    32: (1.00, 0.115),
    40: (1.00, 0.114),
    48: (1.00, 0.113),
    64: (1.00, 0.112),
    128: (1.00, 0.112),
    256: (1.00, 0.112),
}

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = REPO_ROOT / "icon.ico"


def _base() -> Image.Image:
    """圆角方块 + 垂直渐变，无文字。"""

    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, S - 1, S - 1], radius=int(S * CORNER_RATIO), fill=255
    )

    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / (S - 1)
        gd.line(
            [(0, y), (S, y)],
            fill=tuple(
                int(GRAD_TOP[i] + (GRAD_BOTTOM[i] - GRAD_TOP[i]) * t) for i in range(3)
            )
            + (255,),
        )
    img.paste(grad, (0, 0), mask)
    return img


def _draw_yen(scale: float, width_ratio: float) -> Image.Image:
    """在母版上画手绘 ¥。

    ``scale`` 缩放整体字形（1.0 = 原始比例），``width_ratio`` 是笔画宽度占边长的比。
    """

    img = _base()
    d = ImageDraw.Draw(img)
    w = max(1, int(S * width_ratio))
    cx = S // 2

    def sx(v: float) -> int:
        return int(S * (0.5 + (v - 0.5) * scale))

    def sy(v: float) -> int:
        return int(S * v)

    # 两撇（V 形）
    d.line([(sx(0.20), sy(0.195)), (cx, sy(0.545))], fill=WHITE, width=w, joint="curve")
    d.line([(sx(0.80), sy(0.195)), (cx, sy(0.545))], fill=WHITE, width=w, joint="curve")
    # 中竖
    d.line([(cx, sy(0.465)), (cx, sy(0.845))], fill=WHITE, width=w)
    # 两横
    d.line([(sx(0.245), sy(0.570)), (sx(0.755), sy(0.570))], fill=WHITE, width=w)
    d.line([(sx(0.245), sy(0.712)), (sx(0.755), sy(0.712))], fill=WHITE, width=w)
    return img


def build(output: Path = OUTPUT, preview: Path | None = None) -> Path:
    frames = []
    for size in sorted(SIZE_PLAN):
        scale, width = SIZE_PLAN[size]
        frames.append(_draw_yen(scale, width).resize((size, size), Image.LANCZOS))

    output.parent.mkdir(parents=True, exist_ok=True)
    frames[-1].save(
        str(output), format="ICO", sizes=[(f.width, f.height) for f in frames]
    )

    if preview is not None:
        show = [16, 20, 24, 32, 48, 256]
        cell = 150
        sheet = Image.new(
            "RGBA", (cell * len(show) + 8 * (len(show) + 1), cell + 16), (245, 246, 248, 255)
        )
        x = 8
        for frame in frames:
            if frame.width not in show:
                continue
            big = frame.resize(
                (cell, cell), Image.NEAREST if frame.width < 48 else Image.LANCZOS
            )
            sheet.paste(big, (x, 8), big)
            x += cell + 8
        preview.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(str(preview))

    return output


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    preview = Path(argv[0]) if argv else None
    path = build(preview=preview)

    im = Image.open(str(path))
    sizes = sorted(im.info.get("sizes", []))
    print("已生成: %s" % path)
    print("尺寸: %s" % ", ".join("%dx%d" % s for s in sizes))
    print("文件大小: %.1f KB" % (os.path.getsize(path) / 1024))
    if preview:
        print("预览: %s" % preview)
    print()
    print("⚠️ 改完图标必须重新 build.ps1，否则 exe 里仍是旧图标。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
