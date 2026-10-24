"""Review effort settings and an owned, resumable metadata synchronization pump."""
from copy import deepcopy
import re

from PySide6.QtCore import QObject, QTimer, Signal, Qt, QThread
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QFormLayout, QGroupBox,
    QCheckBox, QLineEdit, QSpinBox, QLabel, QDialog, QPlainTextEdit,
    QDialogButtonBox, QTableWidget, QTableWidgetItem, QHeaderView)


class ReviewFeatureSettings(QWidget):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        group = QGroupBox('分组快速检查')
        form = QFormLayout(group)
        self.triage_enabled = QCheckBox('启用分阶段取样，低风险暂不作为待办')
        self.triage_prefixes = QLineEdit()
        self.triage_samples = QSpinBox()
        self.triage_samples.setRange(24, 120)
        form.addRow(self.triage_enabled)
        form.addRow('文件名前缀（逗号分隔）', self.triage_prefixes)
        form.addRow('首轮取样数', self.triage_samples)
        hint = QLabel('默认 FC2，可添加其他分组。首轮覆盖全片时段；疑似、重复画面过多或取样不足时继续完整检查。低风险不是人工确认，无标签写入。')
        hint.setWordWrap(True)
        form.addRow(hint)
        layout.addWidget(group)
        group = QGroupBox('人工确认后的标签同步')
        form = QFormLayout(group)
        self.sync_nfo = QCheckBox('同步到 NFO')
        self.sync_create_nfo = QCheckBox('没有 NFO 时创建同名文件')
        self.sync_server = QCheckBox('同步到已配置的 Jellyfin / Emby')
        self.tag_positive = QLineEdit()
        self.tag_negative = QLineEdit()
        for checkbox in (self.sync_nfo, self.sync_create_nfo, self.sync_server):
            form.addRow(checkbox)
        form.addRow('确认有马赛克的标签', self.tag_positive)
        form.addRow('确认未见马赛克的标签', self.tag_negative)
        hint = QLabel('仅同步当前文件版本的人工确认，保留无关标签。撤回时恢复本功能的标签差量。服务器沿用全局连接与路径映射；失败可重试，不影响本地确认。')
        hint.setWordWrap(True)
        form.addRow(hint)
        layout.addWidget(group)
        self.reload(cfg)

    def reload(self, cfg):
        self.triage_enabled.setChecked(cfg.censor_triage_enabled)
        self.triage_prefixes.setText(', '.join(cfg.censor_triage_prefixes))
        self.triage_samples.setValue(cfg.censor_triage_samples)
        self.sync_nfo.setChecked(cfg.censor_sync_nfo)
        self.sync_create_nfo.setChecked(cfg.censor_sync_create_nfo)
        self.sync_server.setChecked(cfg.censor_sync_jellyfin)
        self.tag_positive.setText(cfg.censor_sync_tag_censored)
        self.tag_negative.setText(cfg.censor_sync_tag_uncensored)

    def apply(self, cfg):
        from config import _coerce_config
        raw = dict(censor_triage_enabled=self.triage_enabled.isChecked(),
            censor_triage_prefixes=re.split(r'[,，;；\s]+', self.triage_prefixes.text()),
            censor_triage_samples=self.triage_samples.value(),
            censor_sync_nfo=self.sync_nfo.isChecked(),
            censor_sync_create_nfo=self.sync_create_nfo.isChecked(),
            censor_sync_jellyfin=self.sync_server.isChecked(),
            censor_sync_tag_censored=self.tag_positive.text(),
            censor_sync_tag_uncensored=self.tag_negative.text())
        positive, negative = self.tag_positive.text().strip(), self.tag_negative.text().strip()
        if not positive or not negative or positive.casefold() == negative.casefold():
            raise ValueError('两个确认标签必须非空且不同')
        if any(len(tag) > 64 or any(ord(c) < 32 for c in tag) for tag in (positive, negative)):
            raise ValueError('标签不能超过64个字符或包含控制字符')
        normalized = _coerce_config(raw)
        for key in raw:
            setattr(cfg, key, normalized[key])


