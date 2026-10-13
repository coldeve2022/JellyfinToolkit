"""Compact action grids and collapsible advanced parameters for small windows."""

from PySide6.QtWidgets import QWidget, QPushButton, QVBoxLayout, QGridLayout, QHBoxLayout


def _layouts(layout):
    yield layout
    for index in range(layout.count()):
        child = layout.itemAt(index)
        if child.layout() is not None:
            yield from _layouts(child.layout())


def _find_row(page, widget):
    for container in [page] + page.findChildren(QWidget):
        if container.layout() is None:
            continue
        for layout in _layouts(container.layout()):
            for index in range(layout.count()):
                if layout.itemAt(index).widget() is widget:
                    return layout
    return None


def dock_actions(page, target_layout):
    module = type(page).__module__.rsplit(".", 1)[-1]
    attr = {
        "profile": "btn_analyze",
        "insight": "btn_scan",
        "tasks": "btn_stop",
        "review": "btn_analyze",
        "delete": "btn_load",
        "dedup": "btn_load",
        "subtitle": "btn_start",
        "subtitle_gen": "btn_start",
        "replace": "btn_start",
        "lada": "btn_start",
        "repair": "btn_start",
        "nfo_fix": "btn_start",
        "db_tools": "btn_transfer",
        "merge": "btn_merge_checked",
        "automation": "btn_audit",
        "settings": "btn_save",
    }.get(module)
    button = getattr(page, attr, None) if attr else None
    row = _find_row(page, button) if button is not None else None
    if row is None or not isinstance(row, QHBoxLayout):
        return
    footer = QWidget()
    footer.setObjectName("fixed-action-footer")
    grid = QGridLayout(footer)
    grid.setContentsMargins(12, 8, 12, 8)
    widgets = []
    while row.count():
        item = row.takeAt(0)
        if item.widget() is not None:
            widgets.append(item.widget())
    for index, widget in enumerate(widgets):
        widget.setMinimumWidth(0)
        widget.setMaximumWidth(16777215)
        grid.addWidget(widget, index // 2, index % 2)
    from PySide6.QtWidgets import QProgressBar

    for progress in page.findChildren(QProgressBar):
        progress_row = _find_row(page, progress)
        if progress_row:
            progress_row.removeWidget(progress)
        grid.addWidget(progress, grid.rowCount(), 0, 1, 2)
    target_layout.addWidget(footer)
    page._action_footer = footer


def collapse_advanced(page):
    from PySide6.QtWidgets import QGroupBox

    if type(page).__module__.rsplit(".", 1)[-1] not in ("lada", "subtitle_gen"):
        return
    for group in list(page.findChildren(QGroupBox)):
        if not group.title().startswith("参数"):
            continue
        row = _find_row(page, group)
        if row is None:
            continue
        index = next(i for i in range(row.count()) if row.itemAt(i).widget() is group)
        row.takeAt(index)
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        toggle = QPushButton("▸ 参数与高级选项（当前已保存默认值）")
        toggle.setCheckable(True)
        layout.addWidget(toggle)
        layout.addWidget(group)
        group.hide()
        toggle.toggled.connect(group.setVisible)
        toggle.toggled.connect(
            lambda checked, button=toggle: button.setText(
                "▾ 收起参数与高级选项" if checked else "▸ 参数与高级选项（当前已保存默认值）"
            )
        )
        row.insertWidget(index, panel)


def compact_forms(page):
    """Wrap explanatory labels; dense form rows use two columns at every window size."""
    from PySide6.QtWidgets import QLabel, QBoxLayout

    for label in page.findChildren(QLabel):
        label.setWordWrap(True)
    if type(page).__module__.rsplit(".", 1)[-1] not in ("settings", "review"):
        return
    for container in [page] + page.findChildren(QWidget):
        root = container.layout()
        if root is None:
            continue
        for parent in list(_layouts(root)):
            if not isinstance(parent, QBoxLayout):
                continue
            for idx in range(parent.count() - 1, -1, -1):
                row = parent.itemAt(idx).layout()
                if not isinstance(row, QHBoxLayout) or row.count() < 3:
                    continue
                if (
                    type(page).__module__.rsplit(".", 1)[-1] != "settings"
                    and row.minimumSize().width() < 530
                ):
                    continue
                parent.takeAt(idx)
                grid = QGridLayout()
                widgets = []
                while row.count():
                    item = row.takeAt(0)
                    if item.widget() is not None:
                        widgets.append(item.widget())
                for index, widget in enumerate(widgets):
                    grid.addWidget(widget, index // 2, index % 2)
                parent.insertLayout(idx, grid)
