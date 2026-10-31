"""Choose a persistent library before services start, and expose its location."""
import os

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QInputDialog, QLabel,
    QMessageBox, QPushButton, QVBoxLayout, QPlainTextEdit, QApplication,
)

from utils.data_profile import (PROFILE_FILENAME, ProfileError, inspect_root,
                               profile_diagnostics, format_profile_diagnostics)
from utils.profile_runtime import startup_selection, open_profile, write_local_pointer
from utils.profile_lock import ProfileLock

_startup_lock = None


def choose_profile(rows):
    labels = [f"人工确认 {row['labels']} · 扫描 {row['scans']}\n{row['root']}" for row in rows]
    label, accepted = QInputDialog.getItem(None, '选择继续使用的资料库',
        '发现旧资料库。请选择本次使用的位置；选择外部目录后仅登记到当前程序文件夹，不会自动合并。',
        labels, 0, False)
    return rows[labels.index(label)]['root'] if accepted else None


def select_existing_profile(parent=None):
    import config
    selected = QFileDialog.getExistingDirectory(parent, '选择资料库根目录（包含 data 子目录）', str(config.BASE_DIR))
    if not selected:
        return False
    try:
        info = inspect_root(selected)
        if not info['database_exists']:
            raise ProfileError('未找到 data/censor.db。请选择包含 data 子目录的资料库根目录，不能选择 data 子目录本身。')
        detail = (f"下次普通启动使用：\n{info['root']}\n\n"
                  f"扫描 {info['scans']} 条 · 人工确认 {info['labels']} 条\n"
                  '本次运行仍使用当前库；不会移动、复制或覆盖任何数据库。是否登记？')
        if QMessageBox.question(parent, '登记默认资料库', detail,
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return False
        write_local_pointer(config.program_dir(), selected)
    except (ProfileError, OSError) as exc:
        QMessageBox.warning(parent, '资料库未切换', str(exc))
        return False
    suffix = ('\n当前启动指定了 data-dir 或便携模式；带相同参数启动仍优先使用显式目录。'
              if os.environ.get(config.DATA_DIR_ENV) or config.portable_mode() else '')
    QMessageBox.information(parent, '已登记', '默认资料库已保存，普通重启后生效。原数据均保留。' + suffix)
    return True


def recover_profile(message):
    import config
    box = QMessageBox(QMessageBox.Warning, '资料库未能打开', message +
        '\n\n请选择已有资料库，或在没有本地资料时明确新建。')
    select = box.addButton('选择已有资料库', QMessageBox.ActionRole)
    new = None
    program = config.program_dir()
    if not (program / 'profile.json').exists() and not (program / 'data/censor.db').exists():
        new = box.addButton('新建程序文件夹内的资料库', QMessageBox.AcceptRole)
    cancel = box.addButton(QMessageBox.Cancel)
    box.setDefaultButton(cancel)
    box.exec()
    if new is not None and box.clickedButton() is new:
        return 'new_local'
    return box.clickedButton() is select and select_existing_profile()


def release_startup_profile():
    global _startup_lock
    if _startup_lock is not None:
        _startup_lock.release()
        _startup_lock = None


def prepare_startup_profile(*, create=False):
    global _startup_lock
    import config
    for attempt in range(2):
        try:
            explicit = os.environ.get(config.DATA_DIR_ENV)
            root, may_create = startup_selection(config.program_dir(), explicit_root=explicit,
                create=create, legacy_registry=config.user_data_root() / PROFILE_FILENAME, choose=choose_profile)
            if may_create:
                root.mkdir(parents=True, exist_ok=True)
            lock = ProfileLock(root).acquire()
            try:
                open_profile(root, create=may_create)
                if not explicit and root != config.program_dir().resolve():
                    write_local_pointer(config.program_dir(), root)
            except BaseException:
                lock.release()
                raise
            _startup_lock = lock
            config.set_data_root(root)
            return True
        except (ProfileError, OSError) as exc:
            # Explicit launch arguments cannot be repaired by saving a different
            # local pointer; make that failure actionable instead of looping.
            answer = recover_profile(str(exc)) if not explicit and not attempt else False
            if answer == 'new_local':
                create = True
                continue
            if explicit or attempt or not answer:
                if explicit:
                    QMessageBox.critical(None, '指定资料库不可用', str(exc))
                return False
    return False


def show_profile_manager(parent=None):
    import config
    dialog = QDialog(parent)
    dialog.setWindowTitle('资料库与历史数据')
    dialog.setMinimumWidth(620)
    layout = QVBoxLayout(dialog)
    location = QLabel('当前资料库根目录：\n' + str(config.BASE_DIR) + '\n\n数据库：\n' + str(config.DATA_DIR / 'censor.db'))
    location.setTextFormat(Qt.PlainText)
    location.setTextInteractionFlags(Qt.TextSelectableByMouse)
    location.setWordWrap(True)
    layout.addWidget(location)
    details = QPlainTextEdit()
    details.setReadOnly(True)
    details.setMinimumHeight(230)
    def refresh_diagnostics():
        try:
            info = profile_diagnostics(config.BASE_DIR, config.program_dir() / PROFILE_FILENAME)
            text = format_profile_diagnostics(info)
        except (ProfileError, OSError) as exc:
            text = str(exc)
        details.setPlainText(text)
    refresh_diagnostics()
    layout.addWidget(details)
    refresh_button = QPushButton('刷新资料库诊断')
    refresh_button.clicked.connect(refresh_diagnostics)
    layout.addWidget(refresh_button)
    copy_button = QPushButton('复制资料库诊断信息')
    copy_button.clicked.connect(lambda: QApplication.clipboard().setText(details.toPlainText()))
    layout.addWidget(copy_button)
    note = QLabel('默认资料保存在程序文件夹内；开发或外部资料库由当前程序的本地指针指定。\n'
                  '停止、关闭页面或清空列表不会删除数据库；不同独立库不会自动覆盖合并。\n'
                  '切换目录下次启动生效，运行中不会混用两个资料库。')
    note.setWordWrap(True); layout.addWidget(note)
    open_button = QPushButton('打开当前资料库目录')
    open_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(config.BASE_DIR))))
    layout.addWidget(open_button)
    switch_button = QPushButton('选择已有资料库作为下次启动默认')
    switch_button.clicked.connect(lambda: select_existing_profile(dialog))
    layout.addWidget(switch_button)
    close = QDialogButtonBox(QDialogButtonBox.Close)
    close.rejected.connect(dialog.reject); layout.addWidget(close)
    dialog.exec()
