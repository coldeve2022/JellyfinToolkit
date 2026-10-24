"""Reviewed local video version management with preserved inheritance snapshots."""

from __future__ import annotations

from ui.services import busy, start_task, stop_page

import os
import json
import hashlib
import uuid
from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QMessageBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QComboBox,
    QMenu,
)

from config import ToolkitConfig

from ui.widgets import DragDropListWidget, LogPanel, InfoCard, FileInputActions
from utils.censor_tags import update_jellyfin_db_tags
from utils.nfo import update_nfo_censor_tags
from utils.nfo_source import find_nfo
from ui.services import CallWorker, events, after_stopped
from utils.media_upgrade import MediaUpgradeService

#: 配对状态的中文名（原来直接把 ready / ambiguous 抛给用户看）
_UPGRADE_STATUS = {
    'ready': '就绪',
    'ambiguous': '要选版本',
    'container_convert': '要转封装',
    'not_found': '没找到新版本',
    'conflict': '冲突',
}


def apply_candidate_choice(rows, index, path) -> bool:
    """把第 ``index`` 行要用的新版本定成 ``path``。

    纯函数：只改传入的行数据，不碰界面。UI 负责弹菜单，这里负责改状态 ——
    这样"能不能选、选完变什么"可以直接测。
    """
    if not isinstance(rows, list) or not (0 <= index < len(rows)):
        return False
    entry = rows[index]
    candidates = entry.get('candidates') or []
    if path not in candidates:
        return False
    entry['target'] = path
    entry['status'] = 'ready'
    entry['error'] = ''
    return True


def drop_plan_row(rows, index) -> bool:
    """从配对计划里移除一行（踢出无法解决的文件，让其余继续）。

    ``not_found``（没有新版本）、``conflict``（多行抢同一个原文件）这类问题
    没法靠"选一个"解决；没有移除入口的话，一行就能卡住整批。
    """
    if not isinstance(rows, list) or not (0 <= index < len(rows)):
        return False
    del rows[index]
    return True


