from utils.file_scan import iter_media_files


def test_auxiliary_tokens_and_root_folders_are_excluded(tmp_path):
    for name in ("ABC-trailer-1080p.mp4", "sample_001.mp4", "预告片_ABC.mp4", "ABC.mp4"):
        (tmp_path / name).touch()
    extras = tmp_path / "trailers"
    extras.mkdir()
    (extras / "ABC.mp4").touch()
    assert [p.split("\\")[-1] for p in iter_media_files([tmp_path, extras])] == ["ABC.mp4"]


def test_job_survives_restart_and_retries_interrupted_file(tmp_path):
    from censor.jobs import ScanJobs
    from censor.models import VideoVerdict, ProcessingStatus

    jobs = ScanJobs(tmp_path / "jobs.db")
    jobs.enqueue(["one", "two"])
    jobs.started("one")
    restarted = ScanJobs(tmp_path / "jobs.db")
    assert set(restarted.pending()) == {"one", "two"}
    restarted.finished(VideoVerdict(path="one"))
    restarted.finished(VideoVerdict(path="two", status=ProcessingStatus.FAILED, error="broken"))
    assert restarted.pending() == []
    assert restarted.retry_failed() == 1
    assert restarted.pending() == ["two"]


def test_nfo_priority_is_a_clue_not_a_verdict(tmp_path):
    from censor.jobs import scan_priority

    for name, tag in [("first", "有码"), ("later", "无码破解"), ("both", "有码</tag><tag>无码")]:
        path = tmp_path / (name + ".mp4")
        path.touch()
        path.with_suffix(".nfo").write_text(f"<movie><tag>{tag}</tag></movie>", encoding="utf-8")
    assert scan_priority(str(tmp_path / "first.mp4"))[0] == 0
    assert scan_priority(str(tmp_path / "both.mp4"))[0] == 0
    assert scan_priority(str(tmp_path / "plain.mp4"))[0] == 1
    assert scan_priority(str(tmp_path / "later.mp4"))[0] == 2


def test_normal_titles_are_not_attachment_substrings(tmp_path):
    from pathlib import Path

    for name in ("Trailer Park Boys.mp4", "Sample Movie.mp4", "previewing.mp4"):
        (tmp_path / name).touch()
    assert {Path(p).name for p in iter_media_files([tmp_path])} == {
        "Trailer Park Boys.mp4",
        "Sample Movie.mp4",
        "previewing.mp4",
    }


def test_history_is_available_after_reopening(qapp, qtbot, tmp_path):
    from config import ToolkitConfig
    from ui.pages.censorship import CensorshipPage
    from censor.models import VideoVerdict

    page = CensorshipPage(ToolkitConfig())
    page._service.store.put(VideoVerdict(path=str(tmp_path / "sample.mp4")))
    reopened = CensorshipPage(ToolkitConfig())
    reopened._load_history()
    qtbot.waitUntil(lambda: reopened.table.rowCount() == 1, timeout=3000)
    reopened._clear()
    assert reopened._service.store.history()[0].path.endswith("sample.mp4")
    page.deleteLater()
    reopened.deleteLater()


def test_worker_reports_preparation_failure_and_always_finishes(tmp_path):
    from censor.service import CensorService, CensorOptions
    from ui.pages.censorship import CensorScanWorker

    worker = CensorScanWorker(CensorService({}, data_dir=tmp_path), ["synthetic"], CensorOptions())
    completed, errors = [], []
    worker.all_done.connect(completed.append)
    worker.error.connect(errors.append)

    def broken(*args):
        raise OSError("database unavailable")

    worker.jobs.enqueue = broken
    worker.run()
    assert completed == [0]
    assert "database unavailable" in errors[0]


def test_worker_prioritizes_nfo_and_reuses_completed_analysis(tmp_path):
    from censor.service import CensorService, CensorOptions
    from ui.pages.censorship import CensorScanWorker

    first, later = tmp_path / "first.mp4", tmp_path / "later.mp4"
    for path, tag in [(first, "有码"), (later, "无码")]:
        path.write_bytes(b"synthetic")
        path.with_suffix(".nfo").write_text(f"<movie><tag>{tag}</tag></movie>", encoding="utf-8")
    service = CensorService({"censor_visual_enabled": False}, data_dir=tmp_path / "state")
    worker = CensorScanWorker(service, [str(later), str(first)], CensorOptions(detect_visual=False), workers=1)
    results = []
    worker.item_done.connect(results.append)
    worker.run()
    assert [r.path for r in results] == [str(first)]
    assert worker.jobs.deferred()[0][0] == str(later)
    assert worker.jobs.pending() == []
    assert worker.jobs.unfinished_sources() == []
    second = CensorScanWorker(service, [str(first)], CensorOptions(detect_visual=False))
    cached = []
    second.item_done.connect(cached.append)
    second.run()
    assert cached[0].elapsed == 0


def test_stop_during_discovery_preserves_source_and_discovered_queue(tmp_path, monkeypatch):
    from censor.service import CensorService, CensorOptions
    from ui.pages.censorship import CensorScanWorker

    path = tmp_path / "sample.mp4"
    path.touch()
    worker = CensorScanWorker(
        CensorService({}, data_dir=tmp_path / "state"), [str(tmp_path)], CensorOptions()
    )

    def enumerate_slow(*args, **kwargs):
        yield str(path)
        worker.stop()

    monkeypatch.setattr("utils.file_scan.iter_media_files", enumerate_slow)
    worker.run()
    assert worker.jobs.unfinished_sources() == [str(tmp_path)]
    assert worker.jobs.pending() == [str(path)]


def test_selecting_history_does_not_read_nfo(qapp, tmp_path, monkeypatch):
    from config import ToolkitConfig
    from ui.pages.censorship import CensorshipPage
    from censor.models import VideoVerdict

    page = CensorshipPage(ToolkitConfig())
    page._on_item(VideoVerdict(path=str(tmp_path / "sample.mp4")))

    def forbidden(*args):
        raise AssertionError("NFO I/O on GUI thread")

    monkeypatch.setattr("ui.pages.censorship.scan_priority", forbidden)
    page.table.selectRow(0)
    page._on_row_selected()
    assert "历史记录暂无调度线索" in page.txt_evidence.toPlainText()
    page.deleteLater()


def test_missing_queued_video_becomes_retryable_failure(tmp_path):
    from censor.service import CensorService, CensorOptions
    from ui.pages.censorship import CensorScanWorker
    path = tmp_path / 'removed.mp4'
    worker = CensorScanWorker(CensorService({}, data_dir=tmp_path / 'state'),
                              [str(path)], CensorOptions())
    worker.jobs.enqueue([str(path)])
    worker.run()
    assert worker.jobs.pending() == []
    assert worker.jobs.unfinished_sources() == []
    assert worker.jobs.stats()['failed'] == 1
    assert worker.jobs.retry_failed() == 1
