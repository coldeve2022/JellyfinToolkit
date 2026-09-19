"""通用组件 — 可拖拽列表、日志面板、条形图等复用组件。"""
from __future__ import annotations

import os
import subprocess

from PySide6.QtCore import Qt, Signal, QUrl, QMimeData, QRectF
from PySide6.QtGui import QColor, QDrag, QPainter, QPixmap
from PySide6.QtWidgets import (
    QListWidget, QListWidgetItem, QTableWidget, QTableWidgetItem,
    QTextEdit, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QHeaderView,
    QAbstractItemView, QMenu, QFrame, QProgressBar, QStyledItemDelegate,
    QStyle,
)

from ui import theme


# ───────────────── 可拖拽出入的列表 ─────────────────

class DragDropListWidget(QListWidget):
    """支持外部文件拖入、内部文件拖出（到资源管理器）的通用列表组件。

    内置能力：
    - Delete 键 / 右键菜单删除选中项
    - 右键菜单打开所在文件夹
    """

    file_dropped = Signal(str)          # 拖入时发射文件路径
    files_dropped = Signal(list)        # 批量拖入

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setDefaultDropAction(Qt.CopyAction)
        self.setDragDropMode(QAbstractItemView.DragOnly)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    # ── 拖拽 ──
    def startDrag(self, supportedActions):
        """向外拖拽：将选中文件的路径作为 URL 传出（可拖到资源管理器或 bat）。"""
        items = self.selectedItems()
        if not items:
            return
        mime = QMimeData()
        urls = [QUrl.fromLocalFile(it.data(Qt.UserRole)) for it in items if it.data(Qt.UserRole)]
        if not urls:
            super().startDrag(supportedActions)
            return
        mime.setUrls(urls)
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(supportedActions or Qt.CopyAction)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        # 收集路径（不抛异常到 Qt 层）
        try:
            paths = []
            for url in event.mimeData().urls():
                p = os.path.normpath(url.toLocalFile())
                # 拦截超长路径（触发 Windows MAX_PATH 会抛异常/崩溃），静默跳过
                if not p or len(p) > 240:
                    continue
                if os.path.exists(p):
                    paths.append(p)
            if not paths:
                event.ignore()
                return
            event.acceptProposedAction()
            # 关键：延迟到拖拽释放事件完全结束后再 emit。
            # 在 drop 回调内直接向 QListWidget 插 row（下游 add_item）会让
            # Qt 拖拽状态机遍历 model 时崩溃（C++ 层），原 tkinterdnd2 无此问题，
            # 故整合后需用 singleShot 延迟到事件堆栈弹出。
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self, lambda: self.files_dropped.emit(paths))
        except Exception:
            # 任何异常都不应冒泡到 Qt 拖拽引擎，否则 PySide6 会 qFatal 崩溃
            event.ignore()

    # ── 删除与右键菜单 ──
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete:
            self._remove_selected()
        else:
            super().keyPressEvent(event)

    def _show_context_menu(self, pos):
        item = self.itemAt(pos)
        if not item:
            return
        fp = item.data(Qt.UserRole)
        menu = QMenu(self)
        act_open = menu.addAction("打开所在文件夹")
        menu.addSeparator()
        act_del = menu.addAction("从列表移除 (Delete)")
        action = menu.exec(self.mapToGlobal(pos))
        if action == act_open and fp:
            subprocess.Popen(f'explorer /select,"{os.path.normpath(fp)}"')
        elif action == act_del:
            self._remove_selected()

    def _remove_selected(self) -> None:
        """删除所有选中项。"""
        rows = sorted({self.row(it) for it in self.selectedItems()}, reverse=True)
        for r in rows:
            self.takeItem(r)

    # ── 数据管理 ──
    def add_item(self, text: str, file_path: str) -> None:
        """添加带路径数据的列表项，自动去重。"""
        for i in range(self.count()):
            if self.item(i).data(Qt.UserRole) == file_path:
                return
        item = QListWidgetItem(text)
        item.setData(Qt.UserRole, file_path)
        item.setToolTip(file_path)
        self.addItem(item)

    def get_all_paths(self) -> list[str]:
        """获取列表中所有项的完整路径。"""
        return [self.item(i).data(Qt.UserRole) for i in range(self.count())]


# ───────────────── 可拖拽表格（有码检测专用） ─────────────────

