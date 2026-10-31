"""GUI 冒烟测试 — offscreen 模式下逐页构建 + 核心交互 + 收尾安全。

为什么要 pytest 化：仓库里原先叫 ``tests/gui_smoke.py``，看着像测试，
但 pytest 只收 ``test_*.py`` / ``*_test.py``，**CI 从来没有跑过它**。
而正是它能挡住"打包即崩"这类问题 —— 页面构造期的 AttributeError
在窗口版 exe 里没有控制台，只会表现为"主窗口不显示"。

配套：``tests/conftest.py`` 已把数据目录重定向到临时目录，
所以这里的存档/状态文件不会落到项目里。
"""

from __future__ import annotations

import importlib
import os
import tempfile
import time
from pathlib import Path

import pytest

from PySide6.QtWidgets import QApplication

from config import ToolkitConfig
from ui.main_window import MainWindow, NAV_LABELS

# 页面构造清单：(模块, 类名)
PAGE_SPECS = [
    ("ui.pages.onboarding", "OnboardingPage"),
    ("ui.pages.insight", "InsightPage"),
    ("ui.pages.profile", "ProfilePage"),
    ("ui.pages.review", "ReviewPage"),
    ("ui.pages.delete", "DeletePage"),
    ("ui.pages.dedup", "DedupPage"),
    ("ui.pages.censorship", "CensorshipPage"),
    ("ui.pages.censor_replace", "CensorReplacePage"),
    ("ui.pages.subtitle", "SubtitlePage"),
    ("ui.pages.subtitle_gen", "SubtitleGenPage"),
    ("ui.pages.replace", "ReplacePage"),
    ("ui.pages.lada", "LadaPage"),
    ("ui.pages.repair", "RepairPage"),
    ("ui.pages.nfo_fix", "NFOPage"),
    ("ui.pages.db_tools", "DBToolsPage"),
    ("ui.pages.merge", "MergePage"),
    ("ui.pages.automation", "AutomationPage"),
    ("ui.pages.settings", "SettingsPage"),
    ("ui.pages.tasks", "TaskCenterPage"),
]


@pytest.fixture()
def cfg():
    return ToolkitConfig.load()


def _wait_worker(worker, timeout_s: float = 20.0) -> bool:
    """等 QThread 结束并泵事件循环。"""
    deadline = time.monotonic() + timeout_s
    while worker.isRunning() and time.monotonic() < deadline:
        QApplication.processEvents()
        worker.wait(50)
    QApplication.processEvents()
    return not worker.isRunning()


# ── 1. 页面构造 / 导航一致性 ────────────────────────────────
@pytest.mark.parametrize("module_name,cls_name", PAGE_SPECS)
def test_page_constructs_and_shuts_down(qapp, cfg, module_name, cls_name):
    page_cls = getattr(importlib.import_module(module_name), cls_name)
    page = page_cls(cfg)
    assert page is not None
    shutdown = getattr(page, "shutdown", None)
    if callable(shutdown):
        shutdown()          # 没有 worker 在跑时必须是安全的空操作
    QApplication.processEvents()


def test_nav_index_matches_pages(qapp, cfg):
    """侧边栏条目数必须与页面清单一致。

    只注册部分页面时 ``_navigate(NAV_LABELS.index(...))`` 会静默切到错误页面，
    截图/交互测试会拍到别的页 —— 所以这里必须断言相等。
    """
    assert len(NAV_LABELS) == len(PAGE_SPECS), (
        f"侧边栏有 {len(NAV_LABELS)} 项，页面清单有 {len(PAGE_SPECS)} 项；"
        "新增页面时忘了同步 ui/main_window.py 的 NAV_ITEMS 或本测试的 PAGE_SPECS")


def test_main_window_registers_and_navigates(qapp, cfg):
    window = MainWindow(cfg)
    for module_name, cls_name in PAGE_SPECS:
        window.register_page(getattr(importlib.import_module(module_name), cls_name)(cfg))
    window.show()
    assert window.stack.count() == len(PAGE_SPECS)

    for i in range(window.stack.count()):
        window._navigate(i)
        QApplication.processEvents()
        assert window.stack.currentIndex() == i

    window.close()
    QApplication.processEvents()


def test_close_event_stops_all_workers(qapp, cfg):
    """关闭主窗口要优雅停掉所有后台线程，否则会 QThread destroyed while running 崩溃。"""
    window = MainWindow(cfg)
    for module_name, cls_name in PAGE_SPECS:
        window.register_page(getattr(importlib.import_module(module_name), cls_name)(cfg))
    window.show()
    window.close()
    QApplication.processEvents()