class ReplaceWorker(QThread):
    """后台替换线程 — 索引原库后匹配 restored 文件。

    安全策略：执行已审阅的同一匹配计划；保留事务备份和日志，
    失败回滚，成功后可在任务中心恢复。
    """

    log = Signal(str)
    finished = Signal(int, int)  # 成功数, 未匹配数

    def __init__(
        self,
        restored_paths: list[str],
        library_paths: list[str],
        marker: str = "restored",
        cfg: ToolkitConfig | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._restored = restored_paths
        self._library = library_paths
        self._marker = marker
        self._cfg = cfg
        self._db_backed_up = False
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def run(self) -> None:
        from utils.replacement import preview_replacements, execute_replacements

        try:
            plan = getattr(self, "plan", None)
            if plan is None:
                plan = preview_replacements(
                    self._restored, self._library, self._marker, lambda: self._stopped
                )
            stats = execute_replacements(plan, should_stop=lambda: self._stopped, log=self.log.emit)
            self.stats = stats
            stats['metadata'] = []
            for operation in stats.get("operations", []):
                if operation.get("ok") and not self._stopped:
                    try:
                        outcome = self._sync_censor_metadata(operation["target"])
                    except Exception as exc:
                        outcome = dict(target=operation['target'], status='failed', error=str(exc))
                        self.log.emit(f'文件已替换；元数据同步失败：{exc}')
                    stats['metadata'].append(outcome)
            stats['metadata_failed'] = sum(row.get('status') == 'failed' for row in stats['metadata'])
            self.finished.emit(stats["ok"], stats["fail"])
        except Exception as exc:
            self.stats = {"error": str(exc)}
            self.log.emit(f"替换失败：{exc}")
            self.finished.emit(0, 1)

    def _sync_censor_metadata(self, target: str) -> dict:
        """替换成功后同步 NFO 与 Jellyfin 数据库的“有码 → 无码破解”。"""
        nfo = find_nfo(target)
        result = dict(target=target, status='completed', nfo='skipped', server='skipped')
        if nfo:
            ok, detail = update_nfo_censor_tags(nfo)
            result['nfo'] = 'completed' if ok else 'failed'
            result['nfo_detail'] = detail
            if not ok:
                result['status'] = 'failed'
            self.log.emit(("  ✅ " if ok else "  ⚠️ ") + f"NFO 标签：{detail}")
        else:
            self.log.emit("  ⚠️ 未找到同名 NFO，跳过 NFO 标签同步")

        if not self._cfg:
            self.log.emit("  ⚠️ 未提供配置，跳过 Jellyfin 数据库标签同步")
            return result
        db_path = getattr(self._cfg, "jellyfin_db_path", "")
        if not db_path or not Path(db_path).exists():
            self.log.emit(
                "  ⚠️ 未找到本地库文件，跳过数据库标签同步"
                f"（配置值：{db_path or '空'}）。\n"
                "     换名后的条目会在服务器下次扫描时自动更新；"
                "若使用 Emby 或「服务器 API」模式，请在服务器端刷新媒体库。"
            )
            return result
        backup = not self._db_backed_up
        self._db_backed_up = True
        ok, detail = update_jellyfin_db_tags(db_path, target, backup=backup, log=self.log.emit)
        result['server'] = 'completed' if ok else 'failed'
        result['server_detail'] = detail
        if not ok:
            result['status'] = 'failed'
        self.log.emit(("  ✅ " if ok else "  ⚠️ ") + f"Jellyfin 数据库：{detail}")
        return result


def _configuration_key(cfg):
    return hashlib.sha256(json.dumps(vars(cfg), sort_keys=True, default=str).encode()).hexdigest()


def _save_upgrade_reference(data_dir, prepared, context_key, state):
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    reference = {key: prepared[key] for key in ('snapshot_id', 'snapshot_path', 'snapshot_sha256')}
    reference.update(context_key=context_key, state=state)
    temporary = data_dir / ('latest-' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as fh:
            json.dump(reference, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, data_dir / 'latest.json')
    finally:
        temporary.unlink(missing_ok=True)


class UpgradeWorker(QThread):
    """Generic owned worker: never invokes ReplaceWorker or metadata tag updaters."""
    result = Signal(object)
    error = Signal(str)
    log = Signal(str)

    def __init__(self, cfg, data_dir, phase, payload):
        super().__init__()
        self.cfg = deepcopy(cfg)
        self.data_dir = Path(data_dir)
        self.phase = phase
        self.payload = deepcopy(payload)
        self._stopped = False

    def stop(self):
        self._stopped = True
        self.requestInterruption()

    def _save_reference(self, prepared, state):
        try:
            _save_upgrade_reference(self.data_dir, prepared, _configuration_key(self.cfg), state)
        except (OSError, ValueError, KeyError):
            self.log.emit('上次快照引用未能保存；已生成的独立快照及文件操作日志仍保留。')

    def run(self):
        def cancel():
            return self._stopped
        try:
            service = MediaUpgradeService(self.cfg, data_dir=self.data_dir)
            if self.phase == 'preview':
                value = service.preview(*self.payload, cancel=cancel)
            elif self.phase == 'prepare':
                value = service.prepare(self.payload, cancel=cancel)
                self._save_reference(value, 'prepared')
            elif self.phase == 'execute':
                value = service.execute(self.payload, cancel=cancel, log=self.log.emit)
                self._save_reference(self.payload, 'executed')
            elif self.phase == 'verify':
                value = service.verify(self.payload, cancel=cancel)
            elif self.phase == 'reload':
                latest = self.data_dir / 'latest.json'
                if not latest.exists():
                    raise ValueError('还没有保存过快照。先点「2. 保存进度快照」，'
                                     '存好之后「载入上次记录」才有东西可读')
                reference = json.loads(latest.read_text(encoding='utf-8'))
                if reference.get('context_key') != _configuration_key(self.cfg):
                    raise ValueError('上次快照属于其他服务器/配置；请重新预览并保存快照')
                snapshot_id = reference['snapshot_id']
                if len(snapshot_id) != 32 or any(ch not in '0123456789abcdef' for ch in snapshot_id):
                    raise ValueError('上次快照引用无效')
                path = self.data_dir / 'snapshots' / (snapshot_id + '.json')
                prepared = json.loads(path.read_text(encoding='utf-8'))
                prepared.update(snapshot_path=str(path), snapshot_sha256=reference['snapshot_sha256'])
                # The backend validates its fixed snapshot location, bytes, and contents.
                service._load(prepared)
                value = {'prepared': prepared, 'state': reference.get('state')}
            else:
                raise ValueError('未知版本管理阶段')
            # Mutation results must be reported even when stop was requested,
            # so the page can notify other views about changed/recovered files.
            if not self._stopped or self.phase == 'execute':
                self.result.emit(value)
        except Exception as exc:
            self.error.emit(str(exc))


class ReplacePage(QWidget):
    """Table-first version upgrades; legacy restored pairing remains available."""

    def __init__(self, cfg: ToolkitConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._worker = None
        self._plan = None
        self._prepared = None
        self._executed = False
        self._revision = 0
        self._prepared_context = None
        self._setup_ui()
        self.list_restored.paths_changed.connect(self._invalidate_plan)
        self.list_library.paths_changed.connect(self._invalidate_plan)
        events.config_changed.connect(self._invalidate_plan)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)
        title = QLabel('视频版本管理')
        title.setProperty('cssClass', 'page-title')
        layout.addWidget(title)
        layout.addWidget(InfoCard(
            '将已下载的更高画质或其他本地版本按完整标识配对，保留原文件路径、NFO、字幕及可恢复备份。\n'
            '先预览配对，再保存元数据和每个可访问用户的状态快照；审阅确认后才替换。服务器刷新完成后单独验证继承。'))
        self.match_mode = QComboBox()
        self.match_mode.addItem('通用本地版本：按完整标识配对并保留元数据', 'upgrade')
        self.match_mode.addItem('兼容：按 restored 文件名配对', 'restored')
        self.match_mode.addItem('兼容：按作品标识配对本地候选', 'release')
        self.match_mode.currentIndexChanged.connect(self._invalidate_plan)
        layout.addWidget(self.match_mode)
        splitter = QSplitter()
        splitter.setMaximumHeight(155)
        for label, callback, role, attribute in (
            ('本地新版本 / 文件夹', self._add_to_restored, 'restored', 'list_restored'),
            ('媒体库原文件 / 文件夹', self._add_to_library, 'library', 'list_library')):
            box = QWidget()
            column = QVBoxLayout(box)
            column.setContentsMargins(0, 0, 6, 0)
            column.addWidget(QLabel(label))
            column.addWidget(FileInputActions(self, callback, role=role))
            paths = DragDropListWidget()
            paths.files_dropped.connect(callback)
            column.addWidget(paths)
            setattr(self, attribute, paths)
            splitter.addWidget(box)
        layout.addWidget(splitter)
        self.pairs = QTableWidget(0, 5)
        self.pairs.setHorizontalHeaderLabels(['本地新版本', '保留路径的原文件', '完整标识', '状态', '元数据 / 用户状态'])
        self.pairs.setEditTriggers(QTableWidget.NoEditTriggers)
        self.pairs.setSelectionBehavior(QTableWidget.SelectRows)
        self.pairs.setWordWrap(False)
        self.pairs.cellDoubleClicked.connect(self._choose_candidate)
        self.pairs.setContextMenuPolicy(Qt.CustomContextMenu)
        self.pairs.customContextMenuRequested.connect(self._pair_menu)
        self.pairs.setProperty('taskInteractive', True)
        self.pairs.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.pairs.horizontalHeader().setStretchLastSection(True)
        for col, width in enumerate((230, 230, 100, 110)):
            self.pairs.setColumnWidth(col, width)
        self.pairs.setMinimumHeight(180)
        layout.addWidget(self.pairs, 1)
        self.log_panel = LogPanel()
        self.log_panel.setMaximumHeight(95)
        layout.addWidget(self.log_panel)
        buttons = QHBoxLayout()
        for attribute, text, callback in (
            ('btn_preview', '1. 预览配对', self._preview),
            ('btn_prepare', '2. 保存进度快照', self._prepare_upgrade),
            ('btn_start', '3. 审阅后替换…', self._start),
            ('btn_verify', '4. 核对结果', self._verify_upgrade),
            ('btn_reload', '载入上次记录', self._reload_upgrade),
            ('btn_stop', '停止', self._stop)):
            button = QPushButton(text)
            button.clicked.connect(callback)
            setattr(self, attribute, button)
            buttons.addWidget(button)
        self.btn_start.setProperty('cssClass', 'accent')
        clear = QPushButton('清空队列')
        clear.clicked.connect(self._clear)
        buttons.addWidget(clear)
        layout.addLayout(buttons)
        self.stats = QLabel('按 1 → 4 依次操作')
        self.stats.setWordWrap(True)
        self.stats.setProperty('cssClass', 'muted-label')
        layout.addWidget(self.stats)
        self._sync_buttons()

    def _generic(self):
        return self.match_mode.currentData() == 'upgrade'

    def _context(self):
        return self._revision, _configuration_key(self.cfg)

    def _sync_buttons(self):
        rows = self._plan.get('rows', []) if isinstance(self._plan, dict) else (self._plan or [])
        # "要转封装"和"就绪"一样能执行：转换是程序自己做的，不需要用户干预
        runnable = ('ready', 'container_convert')
        ready = bool(rows) and all(row.get('status') in runnable for row in rows)
        prepared_ready = bool(self._prepared) and all(
            row.get('status') in runnable for row in self._prepared.get('rows', []))
        self.btn_prepare.setVisible(self._generic())
        self.btn_verify.setVisible(self._generic())
        self.btn_reload.setVisible(self._generic())
        self.btn_prepare.setEnabled(self._generic() and ready and not self._executed)
        self.btn_start.setEnabled(prepared_ready and not self._executed if self._generic() else ready)
        self.btn_verify.setEnabled(self._generic() and bool(self._prepared) and self._executed)
        self.btn_stop.setEnabled(busy(self))

    def _invalidate_plan(self, *args):
        self._revision += 1
        self._plan = None
        self._prepared = None
        self._prepared_context = None
        self._executed = False
        if isinstance(self._worker, UpgradeWorker) and self._worker.isRunning():
            self._worker.stop()
        self.pairs.setRowCount(0)
        self._sync_buttons()
        self.btn_start.setEnabled(False)
        if busy(self):
            after_stopped(self, self._sync_buttons)

    def _run_upgrade(self, phase, payload):
        from config import DATA_DIR
        context = self._context()
        worker = UpgradeWorker(deepcopy(self.cfg), Path(DATA_DIR) / 'media_upgrades', phase, payload)
        self._worker = worker
        worker.log.connect(self.log_panel.log)
        worker.error.connect(self.log_panel.log_error)
        worker.result.connect(lambda result: self._upgrade_result(phase, result, context))
        worker.finished.connect(lambda: after_stopped(self, self._sync_buttons))
        self.btn_stop.setEnabled(True)
        start_task(self, worker, {'preview': '预览本地视频版本配对', 'prepare': '保存版本继承快照',
            'execute': '按已审阅快照替换本地视频', 'verify': '验证版本元数据和用户状态继承',
            'reload': '载入上次版本继承快照'}[phase], mutating=phase == 'execute')

    def _preview(self):
        if busy(self):
            return
        restored, library = self.list_restored.get_all_paths(), self.list_library.get_all_paths()
        if not restored or not library:
            self.log_panel.log_warning('请加入本地新版本及媒体库原文件，再预览配对。')
            return
        self._invalidate_plan()
        if self._generic():
            self._run_upgrade('preview', (restored, library))
            return
        from utils.replacement import preview_replacements, preview_release_replacements
        mode = self.match_mode.currentData()
        marker = self.cfg.restored_suffix_marker
        context = self._context()
        self._worker = CallWorker(lambda cancel: preview_release_replacements(restored, library, cancel)
            if mode == 'release' else preview_replacements(restored, library, marker, cancel))
        self._worker.result.connect(lambda plan: self._show_plan(plan) if context == self._context() else None)
        self._worker.error.connect(self.log_panel.log_error)
        self._worker.finished.connect(lambda: after_stopped(self, self._sync_buttons))
        start_task(self, self._worker, '生成本地视频配对计划')

    def _show_plan(self, plan):
        self._plan = plan
        rows = plan.get('rows', []) if isinstance(plan, dict) else plan
        self._render_rows(rows)
        self._sync_buttons()
        self.stats.setText(f'配对计划 {len(rows)} 项；冲突、多候选或封装格式不同时请调整输入。保存快照不会替换视频。')

    def _render_rows(self, rows):
        self.pairs.setRowCount(0)
        for entry in rows:
            row = self.pairs.rowCount()
            self.pairs.insertRow(row)
            summary = entry.get('inheritance_summary', '')
            if entry.get('server'):
                summary = f"元数据已快照；{len(entry['server'].get('users', {}))} 个用户状态；保留 NFO/字幕"
            if entry.get('error'):
                summary = entry['error']
            status = entry.get('status', '')
            candidates = entry.get('candidates', [])
            pair_text = entry.get('target') or ' / '.join(candidates)
            for col, value in enumerate((entry.get('source', ''), pair_text,
                    entry.get('identifier', ''), _UPGRADE_STATUS.get(status, status), summary)):
                cell = QTableWidgetItem(str(value))
                tip = str(value)
                if status == 'ambiguous':
                    tip += '\n双击这一行，选择要用的新版本'
                elif status == 'container_convert':
                    tip += '\n封装与原文件不同（如 mkv / mp4）；双击选择后会自动无损转封装'
                cell.setToolTip(tip)
                self.pairs.setItem(row, col, cell)

    def _pair_menu(self, pos) -> None:
        row = self.pairs.rowAt(pos.y())
        rows = self._plan.get('rows', []) if isinstance(self._plan, dict) else []
        if not (0 <= row < len(rows)):
            return
        entry = rows[row]
        menu = QMenu(self)
        if entry.get('status') == 'ambiguous':
            menu.addAction('选择版本…', lambda: self._choose_candidate(row))
        menu.addAction('从配对里移除', lambda: self._drop_row(row))
        menu.exec(self.pairs.viewport().mapToGlobal(pos))

    def _drop_row(self, row: int) -> None:
        rows = self._plan.get('rows', []) if isinstance(self._plan, dict) else []
        if not drop_plan_row(rows, row):
            return
        self._render_rows(rows)
        self._sync_buttons()
        self.log_panel.log('已从本次配对里移除该文件；原文件不会被改动')

    def _choose_candidate(self, row: int, _column: int = 0) -> None:
        """给"匹配到多个新版本"的行一个选择入口。

        以前这类行只有一句"需要选择唯一版本并匹配封装格式"，界面上却没有
        任何可选的地方 —— 而且只要有一行没定，整批的「保存进度快照」就是
        灰的，等于卡死。
        """
        rows = self._plan.get('rows', []) if isinstance(self._plan, dict) else []
        if not (0 <= row < len(rows)):
            return
        entry = rows[row]
        status = entry.get('status', '')
        if status == 'ready':
            return
        candidates = entry.get('candidates') or []
        if not candidates:
            self.log_panel.log_warning('这一行没有可选的本地新版本；先在左上「添加文件…」加入')
            return
        menu = QMenu(self)
        for path in candidates:
            action = menu.addAction(os.path.basename(path))
            action.setToolTip(path)
            action.setData(path)
        chosen = menu.exec(QCursor.pos())
        if chosen is None:
            return
        if not apply_candidate_choice(rows, row, chosen.data()):
            return
        self._render_rows(rows)
        self._sync_buttons()
        if entry.get('convert_container'):
            suffix = Path(entry['source']).suffix
            self.log_panel.log(f'已选定：{os.path.basename(entry["target"])}'
                               f'（封装不同，替换前会用 ffmpeg 无损转换成 {suffix}）')
        else:
            self.log_panel.log(f'已选定：{os.path.basename(entry["target"])}；可继续保存进度快照')

    def _prepare_upgrade(self):
        if busy(self) or not self._generic() or not isinstance(self._plan, dict):
            return
        self._prepared = None
        self._run_upgrade('prepare', self._plan)

    def _verify_upgrade(self):
        if busy(self) or not self._generic() or not self._prepared or not self._executed:
            return
        self.log_panel.log('验证当前服务器状态；请先确认服务器受控刷新已完成。状态变化将保留快照供人工审阅。')
        self._run_upgrade('verify', self._prepared)

    def _reload_upgrade(self):
        if busy(self) or not self._generic():
            return
        from config import DATA_DIR

        if not (Path(DATA_DIR) / 'media_upgrades' / 'latest.json').exists():
            self.log_panel.log_warning(
                '还没有保存过快照，没有可载入的记录。先「1. 预览配对」→「2. 保存进度快照」')
            return
        self._invalidate_plan()
        self._run_upgrade('reload', None)

    def _upgrade_result(self, phase, result, context):
        if phase == 'execute':
            changed = [row['target'] for row in result.get('operations', []) if row.get('ok') or row.get('state') == 'partial']
            if changed:
                events.media_changed.emit(changed)
        if context != self._context():
            self.log_panel.log_warning('输入或配置已变化，旧任务结果未作为当前计划；可在任务中心审阅已执行的文件操作。')
            return
        if phase == 'preview':
            self._show_plan(result)
        elif phase == 'prepare':
            self._prepared = result
            self._prepared_context = context
            self._render_rows(result.get('rows', []))
            blocked = sum(row.get('status') != 'ready' for row in result.get('rows', []))
            self.stats.setText(f"继承快照已保存：{result['snapshot_id']}；未替换视频。" + (f' {blocked} 项未就绪，详见表格。' if blocked else ' 请审阅配对和快照摘要后确认替换。'))
        elif phase == 'execute':
            self._executed = True
            self._render_rows(result.get('rows', []))
            self.stats.setText(f"本地替换成功 {result.get('ok', 0)}；失败 {result.get('fail', 0)}；待验证/人工处理 {result.get('needs_attention', 0)}。" + (' 已停止。' if result.get('cancelled') else '') + ' 服务器刷新排队不表示继承已完成。')
            if result.get('error'):
                self.log_panel.log_error(result['error'])
        elif phase == 'verify':
            self._render_rows(result.get('rows', []))
            attention = sum(row.get('status') != 'verified' for row in result.get('rows', []))
            self.stats.setText(f'继承验证：{attention} 项待人工审阅；快照保留。' if attention else '已验证原条目身份、元数据及已快照用户状态一致。')
        elif phase == 'reload':
            self._prepared = result['prepared']
            self._prepared_context = context
            self._executed = result.get('state') == 'executed'
            self._render_rows(self._prepared.get('rows', []))
            # Restarted snapshots are for verification only; execution requires a fresh review.
            self._executed = True
            self.stats.setText('上次继承快照已载入，仅用于验证；再次替换请重新预览并保存快照。')
        self._sync_buttons()

    def receive_originals(self, paths):
        if busy(self):
            self.log_panel.log_warning('当前任务仍在运行，原文件未加入；请停止或等待后重试。')
            return
        self._clear()
        self.match_mode.setCurrentIndex(self.match_mode.findData('upgrade'))
        self._add_to_library(paths)
        self.log_panel.log('已将原视频加入版本管理队列；仅加入队列，尚未替换。请手动加入已下载的本地新版本。')

    def receive_outputs(self, restored, originals, *, mode='restored'):
        if busy(self):
            return
        self._clear()
        self.match_mode.setCurrentIndex(max(0, self.match_mode.findData(mode)))
        self._add_to_restored(restored)
        self._add_to_library(originals)
        self._preview()

    def receive_candidates(self, candidates, originals):
        self.receive_outputs(candidates, originals, mode='release')

    def _add_to_restored(self, paths):
        if busy(self):
            return
        for path in paths:
            self.list_restored.add_item(os.path.basename(path) or path, path)

    def _add_to_library(self, paths):
        if busy(self):
            return
        for path in paths:
            self.list_library.add_item(os.path.basename(path) or path, path)

    def _start(self):
        if busy(self, mutating=True):
            return
        if self._generic():
            if not self._prepared or self._executed or self._prepared_context != self._context():
                self.log_panel.log_warning('请先为当前配对保存继承快照并审阅。')
                return
            rows = self._prepared.get('rows', [])
            if not rows or any(row.get('status') not in ('ready', 'container_convert')
                               for row in rows):
                self.log_panel.log_warning('快照里有还没处理的项目，请先修正配对。')
                return
            detail = '\n'.join(row['source'] + ' → ' + row['target'] for row in rows)
            converts = sum(1 for row in rows if row.get('convert_container'))
            if converts:
                detail += (f'\n\n其中 {converts} 条封装不同，会先用 ffmpeg 无损转封装'
                           f'（-c copy，不重新编码）再替换')
            if QMessageBox.question(self, '确认本地版本替换', detail + '\n\n将按已保存的同一快照替换。原件保留可恢复备份，NFO/字幕保留原字节。服务器继承需刷新完成后单独验证。确认执行？', QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
                self._run_upgrade('execute', self._prepared)
            return
        if not self._plan or any(row['status'] != 'ready' for row in self._plan):
            return
        detail = '\n'.join(row['source'] + ' → ' + row['target'] for row in self._plan)
        if QMessageBox.question(self, '确认兼容替换计划', detail + '\n\n兼容模式会运行原有恢复视频元数据同步。原件保留备份。确认执行？', QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self._worker = ReplaceWorker(self.list_restored.get_all_paths(), self.list_library.get_all_paths(), self.cfg.restored_suffix_marker, deepcopy(self.cfg))
        self._worker.plan = deepcopy(self._plan)
        self._worker.log.connect(self.log_panel.log)
        self._worker.finished.connect(self._on_finished)
        start_task(self, self._worker)
        self.btn_stop.setEnabled(True)

    def _stop(self):
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self.log_panel.log_warning('已请求停止；正在等待安全检查或回滚完成。')
            self.btn_start.setEnabled(False)

    def _on_finished(self, replaced, not_found):
        stats = getattr(self._worker, 'stats', {})
        self._plan = None
        self.stats.setText(f"兼容模式文件替换：成功 {replaced} / 失败 {not_found}；元数据同步失败 {stats.get('metadata_failed', 0)}。")
        changed = [row['target'] for row in stats.get('operations', []) if row.get('ok')]
        if changed:
            events.media_changed.emit(changed)
        after_stopped(self, self._sync_buttons)

    def _clear(self):
        if busy(self):
            return
        self._invalidate_plan()
        self.list_restored.clear()
        self.list_library.clear()
        self.log_panel.clear()
        self.stats.setText('')

    def shutdown(self):
        stop_page(self)