class DragDropTableWidget(QTableWidget):
    """支持右键菜单、拖出文件、多选的表格组件。"""

    def __init__(self, columns: list[str], parent=None):
        super().__init__(parent)
        self.setColumnCount(len(columns))
        self.setHorizontalHeaderLabels(columns)
        self.horizontalHeader().setStretchLastSection(True)
        for i in range(len(columns) - 1):
            self.horizontalHeader().setSectionResizeMode(i, QHeaderView.Stretch)
        self.horizontalHeader().setSectionResizeMode(len(columns) - 1, QHeaderView.ResizeToContents)
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setDefaultDropAction(Qt.CopyAction)
        self.setDragDropMode(QAbstractItemView.DragOnly)
        self.setSortingEnabled(True)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)

    def _context_menu(self, pos):
        item = self.itemAt(pos)
        if not item:
            return
        fp = item.data(Qt.UserRole)
        if not fp:
            return
        menu = QMenu(self)
        act_open = menu.addAction("打开所在文件夹")
        menu.addSeparator()
        act_del = menu.addAction("从列表移除 (Delete)")
        action = menu.exec(self.mapToGlobal(pos))
        if action == act_open:
            subprocess.Popen(f'explorer /select,"{os.path.normpath(fp)}"')
        elif action == act_del:
            rows = sorted({it.row() for it in self.selectedItems()}, reverse=True)
            for r in rows:
                self.removeRow(r)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete:
            rows = sorted({it.row() for it in self.selectedItems()}, reverse=True)
            for r in rows:
                self.removeRow(r)
        else:
            super().keyPressEvent(event)

    def mimeData(self, items):
        mime = QMimeData()
        urls = []
        seen = set()
        for it in items:
            fp = it.data(Qt.UserRole)
            if fp and fp not in seen:
                urls.append(QUrl.fromLocalFile(fp))
                seen.add(fp)
        mime.setUrls(urls)
        return mime

    def add_file_row(self, filename: str, date_str: str, file_path: str) -> None:
        """添加一行，自动去重。"""
        for r in range(self.rowCount()):
            if self.item(r, 0) and self.item(r, 0).data(Qt.UserRole) == file_path:
                return
        self.setSortingEnabled(False)
        r = self.rowCount()
        self.insertRow(r)
        name_item = QTableWidgetItem(filename)
        date_item = QTableWidgetItem(date_str)
        name_item.setData(Qt.UserRole, file_path)
        date_item.setData(Qt.UserRole, file_path)
        name_item.setToolTip(file_path)
        name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
        date_item.setFlags(date_item.flags() & ~Qt.ItemIsEditable)
        self.setItem(r, 0, name_item)
        self.setItem(r, 1, date_item)
        self.setSortingEnabled(True)

    def clear_all(self) -> None:
        self.setRowCount(0)

    def gather_all_paths(self) -> set[str]:
        """获取表中所有唯一文件路径。"""
        paths = set()
        for r in range(self.rowCount()):
            it = self.item(r, 0)
            if it and it.data(Qt.UserRole):
                paths.add(it.data(Qt.UserRole))
        return paths


# ───────────────── 日志面板 ─────────────────

class LogPanel(QTextEdit):
    """只读日志面板，自动滚到底部。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)

    def log(self, text: str) -> None:
        self.append(text)
        sb = self.verticalScrollBar()
        sb.setValue(sb.maximum())

    def log_info(self, text: str) -> None:
        self.log(f'<span style="color:{theme.color("text_secondary")};">{text}</span>')

    def log_success(self, text: str) -> None:
        self.log(f'<span style="color:{theme.color("success")};">{text}</span>')

    def log_warning(self, text: str) -> None:
        self.log(f'<span style="color:{theme.color("warning")};">{text}</span>')

    def log_error(self, text: str) -> None:
        self.log(f'<span style="color:{theme.color("danger")};">{text}</span>')


# ───────────────── 提示卡片 ─────────────────

class InfoCard(QLabel):
    """带背景色的信息提示卡片（样式由全局 QSS 的 cssClass="info-card" 控制）。"""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumHeight(60)
        self.setProperty("cssClass", "info-card")


# ───────────────── 双列拖拽面板（替代用） ─────────────────

class DualDropPanel(QWidget):
    """左/右两个 DragDropListWidget 的布局面板。"""

    def __init__(self, left_title: str, right_title: str, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        left_box = QVBoxLayout()
        left_box.addWidget(QLabel(left_title))
        self.left_list = DragDropListWidget()
        left_box.addWidget(self.left_list)

        right_box = QVBoxLayout()
        right_box.addWidget(QLabel(right_title))
        self.right_list = DragDropListWidget()
        right_box.addWidget(self.right_list)

        layout.addLayout(left_box)
        layout.addLayout(right_box)


# ───────────────── 横向条形图（画像可视化） ─────────────────

class HBarChart(QWidget):
    """一行行"名称 + 进度条 + 数值"的横向条形图，适合 Top 排名可视化。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[QProgressBar] = []
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)

    def clear(self) -> None:
        while self._layout.count():
            it = self._layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self._rows = []

    def add(self, label: str, value: float, maxv: float,
            sub: str = "", color_key: str = "accent") -> None:
        """value: 当前值；maxv: 最大值用于归一化；sub: 右侧附加说明。"""
        row = QFrame()
        row.setObjectName("barRow")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lbl = QLabel(label)
        lbl.setObjectName("barLabel")
        lbl.setMaximumWidth(160)
        lbl.setMinimumWidth(110)
        lay.addWidget(lbl)
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        m = maxv or 1
        bar.setValue(int(value / m * 100))
        col = theme.color(color_key)
        bar.setStyleSheet(f"""
            QProgressBar {{
                background: {theme.color('bg_input')};
                border: 1px solid {theme.color('border')};
                border-radius: 5px; height: 14px;
            }}
            QProgressBar::chunk {{
                background: {col}; border-radius: 4px;
            }}""")
        lay.addWidget(bar, 1)
        val = QLabel(sub or f"{value:g}")
        val.setObjectName("barValue")
        val.setMinimumWidth(70)
        val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(val)
        self._layout.addWidget(row)
        self._rows.append(bar)


