"""扫描结果复用 + 扫描汇总(损坏/不兼容/正常计数) 单元测试。

覆盖用户反馈："点开始修复为什么又扫描一遍？扫描完了也没有总结"。
- 扫描结果应被缓存，修复时直接复用（precomputed），不重复 ffprobe 分析
- 扫描完成自动汇总：损坏/不兼容/正常各多少、原因
- 页面从"只有日志框"升级为"汇总卡片 + 结果表格"
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    return app


def _result(corrupted=False, compatible=True, codec="h264", error=""):
    return {
        "is_corrupted": corrupted,
        "is_compatible": compatible,
        "video_codec": codec,
        "audio_codec": "aac",
        "duration": 1.0, "width": 0, "height": 0, "file_size": 0,
        "error": error,
    }


def _make_page(qapp, tmp_path):
    """用 __new__ 绕过 _setup_ui，只装配 `_on_scan_done` 依赖的最小对象。"""
    import config
    # 走 set_data_root：直接赋值 config.DATA_DIR 不会同步到
    # `from config import DATA_DIR` 的模块，也会破坏重绑用的对象身份。
    config.set_data_root(tmp_path)

    from ui.pages.repair import RepairPage
    from ui.widgets import LogPanel

    page = RepairPage.__new__(RepairPage)
    page._scanning = True
    page._scan_files_list = ["D:/a.mp4", "D:/b.mp4", "D:/c.mp4"]
    page._scan_results = {
        "D:/a.mp4": _result(corrupted=True, error="moov atom not found"),
        "D:/b.mp4": _result(corrupted=False, compatible=False, codec="hevc"),
        "D:/c.mp4": _result(corrupted=False, compatible=True, codec="h264"),
    }
    page.log_panel = LogPanel()
    page.summary_label = type("L", (), {"setText": lambda self, t: setattr(self, "_t", t)})()
    page.status_label = type("L", (), {"setText": lambda self, t: setattr(self, "_t", t)})()
    # btn 只需 setEnabled
    page.btn_scan = type("B", (), {"setEnabled": lambda self, x: setattr(self, "_e", x)})()
    page.btn_start = type("B", (), {"setEnabled": lambda self, x: setattr(self, "_e", x)})()
    page.file_list = type("F", (), {
        "get_all_paths": lambda self: ["D:/a.mp4", "D:/b.mp4", "D:/c.mp4"],
    })()
    return page


def test_summary_counts_categories(qapp, tmp_path):
    """损坏/不兼容/正常 各计数正确，status_label 更新。"""
    page = _make_page(qapp, tmp_path)
    page._on_scan_done()
    assert page.status_label._t.startswith("扫描完成:")
    # 1 损坏 + 1 不兼容 + 1 正常 = 3
    assert "损坏 1" in page.status_label._t
    assert "不兼容 1" in page.status_label._t
    assert "正常 1" in page.status_label._t


def test_summary_card_render_top_categories(qapp, tmp_path):
    """汇总卡片 HTML 含三类计数与总数。"""
    page = _make_page(qapp, tmp_path)
    page._on_scan_done()
    html = page.summary_label._t
    assert "共 3 个文件" in html
    assert "损坏 1" in html
    assert "不兼容 1" in html
    assert "正常 1" in html


def test_summary_empty_results_handled(qapp, tmp_path):
    """扫描结果为空时不崩溃，给出提示。"""
    page = _make_page(qapp, tmp_path)
    page._scan_results = {}
    page._on_scan_done()
    assert "未扫描到任何有效文件" in page.summary_label._t


def test_summary_turns_off_scanning_and_reenables_scan(qapp, tmp_path):
    """扫描结束：_scanning=False，扫描按钮恢复可用。"""
    page = _make_page(qapp, tmp_path)
    assert page._scanning is True
    page._on_scan_done()
    assert page._scanning is False
    assert page.btn_scan._e is True


def test_log_panel_contains_detailed_problem_files(qapp, tmp_path):
    """日志内应列出具体问题文件与原因，便于排查。"""
    page = _make_page(qapp, tmp_path)
    page._on_scan_done()
    text = page.log_panel.toPlainText()
    assert "a.mp4" in text          # 损坏文件列出
    assert "moov atom not found" in text
    assert "b.mp4" in text          # 不兼容文件列出
    assert "hevc" in text


# ── worker precomputed 复用：不重复 ffprobe 分析 ──────────────────────
def _fake_analyze(path):
    raise AssertionError("若复用 precomputed，不应再调用 analyze_video")


def test_worker_auto_precomputed_reuses_without_reanalyze(monkeypatch):
    """预扫描结果存在时，auto 模式直接用 precomputed 决策，不再 analyze_video。"""
    from workers.ffmpeg import FFmpegRepairWorker
    import os
    # 把 analyze_video 换成会抛错的哨兵——若被调用即说明未复用
    monkeypatch.setattr("workers.ffmpeg.analyze_video", _fake_analyze)
    # is_compatible=True 的文件 → 应"跳过"，不会走 repair/transcode(避免真调 ffmpeg)
    files = ["D:/ok.mp4"]
    w = FFmpegRepairWorker(
        files, "auto", "libx264", "none", crf=30,
        precomputed={os.path.normpath(files[0]): _result(corrupted=False, compatible=True)},
    )
    logs: list[str] = []
    w.log.connect(logs.append)
    w.run()   # 纯逻辑：兼容文件直接跳过，不触发 analyze，也不会真转码
    assert any("跳过" in l for l in logs), logs


# ── worker log_skips：复用扫描结果时正常文件静默跳过（不刷屏） ─────────────
def test_worker_log_skips_false_suppresses_skip_logs(monkeypatch):
    """precomputed + log_skips=False：兼容文件静默跳过，不再逐条刷"跳过"日志。"""
    from workers.ffmpeg import FFmpegRepairWorker
    import os
    monkeypatch.setattr("workers.ffmpeg.analyze_video", _fake_analyze)
    files = ["D:/ok1.mp4", "D:/ok2.mp4"]   # 两个正常文件
    w = FFmpegRepairWorker(
        files, "auto", "libx264", "none", crf=30,
        precomputed={os.path.normpath(f): _result(corrupted=False, compatible=True)
                     for f in files},
        log_skips=False,
    )
    logs: list[str] = []
    w.log.connect(logs.append)
    w.run()
    # 不应再出现逐条刷屏日志("处理:" 或 "兼容且正常，跳过")
    assert not any("处理:" in l for l in logs), logs
    assert not any("兼容且正常，跳过" in l for l in logs), logs
    # 进度应推进到 100%（正常文件确实被跳过计数）
    assert any("跳过: 2" in l for l in logs), logs  # 汇总行含跳过计数


# ── worker 超时：损坏文件不再无限挂起，超时后保留原文件 ─────────────
def test_worker_config_timeout_propagation(monkeypatch):
    """timeout 参数应被正确接收并归一化（<30 时兜底为 30）。"""
    from workers.ffmpeg import FFmpegRepairWorker
    w = FFmpegRepairWorker([], "auto", "libx264", "none", crf=30, timeout=5)
    assert w._timeout == 30      # 兜底下限
    w2 = FFmpegRepairWorker([], "auto", "libx264", "none", crf=30, timeout=600)
    assert w2._timeout == 600


def test_worker_repair_timeout_kills_and_keeps_original(tmp_path, monkeypatch):
    """无超时的 wait() 会卡死队列——改为带超时后，超时文件被 kill + 保留原文件。

    用 monkeypatch 把 subprocess.Popen 换成"永不返回"的假进程，模拟 ffmpeg
    在处理受损文件(如 contradiction STSC/STCO)时无限挂起的场景。timeout=1 应
    kill 它并返回，不卡死队列、不破坏原文件。
    """
    import workers.ffmpeg as wf
    import subprocess

    class HangingProc:
        """模拟一个永不退出的 ffmpeg 子进程：wait() 阻塞直到超时。"""
        def __init__(self, *a, **k):
            self.stdout = iter([])      # 无输出行，避免无限读阻塞
            self.returncode = -9
            self.killed = False
        def wait(self, timeout=None):
            # 永不自行返回，等 TimeoutExpired 由 worker 抛出
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout)
        def kill(self):
            self.killed = True

    calls = {"killed": 0}

    def fake_popen(*a, **k):
        p = HangingProc(*a, **k)
        # 让 kill() 记录次数并设置 returncode
        orig_kill = p.kill
        def _kill():
            calls["killed"] += 1
            p.returncode = -9
            return orig_kill()
        p.kill = _kill
        return p

    monkeypatch.setattr(wf.subprocess, "Popen", fake_popen)

    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"\x00" * 100)
    before = bad.read_bytes()

    w = wf.FFmpegRepairWorker([str(bad)], "repair", "libx264", "none", crf=30, timeout=1)
    logs: list[str] = []
    w.log.connect(logs.append)
    w.run()
    # 超时后 worker 应返回（不卡死）
    assert calls["killed"] == 1, "超时应 kill 挂起的 ffmpeg 进程"
    assert any("超时" in l for l in logs), logs
    # 原文件不受影响，无临时残留
    assert bad.read_bytes() == before
    assert not list(tmp_path.glob("temp_*"))
    # 失败计数（超时视为处理失败）
    assert any("失败: 1" in l for l in logs), logs


# ── _select_repair_queue：只挑问题文件，正常完全不进队列 ─────────────
def test_select_queue_auto_only_problem_files(qapp, tmp_path):
    """auto 模式：只有损坏/不兼容文件进队列，正常文件被剔除。"""
    page = _make_page(qapp, tmp_path)
    q = page._select_repair_queue("auto")
    # a=损坏, b=不兼容 进队列；c=正常 剔除
    assert sorted(q) == ["D:/a.mp4", "D:/b.mp4"], q


def test_select_queue_repair_only_corrupted(qapp, tmp_path):
    """repair 模式：只修损坏的，不兼容的留给转码不进来。"""
    page = _make_page(qapp, tmp_path)
    q = page._select_repair_queue("repair")
    assert q == ["D:/a.mp4"], q


def test_select_queue_transcode_only_incompatible(qapp, tmp_path):
    """transcode 模式：只转码不兼容的，损坏的交给修复不进来。"""
    page = _make_page(qapp, tmp_path)
    q = page._select_repair_queue("transcode")
    assert q == ["D:/b.mp4"], q


def test_select_queue_empty_results_returns_empty(qapp, tmp_path):
    """无扫描结果（全量修复）时返回空，由调用方回退。"""
    page = _make_page(qapp, tmp_path)
    page._scan_results = {}
    assert page._select_repair_queue("auto") == []
    assert page._select_repair_queue("repair") == []
    assert page._select_repair_queue("transcode") == []