# ── 2. 有码检测页 ──────────────────────────────────────────
def test_censorship_page_classifies(qapp, cfg, tmp_path, qtbot):
    """元数据模式下，所有文件都保持待确认，不冒充画面结论。"""
    from ui.pages.censorship import CensorshipPage

    # 关掉抽帧，只验证证据链与界面回填（画面检测另有独立用例）
    cfg.censor_visual_enabled = False
    cfg.censor_detectors = ["series.rules", "filename.rules"]

    page = CensorshipPage(cfg)
    (tmp_path / "FC2-PPV-123456.mp4").write_bytes(b"\x00" * 100)
    (tmp_path / "ABC-123.mp4").write_bytes(b"\x00" * 100)

    # Read-only scans are owned by this page, not a single global queue.
    # Unrelated pages must not cause this functional assertion to be skipped.
    page._worker = None
    page._start_scan([str(tmp_path)])
    qtbot.waitUntil(lambda: page._worker is not None, timeout=5000)
    # 必须用 waitUntil（内部会 processEvents）等行数：表格是被信号填的，
    # 用 worker.wait() 阻塞 GUI 线程会让队列信号送不到，行永远是 0。
    try:
        qtbot.waitUntil(lambda: page.table.rowCount() == 2, timeout=30000)
    except Exception:
        # 这条断言在"单独跑必过、全量跑偶发"时最难查，所以把现场打出来：
        # 到底是被其它用例的全局状态污染（检测器注册表 / 任务槽位），
        # 还是扫描自己失败。
        from censor.registry import detectors as _detectors
        from ui.services import TaskManager as _TM

        worker = page._worker
        running = None
        if worker is not None and hasattr(worker, "isRunning"):
            try:
                running = worker.isRunning()
            except RuntimeError:
                running = "invalid"
        log_panel = getattr(page, "log", None) or getattr(page, "log_panel", None)
        log_text = ""
        if log_panel is not None and hasattr(log_panel, "toPlainText"):
            log_text = log_panel.toPlainText()[-600:]
        active = [
            (entry["record"].get("title"), entry["record"].get("status"))
            for entry in _TM.instance()._active.values()
        ]
        raise AssertionError(
            "扫描没有产出 2 行 —— 现场：\n"
            f"  worker={type(worker).__name__ if worker else None} running={running}\n"
            f"  stopped={getattr(worker, '_stopped', None)}\n"
            f"  results={len(page._results)} rows={page.table.rowCount()}\n"
            f"  状态栏={getattr(page, '_stats_label', None) and page._stats_label.text()!r}\n"
            f"  已注册检测器={sorted(_detectors.names())}\n"
            f"  任务占位={active}\n"
            f"  页面日志尾部={log_text!r}"
        ) from None
    assert _wait_worker(page._worker)
    qapp.processEvents()

    # 结论列只表达"有没有码"，处理列表达"还需不需要人"——两列分开断言，
    # 元数据模式既不能冒充画面结论（结论=未定），也不能自动当成处理完（处理=待复核）
    conclusions, buckets = {}, {}
    for row in range(page.table.rowCount()):
        name = page.table.item(row, 0).text()
        conclusions[name] = page.table.item(row, 2).text()
        buckets[name] = page.table.item(row, 3).text()
    for name in ("FC2-PPV-123456.mp4", "ABC-123.mp4"):
        assert conclusions[name] == "未定"
        assert buckets[name] == "待复核"

    # TaskManager restores controls after its queued delivery barrier, which
    # can finish later than QThread.wait(). Clearing while busy is disallowed.
    from ui.services import busy
    qtbot.waitUntil(lambda: not busy(page), timeout=2000)

    page._clear()
    assert page.table.rowCount() == 0
    assert page.queued_paths() == []


# ── 3. 字幕缺失检测页（含断点续传） ─────────────────────────
def test_subtitle_page_scan_and_resume(qapp, cfg, tmp_path):
    from config import DATA_DIR
    from ui.pages.subtitle import SubtitlePage

    page = SubtitlePage(cfg)
    page._clear_all()          # 清掉可能存在的历史状态，保证从干净状态开始

    movie = tmp_path / "Movie.mp4"
    movie.write_bytes(b"\x00" * (11 * 1024 * 1024))     # > 默认 10MB 阈值
    with_sub = tmp_path / "HasSub.mp4"
    with_sub.write_bytes(b"\x00" * (11 * 1024 * 1024))
    (tmp_path / "HasSub.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")

    page._on_dirs_dropped([str(tmp_path)])
    assert len(page._directories) == 1
    page.spin_size.setValue(10)

    page._start_scan()
    assert _wait_worker(page._worker)
    assert page.result_list.count() == 1, "只有 Movie.mp4 缺字幕"
    assert page.result_list.item(0).data(256) == str(movie)

    # 断点续传：再扫一次不应重复入列
    page._start_scan()
    assert _wait_worker(page._worker)

    page._save_state()
    assert (Path(DATA_DIR) / "subtitle_state.json").exists(), "状态必须落在数据目录"
    assert not (Path(__file__).resolve().parents[1] / "subtitle_state.json").exists(), \
        "状态文件绝不能写进项目目录"