# ───────────────── 表格占比条 delegate ─────────────────

class TableBarDelegate(QStyledItemDelegate):
    """在单元格中绘制一个横向占比条 + 文字。项需在 Qt.UserRole 存入 0-100 数值。"""

    def paint(self, painter: QPainter, option, index) -> None:
        value = index.data(Qt.UserRole)
        if isinstance(value, (int, float)):
            # 背景
            painter.fillRect(option.rect,
                             QColor(theme.color("bg_input")))
            ratio = max(0.0, min(1.0, float(value) / 100.0))
            w = int(option.rect.width() * ratio)
            rc = QRectF(option.rect.left(), option.rect.top() + 2,
                        w, option.rect.height() - 4)
            painter.fillRect(rc, QColor(theme.color("accent")))
            # 文字
            text = index.data(Qt.DisplayRole)
            painter.setPen(QColor(theme.color("text_primary")))
            painter.drawText(option.rect, Qt.AlignCenter, str(text))
        else:
            super().paint(painter, option, index)


class NumericItem(QTableWidgetItem):
    """按数值排序的表项（UserRole 存数值；解决按字符串排序的 bug）。"""

    def __lt__(self, other: QTableWidgetItem) -> bool:
        a = self.data(Qt.UserRole)
        b = other.data(Qt.UserRole)
        try:
            return float(a) < float(b)
        except (TypeError, ValueError):
            return super().__lt__(other)


def make_number_item(text: str, number: float) -> NumericItem:
    """可排序数值项：显示 text，排序/绘制用 number（存 UserRole）。"""
    item = NumericItem(str(text))
    item.setData(Qt.UserRole, float(number))
    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return item


# ───────────────── 环形图（占比可视化，QPainter 自绘）─────────────────

class DonutChart(QWidget):
    """环形图：显示一组 (label, value, color_key) 的占比。随主题切换用 theme.color()。

    用 QPainter 绘制，无第三方依赖。value 全为 0 时显示"暂无数据"文字。
    """

    def __init__(self, parent=None, center_text: str = ""):
        super().__init__(parent)
        self._segments: list[tuple[str, float, str]] = []   # (label, value, color_key)
        self._center_text = center_text
        self.setMinimumHeight(140)

    def set_center(self, text: str) -> None:
        self._center_text = text
        self.update()

    def set_data(self, segments: list[tuple[str, float, str]]) -> None:
        """segments: [(label, value, color_key)]，color_key 如 'accent'/'success'..."""
        self._segments = segments
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        side = min(w, h) - 20
        if side <= 0:
            p.end()
            return
        rect = QRectF((w - side) / 2, (h - side) / 2, side, side)
        total = sum(v for _, v, _ in self._segments)
        # 背景环
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.color("bg_hover")))
        ring_w = side * 0.22
        p.drawEllipse(rect)

        if total > 0:
            start = 90 * 16  # 从顶部开始，PySide 角度单位 1/16 度
            for label, value, ck in self._segments:
                if value <= 0:
                    continue
                span = int(-(value / total) * 360 * 16)
                p.setBrush(QColor(theme.color(ck)))
                p.drawPie(rect, start, span)
                start += span
        # 挖空中心
        inner = side - 2 * ring_w
        if inner > 0:
            inner_rect = QRectF((w - inner) / 2, (h - inner) / 2, inner, inner)
            p.setBrush(QColor(theme.color("bg_card")))
            p.drawEllipse(inner_rect)

        # 中心文字
        if self._center_text:
            p.setPen(QColor(theme.color("text_primary")))
            f = self.font()
            f.setPointSize(10)
            f.setBold(True)
            p.setFont(f)
            p.drawText(rect, Qt.AlignCenter, self._center_text)
        p.end()


# ───────────────── 竖直柱状图（趋势可视化）─────────────────

