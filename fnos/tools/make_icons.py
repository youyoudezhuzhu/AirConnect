#!/usr/bin/env python3
"""生成 AirConnect 的飞牛应用图标（纯标准库，不依赖 Pillow / ImageMagick）。

    python3 tools/make_icons.py [输出根目录，默认 fnos/]

产出：::

    <root>/ICON.PNG                     64x64
    <root>/ICON_256.PNG                 256x256
    <root>/app/ui/images/icon_64.png    64x64
    <root>/app/ui/images/icon_256.png   256x256

图形语义：左边是 AirPlay 图标（AirPlay 源），中间箭头，右边是 Cast/投屏图标
（DLNA / Sonos / Chromecast 目标）——「AirPlay 桥接到这些设备」。

实现方式：对每个像素做 SS×SS 超采样，颜色由一组解析形状（圆角矩形、圆、
三角、圆环扇形）按解析距离合成，因此不依赖任何绘图库也有平滑边缘。
"""

from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path

SS = 4                      # 每个像素的超采样边长
SIZES = (64, 256)

# 渐变：左上紫 → 右下青，与 Air2DLNA（蓝）明显区分
GRAD_A = (0x5B, 0x45, 0xE8)
GRAD_B = (0x18, 0xC2, 0xE6)

CORNER_RADIUS = 0.225


# ------------------------------------------------------------------ 形状工具
def rrect_sdf(px, py, x0, y0, x1, y1, r):
    """圆角矩形的有符号距离：<0 在内部。"""
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    hw, hh = (x1 - x0) / 2.0, (y1 - y0) / 2.0
    dx = abs(px - cx) - (hw - r)
    dy = abs(py - cy) - (hh - r)
    ax = dx if dx > 0 else 0.0
    ay = dy if dy > 0 else 0.0
    outside = math.hypot(ax, ay)
    inside = dx if dx > dy else dy
    if inside > 0:
        inside = 0.0
    return outside + inside - r


def point_in_triangle(px, py, a, b, c):
    def cross(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])
    d1 = cross(a, b, (px, py))
    d2 = cross(b, c, (px, py))
    d3 = cross(c, a, (px, py))
    has_neg = d1 < 0 or d2 < 0 or d3 < 0
    has_pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (has_neg and has_pos)


def in_arc(px, py, cx, cy, r_in, r_out, a0, a1):
    """圆环扇形（角度制，0°=右，逆时针为正；屏幕坐标 y 向下）。"""
    dx, dy = px - cx, py - cy
    dist = math.hypot(dx, dy)
    if dist < r_in or dist > r_out:
        return False
    ang = math.degrees(math.atan2(-dy, dx))
    if a0 <= a1:
        return a0 <= ang <= a1
    return ang >= a0 or ang <= a1


