# -*- coding: utf-8 -*-
"""从 ImageGen 输出图生成 fin-system 的 icon.ico（可复现）。

用法：
    python tools/make_icon_from_image.py <1024x1024 生图> [--out icon.ico]

背景（踩过的坑，别重犯）：
1. ImageGen 的 `background=transparent` **实测不生效** —— 输出是纯白底 RGB，
   0% 透明像素。所以必须自己抠圆角矩形。
2. 生成的图**满幅铺色**（蓝色一直铺到画布边缘，无内边距），
   不能直接用，得先按"饱和蓝"bbox 裁出真正的圆角方形。
3. 生成图的 ¥ 横线偏细，缩到 16/20px 会与竖线糊在一起。
   所以小尺寸单独做**白色笔画膨胀**再降采样（见 BOLD_FOR）。
4. 生图可能带右下角水印。传入前先目视确认，
   或看本脚本 verify_no_watermark() 的象限分布是否均匀
   （均匀 = 光带/阴影自然过渡；集中在一角 = 水印）。

依赖：Pillow + numpy（hermes venv 里都有）。
"""
import argparse
import os
import struct
import hashlib
import numpy as np
from PIL import Image, ImageFilter, ImageDraw

SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]
# 只在真正会糊的尺寸上加粗白笔画；别的尺寸保持原样，避免失去质感和光影
BOLD_FOR = {16: 1.35, 20: 1.22}


