"""Explicit-user, owned background favorites integration for the review page."""
from copy import deepcopy
import os

from PySide6.QtCore import QObject, Signal, QTimer, Qt
from PySide6.QtWidgets import QInputDialog

from ui.services import CallWorker, busy, start_task, events
from utils.jellyfin_review import JellyfinReviewService


class JellyfinReviewController(QObject):
    changed = Signal(str)
    normalize_path = staticmethod(JellyfinReviewService.normalize_path)

    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.items = {}
        self.context_key = ''
        self.last_status = ''
        self.reload(repaint=False)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.refresh)

    def service(self):
        return JellyfinReviewService(self.page.cfg, data_dir=self.page._service.store.db_path.parent)

    def reload(self, *, repaint=True):
        service = self.service()
        self.context_key = service.context_key
        self.items = service.cached_items()
        self.last_status = ('Jellyfin：请选择观看账号后同步喜欢和播放状态' if not service.user_id
                            else f'Jellyfin：已选账号 · 缓存 {len(self.items)} 项，未匹配项保持未知')
        self.changed.emit(self.last_status)
        if repaint:
            self.repaint()

    def item(self, path):
        return self.items.get(self.normalize_path(path), {})

    def refresh(self):
        if busy(self.page):
            self.changed.emit('Jellyfin：当前任务进行中，结束后可同步喜欢')
            return
        service = self.service()
        if not service.user_id:
            self.choose_user()
            return
        def work(cancel):
            result = service.refresh(should_stop=cancel)
            return dict(summary=result, items=service.cached_items(), context_key=service.context_key)
        worker = CallWorker(work)
        self.page._favorites_worker = worker
        worker.result.connect(self.accept_snapshot)
        worker.error.connect(lambda _: self.changed.emit('Jellyfin：读取失败，请检查数据源和观看账号'))
        self.changed.emit('Jellyfin：正在同步所选用户的喜欢和播放状态…')
        start_task(self.page, worker, '只读同步 Jellyfin 喜欢与播放状态')

    def accept_snapshot(self, payload):
        if payload.get('context_key') != self.service().context_key:
            return
        summary = payload.get('summary', {})
        if summary.get('cancelled'):
            self.changed.emit('Jellyfin：已取消；保留上次完整快照')
            return
        if not summary.get('ok'):
            self.changed.emit('Jellyfin：' + (summary.get('error') or '同步未完成，保留上次完整快照'))
            return
        self.context_key = payload['context_key']
        self.items = payload.get('items', {})
        self.last_status = (f"Jellyfin：已同步 {summary.get('count', len(self.items))} 项"
                            f" · 喜欢 {summary.get('favorites', 0)} · 状态未知 {summary.get('unknown', 0)}")
        self.changed.emit(self.last_status)
        self.repaint()

    def repaint(self):
        table = self.page.table
        table.setSortingEnabled(False)
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if item:
                self.page._set_library_cells(row, item.data(Qt.UserRole))
        table.setSortingEnabled(True)
        self.page._apply_filter()
        self.page._apply_library_sort()

    def choose_user(self):
        if busy(self.page):
            return
        service = self.service()
        worker = CallWorker(lambda cancel: dict(context_key=service.context_key,
            users=service.list_users(should_stop=cancel)))
        self.page._favorites_worker = worker
        worker.result.connect(self._choose_loaded_user)
        worker.error.connect(lambda _: self.changed.emit('Jellyfin：无法读取账号，请在设置检查数据源/API Key'))
        start_task(self.page, worker, '读取 Jellyfin 观看账号')

    def _choose_loaded_user(self, payload):
        if payload.get('context_key') != self.service().context_key:
            return
        users = payload.get('users', [])
        if not users:
            self.changed.emit('Jellyfin：没有可读取的观看账号，请检查数据源或 API Key')
            return
        labels = [f"{row.get('name') or row['id']}  [{row['id']}]" for row in users]
        current = next((i for i, row in enumerate(users) if row['id'] == self.page.cfg.jellyfin_user_id), 0)
        label, accepted = QInputDialog.getItem(self.page, '选择 Jellyfin 观看账号',
            '收藏和播放状态只属于所选账号；不会自动选择管理员', labels, current, False)
        if not accepted:
            return
        if payload['context_key'] != self.service().context_key:
            self.changed.emit('Jellyfin：数据源或账号已变化，请重新读取账号')
            return
        selected = users[labels.index(label)]
        updated = deepcopy(self.page.cfg)
        updated.jellyfin_user_id = selected['id']
        if selected.get('source_db_path') and not updated.jellyfin_db_path:
            updated.jellyfin_db_path = selected['source_db_path']
        try:
            updated.save()
        except OSError:
            self.changed.emit('Jellyfin：账号设置保存失败，未切换')
            return
        vars(self.page.cfg).update(vars(updated))
        self.reload()
        events.config_changed.emit()
        # The completed user-list worker may still be registered for delivery.
        from ui.services import after_stopped
        after_stopped(self.page, self.refresh)

    def import_favorites(self):
        paths = [row['path'] for row in self.items.values()
                 if row.get('is_favorite') is True and row.get('path')]
        if paths:
            self.page._start_scan(list(dict.fromkeys(paths)))
        else:
            self.changed.emit('Jellyfin：当前快照没有可导入的喜欢视频，请先选择账号并同步')

    def start_auto_refresh(self):
        if self.page.cfg.jellyfin_user_id and not os.environ.get('JELLYFIN_TOOLKIT_OFFLINE'):
            self.timer.start(1800)

    def shutdown(self):
        self.timer.stop()