# ------------------------------------------------------------------ 颜色合成
def sample(x, y):
    """返回 (r, g, b, a)，分量 0..1；x/y 为 0..1 的画布坐标。"""
    # ---- 背景：圆角矩形 + 对角渐变，圆角外完全透明
    if rrect_sdf(x, y, 0.0, 0.0, 1.0, 1.0, CORNER_RADIUS) > 0:
        return (0.0, 0.0, 0.0, 0.0)

    t = min(1.0, max(0.0, (x + y) / 2.0))
    r = (GRAD_A[0] + (GRAD_B[0] - GRAD_A[0]) * t) / 255.0
    g = (GRAD_A[1] + (GRAD_B[1] - GRAD_A[1]) * t) / 255.0
    b = (GRAD_A[2] + (GRAD_B[2] - GRAD_A[2]) * t) / 255.0

    # ---- 底部装饰波纹（半透明白）
    wave1 = 0.805 + 0.042 * math.sin(2 * math.pi * (x * 1.05 + 0.10))
    wave2 = 0.885 + 0.036 * math.sin(2 * math.pi * (x * 1.45 + 0.62))
    for wy, alpha in ((wave2, 0.16), (wave1, 0.11)):
        if y > wy:
            r += (1.0 - r) * alpha
            g += (1.0 - g) * alpha
            b += (1.0 - b) * alpha

    # ---- 左侧：AirPlay 图标（圆角矩形描边 + 底部上指三角）
    stroke = 0.043
    if abs(rrect_sdf(x, y, 0.065, 0.170, 0.415, 0.530, 0.062)) <= stroke / 2.0:
        return (1.0, 1.0, 1.0, 1.0)
    if point_in_triangle(x, y, (0.240, 0.373), (0.122, 0.590), (0.358, 0.590)):
        return (1.0, 1.0, 1.0, 1.0)

    # ---- 中间：指向右侧的箭头（表示"桥接/转发"）
    if 0.437 <= x <= 0.545 and abs(y - 0.362) <= 0.016:
        return (1.0, 1.0, 1.0, 1.0)
    if point_in_triangle(x, y, (0.588, 0.362), (0.524, 0.314), (0.524, 0.410)):
        return (1.0, 1.0, 1.0, 1.0)

    # ---- 右侧：Cast / 投屏图标（圆角矩形描边 + 左下角点与同心弧）
    if abs(rrect_sdf(x, y, 0.615, 0.178, 0.945, 0.548, 0.058)) <= 0.040 / 2.0:
        return (1.0, 1.0, 1.0, 1.0)
    if math.hypot(x - 0.661, y - 0.497) <= 0.021:
        return (1.0, 1.0, 1.0, 1.0)
    # 角度 0°=右、90°=上：弧只在屏幕左下角朝右上张开，绝不会溢出到矩形外
    if in_arc(x, y, 0.652, 0.508, 0.052, 0.076, 0.0, 90.0):
        return (1.0, 1.0, 1.0, 1.0)
    if in_arc(x, y, 0.652, 0.508, 0.094, 0.118, 0.0, 90.0):
        return (1.0, 1.0, 1.0, 1.0)

    return (r, g, b, 1.0)


def render(size: int) -> bytes:
    """渲染 RGBA 像素数据。"""
    step = 1.0 / (size * SS)
    samples = SS * SS
    rows = []
    for py in range(size):
        row = bytearray()
        base_y = (py * SS + 0.5) * step
        for px in range(size):
            base_x = (px * SS + 0.5) * step
            acc_r = acc_g = acc_b = acc_a = 0.0
            for sy in range(SS):
                y = base_y + sy * step
                for sx in range(SS):
                    x = base_x + sx * step
                    sr, sg, sb, sa = sample(x, y)
                    acc_r += sr * sa
                    acc_g += sg * sa
                    acc_b += sb * sa
                    acc_a += sa
            alpha = acc_a / samples
            if alpha <= 0.0001:
                row += bytes((0, 0, 0, 0))
                continue
            # 预乘颜色还原（避免透明边缘出现黑边）
            row += bytes((
                max(0, min(255, round(acc_r / acc_a * 255))),
                max(0, min(255, round(acc_g / acc_a * 255))),
                max(0, min(255, round(acc_b / acc_a * 255))),
                max(0, min(255, round(alpha * 255))),
            ))
        rows.append(b"\x00" + bytes(row))
    return b"".join(rows)


def write_png(path: Path, size: int, raw: bytes) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    # 8 bit / colortype 6（RGBA）
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "fnos")
    cache = {}
    for size in SIZES:
        print(f"渲染 {size}x{size} …", flush=True)
        cache[size] = render(size)
    targets = [
        (root / "ICON.PNG", 64),
        (root / "ICON_256.PNG", 256),
        (root / "app/ui/images/icon_64.png", 64),
        (root / "app/ui/images/icon_256.png", 256),
    ]
    for path, size in targets:
        write_png(path, size, cache[size])
        print(f"  wrote {path} ({size}x{size}, {path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
