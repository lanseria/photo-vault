#!/usr/bin/env python3
"""生成 1024×1024 App Icon（深色取景器底 + 琥珀色照片卡 + 同步箭头）。

用代码画而不是手搓图：以后改配色/形状只改这个脚本。
    python3 tools/make_app_icon.py
产物：PhotoVaultSync/Resources/Assets.xcassets/AppIcon.appiconset/AppIcon-1024.png
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

SIZE = 1024
SS = 3                      # 超采样：先画 3072，最后缩到 1024
S = SIZE * SS
OUT = (
    Path(__file__).resolve().parent.parent
    / "PhotoVaultSync/Resources/Assets.xcassets/AppIcon.appiconset/AppIcon-1024.png"
)

BG_TOP = np.array([13, 17, 24], dtype=np.float32)
BG_BOTTOM = np.array([26, 32, 45], dtype=np.float32)
AMBER_TOP = np.array([255, 191, 106], dtype=np.float32)
AMBER_BOTTOM = np.array([247, 133, 26], dtype=np.float32)
CREAM = (255, 249, 242)
INK = (14, 18, 26)


def vertical_gradient(size: tuple[int, int], top, bottom) -> Image.Image:
    h, w = size
    t = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None]
    row = top[None, :] * (1 - t) + bottom[None, :] * t
    arr = np.repeat(row[:, None, :], w, axis=1).astype(np.uint8)
    return Image.fromarray(arr, "RGB")


def background() -> Image.Image:
    img = vertical_gradient((S, S), BG_TOP, BG_BOTTOM).convert("RGBA")
    # 中心柔光
    yy, xx = np.mgrid[0:S, 0:S].astype(np.float32)
    d = np.hypot(xx - S / 2, yy - S / 2) / (S / 2)
    glow = np.clip(1 - d / 1.15, 0, 1) ** 2 * 30
    arr = np.array(img, dtype=np.float32)
    arr[..., 0] = np.clip(arr[..., 0] + glow, 0, 255)
    arr[..., 1] = np.clip(arr[..., 1] + glow * 0.85, 0, 255)
    arr[..., 2] = np.clip(arr[..., 2] + glow * 0.55, 0, 255)
    img = Image.fromarray(arr.astype(np.uint8), "RGBA")

    # 取景器网格（很淡）
    grid = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grid)
    step = S // 10
    for i in range(1, 10):
        alpha = 16 if i != 5 else 22
        gd.line([(i * step, 0), (i * step, S)], fill=(255, 255, 255, alpha), width=2 * SS)
        gd.line([(0, i * step), (S, i * step)], fill=(255, 255, 255, alpha), width=2 * SS)
    img.alpha_composite(grid)
    return img


def photo_card() -> Image.Image:
    """琥珀色照片卡：渐变底 + 圆角裁切 + 太阳 + 两层山。"""
    box = (int(S * 0.17), int(S * 0.155), int(S * 0.83), int(S * 0.705))
    radius = int(S * 0.075)
    w, h = box[2] - box[0], box[3] - box[1]

    card = vertical_gradient((h, w), AMBER_TOP, AMBER_BOTTOM).convert("RGBA")
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    layer.paste(card, (box[0], box[1]))
    d = ImageDraw.Draw(layer)

    # 太阳
    sun = (int(S * 0.335), int(S * 0.285), int(S * 0.055))
    d.ellipse((sun[0] - sun[2], sun[1] - sun[2], sun[0] + sun[2], sun[1] + sun[2]),
              fill=(255, 252, 245, 240))

    # 两层山（远山浅、近山深，用白色不同透明度叠出层次）
    def mountains(base_frac: float, peaks: list[tuple[float, float]], alpha: int) -> None:
        base_y = box[1] + int(h * base_frac)
        pts = [(box[0] - 10, box[3] + 10), (box[0] - 10, base_y)]
        for fx, fy in peaks:
            pts.append((box[0] + int(w * fx), box[1] + int(h * fy)))
        pts += [(box[2] + 10, base_y), (box[2] + 10, box[3] + 10)]
        d.polygon(pts, fill=(255, 255, 255, alpha))

    mountains(0.52, [(0.30, 0.44), (0.46, 0.57), (0.60, 0.44), (0.74, 0.56), (0.88, 0.47)], 128)
    mountains(0.66, [(0.22, 0.52), (0.36, 0.66), (0.50, 0.52), (0.64, 0.63), (0.80, 0.50)], 96)

    # 圆角裁切（连山一起裁）
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=radius, fill=255)
    layer.putalpha(Image.composite(layer.getchannel("A"), Image.new("L", (S, S), 0), mask))

    # 卡片描边 + 投影
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (box[0], box[1] + int(S * 0.018), box[2], box[3] + int(S * 0.018)),
        radius=radius, fill=(0, 0, 0, 170))
    shadow = shadow.filter(ImageFilter.GaussianBlur(int(S * 0.02)))

    d = ImageDraw.Draw(layer)
    d.rounded_rectangle(box, radius=radius, outline=(255, 255, 255, 110), width=3 * SS)

    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.alpha_composite(shadow)
    out.alpha_composite(layer)
    return out


def sync_badge() -> Image.Image:
    """右下角圆形徽章：两段圆弧 + 切向三角箭头。"""
    box = (int(S * 0.60), int(S * 0.665), int(S * 0.855), int(S * 0.92))
    badge = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(badge)

    cx, cy = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
    R = (box[2] - box[0]) // 2
    d.ellipse((cx - R, cy - R, cx + R, cy + R), fill=INK + (255,),
              outline=(255, 255, 255, 55), width=3 * SS)

    arc_r = int(R * 0.58)
    w = int(R * 0.21)
    bbox = (cx - arc_r, cy - arc_r, cx + arc_r, cy + arc_r)

    def arrow_head(angle_deg: float) -> None:
        a = math.radians(angle_deg)
        base = (cx + arc_r * math.cos(a), cy + arc_r * math.sin(a))
        tang = (-math.sin(a), math.cos(a))       # 屏幕坐标：角度增大 = 顺时针
        radial = (math.cos(a), math.sin(a))
        tip = (base[0] + tang[0] * w * 1.45, base[1] + tang[1] * w * 1.45)
        b1 = (base[0] + radial[0] * w * 1.15, base[1] + radial[1] * w * 1.15)
        b2 = (base[0] - radial[0] * w * 1.15, base[1] - radial[1] * w * 1.15)
        d.polygon([tip, b1, b2], fill=CREAM + (255,))

    d.arc(bbox, start=205, end=332, fill=CREAM + (255,), width=w)
    d.arc(bbox, start=25, end=152, fill=CREAM + (255,), width=w)
    arrow_head(332)
    arrow_head(152)
    return badge


def main() -> None:
    img = background()
    img.alpha_composite(photo_card())
    img.alpha_composite(sync_badge())
    icon = img.convert("RGB").resize((SIZE, SIZE), Image.LANCZOS)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    icon.save(OUT, "PNG")
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
