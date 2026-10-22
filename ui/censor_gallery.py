"""Scrollable, expandable review evidence without video decoding on the GUI thread."""
import os
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                              QScrollArea, QDialog, QSizePolicy)


class EvidenceImage(QLabel):
    """Fit evidence into a narrow review pane without clipping its pixels."""
    def __init__(self, text):
        super().__init__(text)
        self._source = QPixmap()
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumWidth(80)
        self.setFixedHeight(146)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)

    def set_source(self, pixmap):
        self._source = pixmap
        self._render()

    def _render(self):
        if not self._source.isNull():
            self.setPixmap(self._source.scaled(self.size(),Qt.KeepAspectRatio,Qt.SmoothTransformation))

    def resizeEvent(self, event):  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._render()


class EvidenceGallery(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.labels = []
        self.body = QWidget()
        self.layout = QVBoxLayout(self.body)
        self.layout.setAlignment(Qt.AlignTop)
        self.setWidget(self.body)

    def show_candidates(self, candidates):
        while self.layout.count():
            item = self.layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.labels.clear()
        if not candidates:
            hint = QLabel('没有保留证据图，可从“此视频 → 深入检查”补充取样。')
            hint.setWordWrap(True)
            self.layout.addWidget(hint)
        for index, candidate in enumerate(candidates):
            card = QWidget()
            box = QVBoxLayout(card)
            title = QLabel(f'{index + 1} · {candidate.hms} · ' +
                           (f'疑似区域 · 结构分 {candidate.score:.1f} · 块约 {candidate.block_size}px'
                            if candidate.score > 0 else '覆盖取样 · 未检出候选'))
            title.setWordWrap(True)
            box.addWidget(title)
            images = QHBoxLayout()
            label = EvidenceImage('缩略图缺失，请重新检查补充')
            self.labels.append(label)
            self._set_image(label, candidate.thumb_path)
            images.addWidget(label,3)
            detail = EvidenceImage('区域细节')
            self._set_image(detail, getattr(candidate, 'crop_path', ''))
            images.addWidget(detail,2)
            box.addLayout(images)
            open_button = QPushButton('放大此帧与疑似区域')
            open_button.clicked.connect(lambda _, c=candidate: self.expand(c))
            box.addWidget(open_button)
            self.layout.addWidget(card)

    @staticmethod
    def _set_image(label, path):
        if path and os.path.isfile(path):
            pixmap = QPixmap(path)
            if not pixmap.isNull():
                if isinstance(label,EvidenceImage):
                    label.set_source(pixmap)
                else:
                    label.setPixmap(pixmap.scaled(label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def expand(self, candidate):
        dialog = QDialog(self)
        dialog.setWindowTitle(f'证据帧 {candidate.hms} · 方框是算法候选区域，不代表确认')
        dialog.resize(1180, 720)
        layout = QHBoxLayout(dialog)
        for path in (candidate.thumb_path, getattr(candidate, 'crop_path', '')):
            label = QLabel('无图像')
            label.setAlignment(Qt.AlignCenter)
            label.setMinimumSize(460, 500)
            self._set_image(label, path)
            layout.addWidget(label)
        dialog.exec()
