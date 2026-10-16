"""Guard filesystem work and GUI batching, rather than fragile timing thresholds."""
import threading

import pytest

from utils import subtitle_clean


def test_scan_index_reads_each_directory_once(tmp_path, monkeypatch):
    import os

    for i in range(80):
        (tmp_path / f"film.{i}.mp4").touch()
        (tmp_path / f"film.{i}.chs.srt").touch()
        (tmp_path / f"poster.{i}.jpg").touch()
    calls = []
    original = os.scandir

    def scan(path):
        calls.append(os.path.normcase(str(path)))
        return original(path)

    monkeypatch.setattr(os, "scandir", scan)
    index = subtitle_clean.SubtitleIndex()
    for i in range(80):
        found = subtitle_clean.find_subtitles(tmp_path / f"film.{i}.mp4", ["srt"], index=index)
        assert [p.name for p in found] == [f"film.{i}.chs.srt"]
    assert len(calls) == len(set(calls))
    assert len(calls) <= 3


@pytest.mark.parametrize("name", ["film.srt", "film.zh-Hans.ass", "film.forced.vtt"])
def test_index_preserves_matching_and_refresh(tmp_path, name):
    video = tmp_path / "film.mp4"
    video.touch()
    folder = tmp_path / "Subs"
    folder.mkdir()
    sub = folder / name
    sub.touch()
    (tmp_path / "film-other.srt").touch()
    (tmp_path / "film.srt").mkdir()  # directories are not subtitles
    formats = ["srt", "ass", "vtt"]
    assert subtitle_clean.find_subtitles(video, formats, index=subtitle_clean.SubtitleIndex()) == [sub]
    sub.unlink()
    assert subtitle_clean.find_subtitles(video, formats, index=subtitle_clean.SubtitleIndex()) == []


def test_index_cancellation_during_directory_read(tmp_path):
    with pytest.raises(InterruptedError):
        subtitle_clean.find_subtitles(tmp_path / "film.mp4", index=subtitle_clean.SubtitleIndex(lambda: True))


def test_index_supports_custom_formats_and_output_mapping(tmp_path):
    from utils.file_ops import source_output_key

    video = tmp_path / "film.mp4"
    output = tmp_path / "output"
    mapped = output / source_output_key(str(video))
    mapped.mkdir(parents=True)
    flat = output / "film.chs.ass"
    custom = mapped / "film.eng.custom"
    flat.touch()
    custom.touch()
    index = subtitle_clean.SubtitleIndex()
    assert subtitle_clean.find_subtitles(video, ["ass"], output, index=index) == [flat]
    assert subtitle_clean.find_subtitles(video, ["custom"], output, index=index) == [custom]


def test_index_cancels_inside_large_directory(tmp_path):
    for i in range(30):
        (tmp_path / f"film-{i}.srt").touch()
    calls = 0

    def stop():
        nonlocal calls
        calls += 1
        return calls >= 10

    with pytest.raises(InterruptedError):
        subtitle_clean.find_subtitles(tmp_path / "film.mp4", index=subtitle_clean.SubtitleIndex(stop))
    assert calls == 10


def test_worker_checks_subtitles_in_background_and_batches(qapp, qtbot, tmp_path, monkeypatch):
    from workers.scanner import FileScannerWorker

    for i in range(300):
        (tmp_path / f"film-{i}.mp4").touch()
        if i % 2:
            (tmp_path / f"film-{i}.chs.srt").touch()
    main_thread = threading.get_ident()
    checked_threads = []
    original = subtitle_clean.find_subtitles

    def find(*args, **kwargs):
        checked_threads.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(subtitle_clean, "find_subtitles", find)
    worker = FileScannerWorker([str(tmp_path)], {".mp4"}, subtitle_formats=["srt"])
    batches, results = [], []
    worker.missing_batch.connect(batches.append)
    worker.finished.connect(lambda files, _: results.extend(files))
    with qtbot.waitSignal(worker.finished, timeout=10000):
        worker.start()
    worker.wait(5000)
    assert len(results) == 150
    assert {p for batch in batches for p in batch} == set(results)
    assert len(batches) < 10
    assert checked_threads and main_thread not in checked_threads


def test_cancel_flushes_partial_results_and_not_incomplete_checkpoint(qapp, tmp_path, monkeypatch):
    from workers.scanner import FileScannerWorker

    for i in range(10):
        (tmp_path / f"film-{i}.mp4").touch()
    worker = FileScannerWorker([str(tmp_path)], {".mp4"}, subtitle_formats=["srt"])
    calls = 0

    def find(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 4:
            worker.stop()
            raise InterruptedError()
        return []

    monkeypatch.setattr(subtitle_clean, "find_subtitles", find)
    batches, finished = [], []
    worker.missing_batch.connect(batches.append)
    worker.finished.connect(lambda paths, checked: finished.append((paths, checked)))
    worker.run()
    assert worker.cancelled
    assert len(batches) == 1 and len(batches[0]) == 3
    assert len(finished[0][0]) == len(finished[0][1]) == 3


def test_stop_while_paused_finishes_without_waiting_for_resume(qapp, qtbot, tmp_path):
    from workers.scanner import FileScannerWorker

    (tmp_path / "film.mp4").touch()
    worker = FileScannerWorker([str(tmp_path)], {".mp4"}, subtitle_formats=["srt"])
    worker.pause()
    worker.start()
    qtbot.waitUntil(worker.isRunning)
    with qtbot.waitSignal(worker.finished, timeout=2000):
        worker.stop()
    assert worker.wait(2000)
    assert worker.cancelled


def test_bulk_list_add_notifies_once_and_preserves_delete(qapp):
    from ui.widgets import DragDropListWidget

    widget = DragDropListWidget()
    signals = []
    widget.paths_changed.connect(signals.append)
    widget.add_items([(str(i), str(i)) for i in range(1000)])
    assert len(signals) == 1
    widget.add_items([("duplicate", "1"), ("new", "1000")])
    assert widget.count() == 1001
    widget.takeItem(1)
    assert "1" not in signals[-1]
    widget.add_items([("back", "1")])
    assert widget.count() == 1001


def test_page_scan_skips_synchronous_old_result_refresh(qapp, qtbot, tmp_path, monkeypatch):
    from config import ToolkitConfig
    from ui.pages.subtitle import SubtitlePage
    from ui.services import busy

    video = tmp_path / "film.mp4"
    video.touch()
    page = SubtitlePage(ToolkitConfig())
    page.spin_size.setValue(0)
    page._on_dirs_dropped([str(tmp_path)])
    monkeypatch.setattr(page, "_refresh_missing", lambda: pytest.fail("GUI must not rescan before starting worker"))
    page._start_scan()
    qtbot.waitUntil(lambda: not busy(page), timeout=10000)
    assert page.result_list.get_all_paths() == [str(video)]
    video.with_suffix(".srt").touch()
    page._start_scan()
    qtbot.waitUntil(lambda: not busy(page), timeout=10000)
    assert page.result_list.count() == 0
    video.with_suffix(".srt").unlink()
    page._start_scan()
    qtbot.waitUntil(lambda: not busy(page), timeout=10000)
    assert page.result_list.get_all_paths() == [str(video)]
