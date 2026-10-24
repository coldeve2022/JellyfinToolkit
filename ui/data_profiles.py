"""Choose a persistent library before services start, and expose its location."""
import os

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QInputDialog, QLabel,
    QMessageBox, QPushButton, QVBoxLayout,
)

from utils.data_profile import PROFILE_FILENAME, ProfileError, inspect_root, prepare_profile, remember_profile


def choose_profile(rows):
    labels = [f"人工确认 {row['labels']} · 扫描 {row['scans']}\n{row['root']}" for row in rows]
    label, accepted = QInputDialog.getItem(None, '选择继续使用的资料库',
        '发现多个独立资料库。选择一次后，各版本继续使用同一个库；不复制或覆盖其他库。',
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
        remember_profile(selected, config.user_data_root() / PROFILE_FILENAME)
    except (ProfileError, OSError) as exc:
        QMessageBox.warning(parent, '资料库未切换', str(exc))
        return False
    suffix = ('\n当前启动指定了 data-dir 或便携模式；带相同参数启动仍优先使用显式目录。'
              if os.environ.get(config.DATA_DIR_ENV) or config.portable_mode() else '')
    QMessageBox.information(parent, '已登记', '默认资料库已保存，普通重启后生效。原数据均保留。' + suffix)
    return True


def recover_profile(message):
    answer = QMessageBox.warning(None, '资料库未能打开', message +
        '\n\n不会静默新建空库。请恢复原目录，或选择已有资料库后重试。',
        QMessageBox.Retry | QMessageBox.Cancel, QMessageBox.Cancel)
    return answer == QMessageBox.Retry and select_existing_profile()


def prepare_startup_profile():
    import config
    arguments = dict(default_root=config.BASE_DIR, registry=config.user_data_root() / PROFILE_FILENAME,
                     program_root=config.program_dir(), shared_root=config.user_data_root(),
                     explicit=bool(os.environ.get(config.DATA_DIR_ENV)), portable=config.portable_mode(),
                     choose=choose_profile)
    try:
        root = prepare_profile(**arguments)
    except (ProfileError, OSError) as exc:
        if not recover_profile(str(exc)):
            return False
        try:
            root = prepare_profile(**arguments)
        except (ProfileError, OSError) as retry_error:
            QMessageBox.critical(None, '资料库仍不可用', str(retry_error))
            return False
    # No services have been constructed yet. Rebind any import-time aliases.
    config.set_data_root(root)
    return True


def show_profile_manager(parent=None):
    import config
    dialog = QDialog(parent)
    dialog.setWindowTitle('资料库与历史数据')
    dialog.setMinimumWidth(620)
    layout = QVBoxLayout(dialog)
    location = QLabel('当前资料库根目录：\n' + str(config.BASE_DIR) + '\n\n数据库：\n' + str(config.DATA_DIR / 'censor.db'))
    location.setTextInteractionFlags(Qt.TextSelectableByMouse)
    location.setWordWrap(True)
    layout.addWidget(location)
    try:
        info = inspect_root(config.BASE_DIR)
        text = f"数据库已保存：扫描 {info['scans']} 条 · 人工确认 {info['labels']} 条 · 历史 {info['history']} 条"
    except (ProfileError, OSError) as exc:
        text = str(exc)
    counts = QLabel(text); counts.setWordWrap(True); layout.addWidget(counts)
    note = QLabel('资料库独立于程序版本，升级继续读取同一目录。\n'
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