def find_badge(img):
    """按"饱和蓝"找到 badge 的真实 bbox 与圆角半径。

    生图是满幅铺色的，白底检测没用 —— 要用颜色饱和度反推。
    """
    a = np.asarray(img.convert("RGB")).astype(np.int16)
    R, G, B = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    blue = (B - R > 25) & (B > 60)
    ys, xs = np.where(blue)
    if len(xs) == 0:
        raise SystemExit("没找到蓝色 badge —— 图可能不是蓝色系图标")
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    bw, bh = x1 - x0 + 1, y1 - y0 + 1

    # 逐行看蓝色左边界回到 0 的那一行，即为圆角半径
    radius = None
    for dy in range(min(bh // 3, 300)):
        rr = np.where(blue[y0 + dy])[0]
        if len(rr) and rr.min() - x0 <= 0:
            radius = dy
            break
    if not radius:
        radius = int(bw * 0.225)
    return (x0, y0, x1, y1), (bw, bh), radius


def cutout(img, box, size, radius):
    """裁出 badge 并按超采样圆角 mask 抠透明（抗锯齿）。"""
    x0, y0, x1, y1 = box
    bw, bh = size
    SS = 8
    canvas = Image.new("L", (bw * SS, bh * SS), 0)
    ImageDraw.Draw(canvas).rounded_rectangle(
        [0, 0, bw * SS - 1, bh * SS - 1], radius=radius * SS, fill=255)
    mask = canvas.resize((bw, bh), Image.LANCZOS)
    badge = img.crop((x0, y0, x1 + 1, y1 + 1)).convert("RGBA")
    badge.putalpha(mask)
    return badge


def render(badge, s):
    """渲染单个尺寸；小尺寸做白笔画膨胀以保住可读性。"""
    if s not in BOLD_FOR:
        return badge.resize((s, s), Image.LANCZOS)
    k = BOLD_FOR[s]
    S = s * 4
    big = badge.resize((S, S), Image.LANCZOS)
    a = np.asarray(big).astype(np.float64)
    lum = a[:, :, :3].mean(axis=2, keepdims=True)
    is_white = (lum > 195) & (a[:, :, 3:4] > 128)
    m = Image.fromarray((is_white[:, :, 0] * 255).astype(np.uint8))
    grow = np.asarray(m.filter(ImageFilter.MaxFilter(3))).astype(np.float64) / 255.0
    delta = np.clip(grow - is_white[:, :, 0].astype(np.float64), 0, 1) * (k - 1.0) * 2.6
    rgb = a[:, :, :3]
    rgb = rgb + (255.0 - rgb) * np.clip(delta, 0, 0.85)[:, :, None]
    out = np.concatenate([np.clip(rgb, 0, 255), a[:, :, 3:4]], axis=2).astype(np.uint8)
    return Image.fromarray(out, "RGBA").resize((s, s), Image.LANCZOS)


def verify_no_watermark(img, box):
    """按象限看中性灰像素分布。均匀=光带/阴影；集中一角=疑似水印。"""
    x0, y0, x1, y1 = box
    inner = np.asarray(img.convert("RGB")).astype(int)[y0:y1, x0:x1]
    R, G, B = inner[:, :, 0], inner[:, :, 1], inner[:, :, 2]
    neutral = (np.abs(R - B) < 18) & (np.abs(R - G) < 18)
    lum = inner.mean(axis=2)
    suspect = neutral & (lum > 120) & (lum < 215)
    qh, qw = inner.shape[0] // 2, inner.shape[1] // 2
    quads = {
        "左上": suspect[:qh, :qw].sum(), "右上": suspect[:qh, qw:].sum(),
        "左下": suspect[qh:, :qw].sum(), "右下": suspect[qh:, qw:].sum(),
    }
    tot = sum(quads.values()) or 1
    spread = max(quads.values()) / tot
    print("[水印检查] 中性灰像素总数=%d 象限分布=%s 最大占比=%.1f%%"
          % (tot, quads, 100 * spread))
    if spread > 0.60:
        print("   ⚠️ 疑似有局部水印（某象限占比 >60%），请目视确认")
    else:
        print("   ✅ 分布均匀，判为光带/阴影自然过渡，非水印")
    return quads


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="ImageGen 产出的 1024x1024 PNG")
    ap.add_argument("--out", default=None, help="输出 ico 路径（默认脚本同级 icon.ico）")
    ap.add_argument("--preview", default=None, help="输出对比预览 PNG")
    ap.add_argument("--no-install", action="store_true", help="只生成不写出 ico")
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_ico = args.out or os.path.join(root, "icon.ico")

    img = Image.open(args.source)
    print("[输入] %s  mode=%s size=%s" % (args.source, img.mode, img.size))
    a = np.asarray(img.convert("RGB"))
    if (a[:, :, 3] if a.shape[-1] == 4 else None) is None:
        print("[警告] 输入无 alpha 通道 —— ImageGen 的 transparent 未生效，走自动抠图")

    box, size, radius = find_badge(img)
    print("[badge] bbox=%s 尺寸=%s 圆角半径=%d (占比 %.3f)"
          % (box, size, radius, radius / size[0]))
    verify_no_watermark(img, box)

    badge = cutout(img, box, size, radius)

    if os.path.exists(out_ico) and not args.no_install:
        old = open(out_ico, "rb").read()
        bak = out_ico + ".bak"
        open(bak, "wb").write(old)
        print("[备份] %s (%d bytes, md5=%s)" % (bak, len(old), hashlib.md5(old).hexdigest()))

    frames = [render(badge, s) for s in SIZES]

    if not args.no_install:
        frames[-1].save(out_ico, format="ICO", sizes=[(s, s) for s in SIZES],
                        append_images=frames[:-1])
        raw = open(out_ico, "rb").read()
        print("[写入] %s  %d bytes  md5=%s"
              % (out_ico, len(raw), hashlib.md5(raw).hexdigest()))
        _r, _t, cnt = struct.unpack_from("<HHH", raw, 0)
        print("       %d 个尺寸" % cnt)

    if args.preview:
        Z = 15
        cell = 32 * Z
        pad = 16
        W = pad + (cell + pad) * 4
        H = 60 + cell + pad + 30
        cv = Image.new("RGB", (W, H), (255, 255, 255))
        d = ImageDraw.Draw(cv)
        x = pad
        for s in [16, 20, 24, 32]:
            fr = render(badge, s)
            big = fr.resize((cell, cell), Image.NEAREST)
            tile = Image.new("RGB", (cell, cell), (228, 231, 236))
            tile.paste(big, (0, 0), big)
            cv.paste(tile, (x, 30))
            d.rectangle([x - 1, 29, x + cell, 30 + cell], outline=(180, 186, 196))
            d.text((x, 14), "%dpx" % s, fill=(60, 70, 90))
            x += cell + pad
        cv.save(args.preview)
        print("[预览] %s" % args.preview)

    print("\n完成。记得重新 build.ps1 打包，否则 release/ 还是旧图标。")


if __name__ == "__main__":
    main()