# ── 4. 破解替换页 ──────────────────────────────────────────
def test_replace_page_worker_and_dragdrop(qapp, cfg, tmp_path, monkeypatch):
    from ui.pages.replace import ReplacePage, ReplaceWorker
    # This legacy test checks pairing and recoverable file transactions. Real
    # valid/invalid-video validation is exercised in media remediation fixtures.
    monkeypatch.setattr('utils.replacement.validate_restored_output', lambda *_: (True, ''))

    page = ReplacePage(cfg)
    lib = tmp_path / "lib"
    new = tmp_path / "new"
    lib.mkdir()
    new.mkdir()
    original = lib / "ABC-123.mp4"
    original.write_bytes(b"original content")
    restored = new / "ABC-123.restored.mp4"
    restored.write_bytes(b"new content")

    worker = ReplaceWorker([str(new)], [str(lib)], "restored")
    result: dict = {}
    worker.finished.connect(lambda r, n: result.update(replaced=r, not_found=n))
    worker.run()                       # 同步运行，不启线程
    assert result.get("replaced") == 1
    assert original.read_bytes() == b"new content"
    assert not restored.exists()
    assert not (lib / "ABC-123.mp4.bak").exists()

    (new / "XYZ-999.restored.mp4").write_bytes(b"x")
    worker2 = ReplaceWorker([str(new)], [str(lib)], "restored")
    result2: dict = {}
    worker2.finished.connect(lambda r, n: result2.update(replaced=r, not_found=n))
    worker2.run()
    assert result2.get("not_found") == 1

    # 拖拽链路：files_dropped 必须有接收者，且要按路径去重
    page._clear()
    assert page.list_restored.count() == 0 and page.list_library.count() == 0
    page.list_restored.files_dropped.emit([str(new)])
    page.list_library.files_dropped.emit([str(lib)])
    assert page.list_restored.count() == 1
    assert page.list_library.count() == 1
    page.list_restored.files_dropped.emit([str(new)])
    assert page.list_restored.count() == 1, "同一路径重复拖入应去重"
    assert page.list_restored.get_all_paths() == [str(new)]


# ── 5. 修复转码页 ──────────────────────────────────────────
def test_repair_page_state_transitions(qapp, qtbot, cfg, tmp_path):
    from ui.pages.repair import RepairPage

    page = RepairPage(cfg)
    assert page.cb_repair.isChecked() and page.cb_transcode.isChecked()
    # 「自动」+ 8 个编码器选项；编码器缺省是自动（不再写死 nvenc）
    assert page.combo_codec.count() == 9
    assert page.combo_codec.itemData(0) == "", "第一项必须是「自动」"
    assert not page.btn_start.isEnabled()
    assert hasattr(page, "summary_label")
    assert page.result_table.columnCount() == 4

    (tmp_path / "test.mp4").write_bytes(b"\x00" * 100)
    page._on_files_dropped([str(tmp_path)])
    qtbot.waitUntil(lambda: page.file_list.count() == 1, timeout=3000)
    assert page.file_list.count() == 1
    assert page.btn_start.isEnabled(), "拖入文件后应无需先扫描即可开始"
    assert page.btn_scan.isEnabled()

    page._clear()
    assert not page.btn_start.isEnabled()
    assert page.summary_label.text() == "尚未扫描"


# ── 6. NFO 关联修复页 ──────────────────────────────────────
def test_nfo_fix_worker_renames_and_fixes(qapp, tmp_path):
    from ui.pages.nfo_fix import NFOWorker

    (tmp_path / "Movie.ts.nfo").write_text("Movie.ts", encoding="utf-8")
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "Sub.ts.nfo").write_text("<x>Sub.ts</x>", encoding="utf-8")

    worker = NFOWorker(str(tmp_path))
    result: dict = {}
    worker.finished.connect(lambda f, r: result.update(fixed=f, renamed=r))
    worker.run()

    assert result.get("renamed") == 2 and result.get("fixed") == 2, result
    renamed = tmp_path / "Movie.mp4.nfo"
    assert renamed.exists()
    assert ".ts" not in renamed.read_text(encoding="utf-8")