class VBarChart(QWidget):
    """竖直柱状图：显示 (label, value, color_key) 序列，适合年份分布等趋势。"""

    def __init__(self, parent=None, max_bars: int = 12):
        super().__init__(parent)
        self._bars: list[tuple[str, float, str]] = []
        self._max_bars = max_bars
        self.setMinimumHeight(120)

    def set_data(self, bars: list[tuple[str, float, str]]) -> None:
        self._bars = bars[-self._max_bars:]
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self._bars:
            p.setPen(QColor(theme.color("text_muted")))
            p.drawText(self.rect(), Qt.AlignCenter, "暂无数据")
            p.end()
            return
        w, h = self.width(), self.height()
        n = len(self._bars)
        maxv = max((v for _, v, _ in self._bars), default=1) or 1
        pad_l, pad_b, pad_t, pad_r = 8, 32, 8, 8
        plot_w = w - pad_l - pad_r
        plot_h = h - pad_b - pad_t
        slot = plot_w / n
        bar_w = min(slot * 0.6, 34)
        p.setPen(QColor(theme.color("text_muted")))
        # 网格线（3 条）
        f = self.font(); f.setPointSize(8); p.setFont(f)
        for g in range(4):
            gy = pad_t + plot_h * g / 3
            p.setPen(QColor(theme.color("border")))
            p.drawLine(pad_l, int(gy), pad_l + plot_w, int(gy))
        for i, (label, value, ck) in enumerate(self._bars):
            x = pad_l + slot * i + (slot - bar_w) / 2
            bh = (value / maxv) * plot_h
            y = pad_t + plot_h - bh
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.color(ck)))
            p.drawRoundedRect(QRectF(x, y, bar_w, bh), 2, 2)
            # 数值
            p.setPen(QColor(theme.color("text_secondary")))
            p.setFont(f)
            p.drawText(QRectF(x, max(0, y - 16), bar_w, 14),
                       Qt.AlignCenter, f"{int(value)}")
            # 标签
            p.setPen(QColor(theme.color("text_muted")))
            p.drawText(QRectF(x - slot / 2 + pad_l / 2, pad_t + plot_h + 4, slot, 16),
                       Qt.AlignCenter, str(label))
        p.end()


# ───────────────── 封面缩略图（打标卡片/删除候选用）─────────────────

class CoverThumb(QWidget):
    """居中显示一张封面图，按比例缩放适配，无图时画占位符。

    path 为空或文件不存在时显示"无封面"灰色占位（含🎬图标）。
    """

    def __init__(self, parent=None, size: int = 96):
        super().__init__(parent)
        self._size = size
        self._path: str | None = None
        self.setFixedSize(size, size)

    def set_cover(self, path: str | None) -> None:
        self._path = path if path and os.path.isfile(path) else None
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        rect = self.rect().adjusted(2, 2, -2, -2)
        # 背景
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.color("bg_hover")))
        p.drawRoundedRect(rect, 6, 6)
        if self._path:
            pix = QPixmap(self._path)
            if not pix.isNull():
                scaled = pix.scaled(self._size - 6, self._size - 6,
                                    Qt.KeepAspectRatio, Qt.SmoothTransformation)
                x = (self.width() - scaled.width()) / 2
                y = (self.height() - scaled.height()) / 2
                p.drawPixmap(int(x), int(y), scaled)
                p.end()
                return
        # 占位符
        p.setPen(QColor(theme.color("text_muted")))
        f = self.font()
        f.setPointSize(9)
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignCenter, "🎬\n无封面")
        p.end()


# 单元格封面 delegate：在表格单元格里画封面（项在 Qt.DecorationRole 存路径）
class CoverThumbDelegate(QStyledItemDelegate):
    """在单元格中绘制封面缩略图。项需在 Qt.DecorationRole 存入封面路径。"""

    THUMB = 40

    def paint(self, painter: QPainter, option, index) -> None:
        path = index.data(Qt.DecorationRole)
        # 画选中态背景
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, QColor(theme.color("accent_dark")))
        if path and os.path.isfile(str(path)):
            pix = QPixmap(str(path))
            if not pix.isNull():
                scaled = pix.scaled(self.THUMB, self.THUMB,
                                    Qt.KeepAspectRatio, Qt.SmoothTransformation)
                x = option.rect.center().x() - scaled.width() / 2
                y = option.rect.center().y() - scaled.height() / 2
                # 圆角裁剪
                painter.save()
                painter.setRenderHint(QPainter.Antialiasing)
                painter.setRenderHint(QPainter.SmoothPixmapTransform)
                painter.drawPixmap(int(x), int(y), scaled)
                painter.restore()
                return
        # 无图占位
        painter.setPen(QColor(theme.color("text_muted")))
        painter.drawText(option.rect, Qt.AlignCenter, "–")
