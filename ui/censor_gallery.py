"""复核证据画廊 —— 缩略图平铺，**点哪张就放大哪张**。

上一版每张图都配一个「放大此帧与疑似区域」按钮，一屏只放得下一两条，
真正要看的图反而被按钮挤出去了。现在改成：

- 缩略图按网格平铺（每张下面一行时间与分数），一眼能扫十几张；
- 点图片本身即放大（整帧 + 局部并排），不占额外按钮位；
- 放大窗口里 Esc / 双击关闭，← → 切换上一张下一张。

不在这里做任何视频解码 —— 图片都是检测阶段已经落盘的像素。
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog, QGridLayout, QHBoxLayout, QLabel, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

__all__ = ["EvidenceImage", "EvidenceGallery"]


class EvidenceImage(QLabel):
    """一张证据图。点击它自身即触发放大。"""

    activated = Signal()

    def __init__(self, text: str = "", *, tile=(186, 104)):
        super().__init__(text)
        self._source = QPixmap()
        self._fallback = text
        self._tile = tile
        self.setAlignment(Qt.AlignCenter)
        if tile is None:
            self.setMinimumSize(1, 1)
            self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        else:
            self.setFixedSize(*tile)
            self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("点击放大")
        self.setProperty("cssClass", "evidence-tile")

    def set_source(self, pixmap: QPixmap, *, expand: bool = False) -> None:
        self._source = pixmap
        self._expand = expand
        self._render()

    def _render(self) -> None:
        if self._source.isNull():
            self.setText(self._fallback)
            return
        mode = Qt.KeepAspectRatio
        self.setPixmap(self._source.scaled(self.size(), mode, Qt.SmoothTransformation))

    def resizeEvent(self, event):  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._render()

    def mousePressEvent(self, event):  # noqa: N802 - Qt override
        if event.button() == Qt.LeftButton:
            self.activated.emit()
        super().mousePressEvent(event)


class EvidenceGallery(QScrollArea):
    """证据图网格。``labels`` 与候选一一对应，方便测试与外部定位。"""

    COLUMNS = 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.labels: list[EvidenceImage] = []
        self._candidates: list = []
        self._cells = []
        self._tile_width = 186
        self._columns = 0
        self.body = QWidget()
        self.grid = QGridLayout(self.body)
        self.grid.setContentsMargins(2, 2, 2, 2)
        self.grid.setHorizontalSpacing(6)
        self.grid.setVerticalSpacing(6)
        self.grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.setWidget(self.body)

    # ── 对外 ──
    def show_candidates(self, candidates) -> None:
        self._clear()
        self._candidates = list(candidates or [])
        if not self._candidates:
            hint = QLabel("没有保留证据图，可从「此视频 → 深入检查」补充取样。")
            hint.setWordWrap(True)
            self.grid.addWidget(hint, 0, 0, 1, self.COLUMNS)
            return
        for index, candidate in enumerate(self._candidates):
            self._cells.append(self._cell(index, candidate))
        self._reflow()

    def set_preferences(self, width=186, columns=0):
        self._tile_width, self._columns = int(width), int(columns)
        self._reflow()

    def _reflow(self):
        if not self._cells:
            return
        while self.grid.count():
            self.grid.takeAt(0)
        margins = self.grid.contentsMargins()
        content_width = max(1, self.viewport().width() - margins.left() - margins.right())
        tile_width = min(self._tile_width, content_width)
        for label in self.labels:
            label.setFixedSize(tile_width, max(1, tile_width * 9 // 16))
            label._render()
        available = max(1, (content_width + 6) // (tile_width + 6))
        columns = min(self._columns or available, available)
        for index, cell in enumerate(self._cells):
            self.grid.addWidget(cell, index // columns, index % columns)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow()

    def expand(self, candidate, index: int = 0) -> None:
        on_close = getattr(self, "_on_expand_close", None)
        dialog = _EvidenceDialog(self, candidate, index, self._candidates,
                                 on_navigate=self._show_at)
        dialog.exec()
        if on_close is not None:
            on_close(candidate)

    def _show_at(self, index: int) -> None:
        """放大窗口里按左右键切换时，同步高亮当前格。"""
        if 0 <= index < len(self.labels):
            self.labels[index].setFocus(Qt.OtherFocusReason)

    # ── 内部 ──
    def _cell(self, index: int, candidate) -> QWidget:
        cell = QWidget()
        box = QVBoxLayout(cell)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(2)

        image = EvidenceImage("缩略图缺失，请重新检查补充",
                              tile=(self._tile_width, max(72, self._tile_width * 9 // 16)))
        self.labels.append(image)
        self._set_image(image, candidate.thumb_path)
        image.activated.connect(lambda c=candidate, i=index: self.expand(c, i))
        box.addWidget(image)

        caption = QLabel(self._caption(index, candidate))
        caption.setWordWrap(True)
        caption.setMinimumWidth(1)
        caption.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        caption.setProperty("cssClass", "muted-label")
        caption.setToolTip(candidate.thumb_path or "无缩略图")
        box.addWidget(caption)
        return cell

    @staticmethod
    def _caption(index: int, candidate) -> str:
        if candidate.score <= 0:
            return f"{index + 1} · {candidate.hms} · 覆盖取样"
        block = f" · 块{candidate.block_size}px" if candidate.block_size else ""
        return f"{index + 1} · {candidate.hms} · {candidate.score:.1f}分{block}"

    def _clear(self) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.labels.clear()
        self._cells.clear()
        self._candidates = []

    @staticmethod
    def _set_image(label, path: str) -> None:
        pixmap = QPixmap(path) if path and os.path.isfile(path) else QPixmap()
        label.set_source(pixmap)


class _EvidenceDialog(QDialog):
    """放大窗口：整帧 + 局部并排，左右键翻页，Esc / 双击关闭。"""

    def __init__(self, parent, candidate, index: int, candidates,
                 *, on_navigate=None):
        super().__init__(parent)
        self._candidates = list(candidates or [candidate])
        self._index = index
        self._on_navigate = on_navigate
        self._build()
        screen = self.screen()
        available = screen.availableGeometry() if screen else None
        self.resize(min(1180, max(1, available.width() - 32)) if available else 1180,
                    min(720, max(1, available.height() - 64)) if available else 720)
        self._load(index)

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        self.title = QLabel()
        self.title.setWordWrap(True)
        outer.addWidget(self.title)
        row = QHBoxLayout()
        self.frame = EvidenceImage("整帧：无图像", tile=None)
        self.crop = EvidenceImage("局部：无图像", tile=None)
        for image in (self.frame, self.crop):
            image.setCursor(Qt.ArrowCursor)
            image.setToolTip("双击关闭 · 方向键切换")
        row.addWidget(self.frame, 3)
        row.addWidget(self.crop, 2)
        outer.addLayout(row, 1)
        hint = QLabel("← → 切换上一张 / 下一张 · Esc 或双击关闭 · 方框是算法候选区域，不代表确认")
        hint.setWordWrap(True)
        hint.setProperty("cssClass", "muted-label")
        outer.addWidget(hint)

    def _load(self, index: int) -> None:
        index = max(0, min(index, len(self._candidates) - 1))
        self._index = index
        candidate = self._candidates[index]
        self.setWindowTitle(f"证据帧 {candidate.hms}（{index + 1}/{len(self._candidates)}）")
        self.title.setText(
            f"{index + 1} / {len(self._candidates)} · {candidate.hms} · "
            + (f"疑似区域 · 结构分 {candidate.score:.1f}"
               + (f" · 块约 {candidate.block_size}px" if candidate.block_size else "")
               if candidate.score > 0 else "覆盖取样 · 未检出候选"))
        self._assign(self.frame, candidate.thumb_path, "整帧")
        self._assign(self.crop, getattr(candidate, "crop_path", ""), "局部")
        if self._on_navigate is not None:
            self._on_navigate(index)

    @staticmethod
    def _assign(label: QLabel, path: str, fallback: str) -> None:
        pixmap = QPixmap(path) if path and os.path.isfile(path) else QPixmap()
        if isinstance(label, EvidenceImage):
            label._fallback = fallback + "：无图像"
            label.set_source(pixmap)
            return
        if pixmap.isNull():
            label.setText(fallback + "：无图像")
            return
        label.setPixmap(pixmap.scaled(label.size(), Qt.KeepAspectRatio,
                                      Qt.SmoothTransformation))

    def keyPressEvent(self, event):  # noqa: N802 - Qt override
        key = event.key()
        if key == Qt.Key_Escape:
            self.reject()
            return
        if key in (Qt.Key_Left, Qt.Key_Up):
            self._load(self._index - 1)
            return
        if key in (Qt.Key_Right, Qt.Key_Down):
            self._load(self._index + 1)
            return
        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event):  # noqa: N802 - Qt override
        self.accept()