def test_settings_save_roundtrip(qapp, cfg, monkeypatch, tmp_path):
    """真正走一遍「保存设置」。

    这条用例是有来历的：`input_codec` / `input_hw` 从 QLineEdit 换成可编辑
    QComboBox 之后，`_save()` 里仍写着 `.text()` —— QComboBox 没有 text() 属性，
    用户一点保存就会崩。当时没有任何测试调用 `_save()`，所以没人发现。
    """
    from PySide6.QtWidgets import QMessageBox

    from ui.pages.settings import SettingsPage
    from utils import tools

    # 把弹窗挡掉（离屏模式下模态框会阻塞事件循环）
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))

    page = SettingsPage(cfg)
    page.input_ffmpeg_path.setText("")
    page._fill_codec_combo("h264_qsv")          # 选中一个具体编码器
    page._fill_hw_combo("none")                 # 选中"不硬解"
    page.input_merge_backup.setText("")         # 留空 = 自动落到数据目录
    page.input_merge_subfolder.setText("_merged_originals")
    page.spin_crf.setValue(20)
    page.input_db.setText("C:/Demo/jellyfin.db")

    page._save()          # 旧实现会在这里 AttributeError

    assert page.cfg.ffmpeg_gpu_codec == "h264_qsv"
    assert page.cfg.ffmpeg_hardware_accel == "none"
    assert page.cfg.merge_backup_root == "", "留空必须保持空，不能塞 H:/"
    assert page.cfg.transcode_crf == 20
    assert page.cfg.jellyfin_db_path == "C:/Demo/jellyfin.db"

    # 「自动」选项的 data 是空串，保存后同样要保持空串
    page._fill_codec_combo("")
    page._fill_hw_combo("")
    page._save()
    assert page.cfg.ffmpeg_gpu_codec == ""
    assert page.cfg.ffmpeg_hardware_accel == ""

    from config import CONFIG_FILE
    assert CONFIG_FILE.exists(), "保存后配置文件应已落盘"
    tools.configure_tools("", force=True)


def test_settings_exposes_ffmpeg_path_field(qapp, cfg):
    """`merge_ffmpeg_path` 曾经有配置项却没有输入框，非 PATH 安装的用户无从填写。"""
    from ui.pages.settings import SettingsPage

    page = SettingsPage(cfg)
    assert hasattr(page, "input_ffmpeg_path")
    assert hasattr(page, "lbl_tool_status")
    assert hasattr(page, "btn_doctor")
    assert hasattr(page, "lbl_backup_hint")
    # 提示必须显示**实际**路径，而不是写死某个盘
    assert page.lbl_backup_hint.text()


# ── 7. 数据库工具页 / 设置页 / 主题 ────────────────────────
def test_db_tools_page_handles_missing_path(qapp, cfg, tmp_path):
    """路径不存在时不能崩，也不能去碰用户真实库。

    注意：这里**必须**等线程结束并调用 shutdown()。原实现里
    DBInspectWorker 没有中止手段，页面被回收而线程还在跑，
    Qt 会直接 fail-fast（0xC0000409）把进程打死。
    """
    from ui.pages.db_tools import DBToolsPage

    page = DBToolsPage(cfg)
    missing = tmp_path / "no-such.db"
    page.db_path_input.setText(str(missing))

    page._inspect_db()
    assert _wait_worker(page._inspector), "检查线程必须能结束"
    assert "不存在" in page.inspect_output.toPlainText()

    shutdown = getattr(page, "shutdown", None)
    if callable(shutdown):
        shutdown()
    QApplication.processEvents()


def test_db_tools_inspect_worker_stops(qapp, tmp_path):
    """中止请求必须让线程尽快退出（大库逐个 COUNT(*) 会很慢）。"""
    import sqlite3

    from ui.pages.db_tools import DBInspectWorker

    db = tmp_path / "demo.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE A (x INTEGER);"
        "CREATE TABLE B (y INTEGER);"
        "INSERT INTO A VALUES (1);")
    conn.commit()
    conn.close()

    worker = DBInspectWorker(str(db))
    worker.stop()                 # 先请求中止，再启动
    results: list[str] = []
    worker.result.connect(results.append)
    worker.start()
    assert _wait_worker(worker)
    assert results == [], "被中止时不应再发射结果给已回收的界面"