class ReviewMetadataController(QObject):
    changed = Signal(str)
    logged = Signal(str)

    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.paused = False
        self._retry = False
        self._backfill = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(300)
        self.timer.timeout.connect(self._pump)
        page._metadata_worker = None
        self.logged.connect(page._on_log, Qt.QueuedConnection)
        from ui.services import events
        events.config_changed.connect(self.retry)

    def schedule(self):
        self.paused = False
        self.timer.start()

    def retry(self):
        self._retry = True
        self.schedule()

    def pause(self):
        self.paused = True
        self.timer.stop()

    def _status(self):
        counts = self.page._service.metadata_sync().status()
        message = ('标签同步（按目标计数）：已完成 {done} · 待处理 {pending} · '
                   '待配置/等待 {waiting} · 失败 {failed} · 版本已变 {stale}').format(**counts)
        self.changed.emit(message)
        return counts

    def _pump(self):
        if self.paused or getattr(self.page.window(), '_closing', False):
            return
        from ui.services import busy, start_task, CallWorker
        if busy(self.page, mutating=True):
            self.timer.start(600)
            return
        cfg = deepcopy(self.page.cfg)
        retry, backfill = self._retry, self._backfill
        self._retry, self._backfill = False, None
        from censor.metadata_sync import MetadataSyncService
        service = MetadataSyncService(cfg, self.page._service.store)
        counts = service.status()
        if (not backfill and not sum(counts.values())) or (not backfill and not counts['pending'] and not retry):
            self._status()
            return
        if not (cfg.censor_sync_nfo or cfg.censor_sync_jellyfin):
            self.changed.emit('标签同步已关闭；本地人工确认仍保留')
            return
        def run(stopped):
            if backfill is not None and not stopped():
                service.enqueue_confirmed(backfill)
            return service.run_pending(should_stop=stopped, retry_failed=retry,
                                       log=self.logged.emit)
        # Log delivery must be a queued Qt signal, never a QWidget call from the worker.
        worker = CallWorker(run)
        worker.result.connect(self._result)
        worker.error.connect(self._error)
        worker.finished.connect(self._finished)
        self.page._metadata_worker = worker
        self.changed.emit('正在同步人工确认标签；停止后可从“更多 → 标签同步”继续')
        start_task(self.page, worker, '同步人工确认标签至 NFO / 媒体服务器', mutating=True)

    def _result(self, report):
        self._status()

    def _error(self, message):
        self.changed.emit('标签同步未完成：' + message)

    def _finished(self):
        worker = self.sender()
        if getattr(worker, '_stopped', False):
            self.pause()
            self.changed.emit('标签同步已停止；未完成项目已保留，可重试')
            return
        counts = self._status()
        if counts['pending'] and not self.paused:
            self.schedule()

    def show_status(self):
        service = self.page._service.metadata_sync()
        dialog = QDialog(self.page)
        dialog.setWindowTitle('人工确认标签同步')
        dialog.resize(780, 480)
        layout = QVBoxLayout(dialog)
        hint = QLabel('本地确认与 NFO、服务器同步各自保存。修正连接或文件访问后可重试；成功目标不会重复写入。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        labels = dict(pending='待处理', done='已完成', failed='失败', waiting='等待配置', stale='文件或确认已变更', skipped='未启用/无需处理')
        lines = []
        for row in service.records():
            state = {'censored': '确认有马赛克', 'uncensored': '确认未见马赛克', None: '撤回确认'}.get(row['desired'], '未定')
            lines.append(f"{row['path']}\n{state} · NFO：{labels.get(row['nfo'], row['nfo'])} · 服务器：{labels.get(row['jellyfin'], row['jellyfin'])}")
            for key in ('nfo_error', 'jellyfin_error'):
                if row.get(key):
                    lines.append(row[key])
        text.setPlainText('\n\n'.join(lines) or '暂无同步任务。确认视频后会自动加入。')
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.Retry | QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Retry).setText('重试未完成的同步')
        buttons.button(QDialogButtonBox.Retry).clicked.connect(lambda: (self.retry(), dialog.accept()))
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def backfill(self):
        from censor.models import EvidenceSource, CensorState
        rows = [result for result in self.page._results.values()
                if self.page._matches_filter(result)
                and result.verdict.of(EvidenceSource.MANUAL) is not None
                and result.state is not CensorState.UNKNOWN]
        dialog = QDialog(self.page)
        dialog.setWindowTitle('补同步当前筛选中的人工确认')
        dialog.resize(780, 440)
        layout = QVBoxLayout(dialog)
        hint = QLabel('先加载需要的历史并筛选。勾选项目会再次核验当前文件版本，跳过失效旧确认；不会修改算法低风险项目。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        table = QTableWidget(len(rows), 2)
        table.setHorizontalHeaderLabels(['同步此文件', '人工结论'])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for index, result in enumerate(rows):
            item = QTableWidgetItem(result.path)
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            table.setItem(index, 0, item)
            table.setItem(index, 1, QTableWidgetItem('有马赛克' if result.state is CensorState.CENSORED else '未见马赛克'))
        layout.addWidget(table)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText('同步勾选项目')
        buttons.button(QDialogButtonBox.Ok).setEnabled(bool(rows))
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.Accepted:
            self._backfill = [result.path for index, result in enumerate(rows)
                              if table.item(index, 0).checkState() == Qt.Checked]
            self.retry()


class BatchConfirmationWorker(QThread):
    """Only an explicitly selected set; every completed confirmation is durable."""
    item_done = Signal(object)
    result = Signal(object)
    progress = Signal(int)
    logged = Signal(str)

    def __init__(self, service, items, state):
        super().__init__()
        from copy import copy
        self.service = copy(service)
        self.service.cfg = deepcopy(service.cfg)
        self.items, self.state = deepcopy(items), state
        self._stopped = False

    def stop(self):
        self._stopped = True

    def run(self):
        completed, failures = [], []
        for index, base in enumerate(self.items):
            if self._stopped:
                break
            try:
                result = self.service.label(base.path, self.state, '界面批量人工确认', base=base)
                completed.append(base.path)
                self.item_done.emit(result)
                message = getattr(self.service, 'last_metadata_sync_error', '')
                if message:
                    self.logged.emit(message)
            except Exception as exc:
                failures.append(base.path)
                self.logged.emit(f'批量确认失败：{base.path}：{exc}')
            self.progress.emit(int(100 * (index + 1) / max(1, len(self.items))))
        self.result.emit(dict(completed=len(completed), paths=completed,
                              fail=len(failures), cancelled=self._stopped))
