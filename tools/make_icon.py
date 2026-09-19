"""生成应用图标 `assets/icon.ico`（多尺寸）+ `assets/icon.png`。

设计上用项目自身的依赖 PySide6 来渲染（不引入 Pillow），
因此这个脚本在任何已装好依赖的机器上都能重复运行 ——
二进制图标不该是唯一真相来源。

图形：深色圆角底板 + 胶片孔位 + 播放三角，主色取主题 accent (#4FC3F7)。
小尺寸（≤24px）单独画简化版（去掉胶片孔、放大三角），
而不是把大图等比缩小 —— 缩到 16px 只会糊成一团。

用法：
    python tools/make_icon.py                 # 生成 ico + png
    python tools/make_icon.py --preview       # 额外输出各尺寸 PNG 便于肉眼检查
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.console import force_utf8_stdout  # noqa: E402

force_utf8_stdout()

ICON_SIZES = (256, 128, 64, 48, 40, 32, 24, 20, 16)

BG_TOP = "#1F2937"
BG_BOTTOM = "#0D1117"
ACCENT = "#4FC3F7"
ACCENT_DEEP = "#0288D1"
GOLD = "#FFC53D"


def _render(size: int):
    """渲染指定边长的图标，返回 QImage（ARGB32）。"""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import (
        QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPen,
    )

    ss = 4 if size <= 64 else 2          # 超采样倍数，保证边缘平滑
    px = size * ss
    img = QImage(px, px, QImage.Format_ARGB32)
    img.fill(Qt.transparent)

    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)

    # ── 底板：圆角方块 + 上浅下深渐变 ──
    radius = px * 0.22
    plate = QPainterPath()
    plate.addRoundedRect(QRectF(0, 0, px, px), radius, radius)
    grad = QLinearGradient(0, 0, 0, px)
    grad.setColorAt(0.0, QColor(BG_TOP))
    grad.setColorAt(1.0, QColor(BG_BOTTOM))
    p.fillPath(plate, grad)

    # 描边（浅色细边，让图标在深色背景上也有轮廓）
    pen = QPen(QColor(255, 255, 255, 38))
    pen.setWidthF(max(1.0, px * 0.012))
    p.setPen(pen)
    p.drawPath(plate)

    simple = size <= 24

    if not simple:
        # ── 胶片孔位：左右两列小圆角矩形 ──
        hole_w = px * 0.075
        hole_h = px * 0.055
        gap = px * 0.115
        margin = px * 0.10
        hole_color = QColor(255, 255, 255, 46)
        p.setPen(Qt.NoPen)
        p.setBrush(hole_color)
        count = 4
        total_h = count * hole_h + (count - 1) * (gap - hole_h)
        top = (px - total_h) / 2
        for i in range(count):
            y = top + i * gap
            for x in (margin, px - margin - hole_w):
                p.drawRoundedRect(
                    QRectF(x, y, hole_w, hole_h), hole_h * 0.35, hole_h * 0.35)

    # ── 播放三角（带柔性渐变）──
    tri = QPainterPath()
    if simple:
        cx, cy = px * 0.50, px * 0.50
        half = px * 0.28
    else:
        cx, cy = px * 0.535, px * 0.50
        half = px * 0.225
    tri.moveTo(QPointF(cx - half * 0.62, cy - half))
    tri.lineTo(QPointF(cx - half * 0.62, cy + half))
    tri.lineTo(QPointF(cx + half * 0.86, cy))
    tri.closeSubpath()

    tri_grad = QLinearGradient(0, cy - half, 0, cy + half)
    tri_grad.setColorAt(0.0, QColor(ACCENT))
    tri_grad.setColorAt(1.0, QColor(ACCENT_DEEP))
    p.setPen(Qt.NoPen)
    p.fillPath(tri, tri_grad)

    if not simple:
        # ── 右上角小光点：区分「库」的记号 ──
        p.setBrush(QColor(GOLD))
        r = px * 0.075
        p.drawEllipse(QPointF(px * 0.775, px * 0.225), r, r)

    p.end()
    return img.scaled(size, size, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)


def _png_bytes(image) -> bytes:
    """把 QImage 编码成 PNG 字节。"""
    from PySide6.QtCore import QBuffer, QIODevice

    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    image.save(buf, "PNG")
    data = bytes(buf.data())
    buf.close()
    return data


def write_ico(images: list[tuple[int, bytes]], out: Path) -> None:
    """把所有尺寸的 PNG 打进一个 ICO（Vista+ 支持 PNG 压缩条目）。"""
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    entries = b""
    offset = 6 + 16 * count
    payload = b""
    for size, data in images:
        dim = 0 if size >= 256 else size
        entries += struct.pack(
            "<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        payload += data
        offset += len(data)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(header + entries + payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成应用图标")
    parser.add_argument("--out-dir", default=str(ROOT / "assets"))
    parser.add_argument("--preview", action="store_true",
                        help="额外导出各尺寸 PNG 到 assets/preview/ 以便肉眼检查")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])  # noqa: F841 — QImage 需要 QGuiApplication

    rendered: list[tuple[int, bytes]] = []
    for size in ICON_SIZES:
        img = _render(size)
        data = _png_bytes(img)
        rendered.append((size, data))
        if args.preview:
            preview = out_dir / "preview"
            preview.mkdir(parents=True, exist_ok=True)
            (preview / f"icon_{size:03d}.png").write_bytes(data)

    # 单文件 PNG（README / Linux .desktop 用）
    (out_dir / "icon.png").write_bytes(dict(rendered)[256])
    ico = out_dir / "icon.ico"
    write_ico(rendered, ico)

    print(f"已生成 {ico}  ({len(rendered)} 个尺寸: "
          f"{', '.join(str(s) for s in ICON_SIZES)})")
    print(f"已生成 {out_dir / 'icon.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