def test_settings_page_and_theme_roundtrip(qapp, cfg):
    from ui import theme as theme_mod
    from ui.pages.settings import SettingsPage

    page = SettingsPage(cfg)
    assert len(page.keywords_edit.toPlainText().strip()) > 0
    assert set(theme_mod.theme_names()) == {"dark", "light"}
    assert page.combo_theme.count() == 2

    theme_mod.set_current("light")
    assert theme_mod.theme()["bg_dark"] == "#FFFFFF"
    theme_mod.set_current("dark")
    assert theme_mod.theme()["bg_dark"] == "#161B22"
    theme_mod.set_current("不存在的主题")
    assert theme_mod.get_theme_name() == "dark", "非法主题应回退 dark"
    theme_mod.set_current(cfg.theme)


# ── 8. 通用组件 ────────────────────────────────────────────
def test_drag_drop_list_widget_dedup_and_delete(qapp, tmp_path):
    from ui.widgets import DragDropListWidget

    widget = DragDropListWidget()
    widget.add_item("a.mp4", r"C:\Demo\a.mp4")
    widget.add_item("b.mp4", r"C:\Demo\b.mp4")
    widget.add_item("a.mp4", r"C:\Demo\a.mp4")          # 重复
    assert widget.count() == 2
    assert widget.get_all_paths() == [r"C:\Demo\a.mp4", r"C:\Demo\b.mp4"]

    widget.setCurrentRow(0)
    widget.item(0).setSelected(True)
    widget._remove_selected()
    assert widget.count() == 1


# ── 9. 智能清理各页渲染（合成数据，无线程、无真实库） ───────
def _syn_item(iid, num, *, bucket=None, actors=(), fav=False, played=False,
              size=300 * 1024 * 1024, cd=False, year=2022):
    from utils.library import BUCKET_JAV, Item

    return Item(id=iid, name=f"{num}-{iid}", path=rf"C:\Demo\{num}-{iid}.mp4",
                type_cn="Movie", year=year, genres=["剧情"], tags=[],
                studios=["示例工作室"], actors=list(actors), size=size, num=num,
                bucket=bucket or BUCKET_JAV, is_cd=cd,
                is_favorite=fav, is_played=played)


def test_scoring_pages_render_with_synthetic_data(qapp, cfg, monkeypatch):
    from ui.pages.delete import DeletePage
    from ui.pages.dedup import DedupPage
    from ui.pages.insight import InsightPage
    from ui.pages.onboarding import OnboardingPage
    from ui.pages.profile import ProfilePage
    from ui.pages.review import ReviewPage
    from utils import profile as profile_mod
    from utils import scoring
    from utils.library import summarize

    syn = [
        _syn_item("p1", "DEMO-1", actors=["示例演员甲"], fav=True),
        _syn_item("s1", "DEMO-2", actors=["示例演员甲"]),
        _syn_item("a1", "DEMO-3", actors=["陌生演员ZZZ"]),
        _syn_item("j1", "junk", size=500 * 1024),          # < 10MB → junk
        _syn_item("c1", "DEMO-4", actors=["示例演员甲"], cd=True),
        _syn_item("c2", "DEMO-4", actors=["示例演员甲"], cd=True),
    ]
    results = scoring.rank_items(syn, {})
    counts = scoring.decision_counts(results)

    insight = InsightPage(cfg)
    insight._render_summary(summarize(syn))
    assert insight.metric_row.count() == 6
    insight.filter_combo.setCurrentText("年份")

    profile_page = ProfilePage(cfg)
    profile_page._on_done({"items": syn, "results": results, "counts": counts})
    assert len(profile_page.actor_chart._rows) >= 1
    assert "为你推荐" in profile_mod.profile_to_csv(profile_page.profile)

    review = ReviewPage(cfg)
    review._on_analyzed({"items": syn, "results": results, "counts": counts})
    review._clear_card()

    delete = DeletePage(cfg)
    delete._delete_rows = [
        {"num": "DEMO-3", "name": "DEMO-3-a1", "hit": "0%", "reason": "零命中",
         "size": 1, "path": r"C:\Demo\DEMO-3.mp4"},
        {"num": "", "name": "junk-still", "hit": "10%", "reason": "过小",
         "size": 2, "path": r"C:\Demo\junk.mp4"},
    ]
    delete._render_table()
    assert delete.table.rowCount() == 2
    delete._refresh_journal()

    dedup = DedupPage(cfg)
    dedup.groups = scoring.dedup_groups(syn)
    dedup._render_tree()
    assert dedup.tree.topLevelItemCount() == 1

    onboarding = OnboardingPage(cfg)
    onboarding._next()
    onboarding._next()
    assert onboarding.stack.currentIndex() == 2

    for page in (insight, profile_page, review, delete, dedup, onboarding):
        shutdown = getattr(page, "shutdown", None)
        if callable(shutdown):
            shutdown()
        QApplication.processEvents()
