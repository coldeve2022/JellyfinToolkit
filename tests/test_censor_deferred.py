import pytest
from censor.file_signals import _classify_words


@pytest.mark.parametrize(
    "tag",
    [
        "無碼",
        "無修正",
        "無碼－破解",
        "發行：無碼流出",
        "ＵＮＣＥＮＳＯＲＥＤ",
        "Un-Censored",
        "DECENSORED",
        "去馬賽克",
        "AI去碼",
        "无 马赛克",
        "去 马赛克",
        "無_馬賽克",
    ],
)
def test_tag_variants_have_uncensored_signal(tag):
    assert _classify_words([tag])[:2] == (True, False)


@pytest.mark.parametrize(
    "tag",
    [
        "有码／无码",
        "有碼+無碼",
        "非无码",
        "not uncensored",
        "未破解",
        "尚未去碼",
        "非流出",
        "not leaked",
    ],
)
def test_conflicts_and_negations_never_allow_default_deferral(tag):
    assert _classify_words([tag])[1] is True


def test_default_worker_defers_and_manual_selection_releases_only_selected(tmp_path):
    from censor.service import CensorService, CensorOptions
    from ui.pages.censorship import CensorScanWorker

    plain, deferred, other = (
        tmp_path / "plain.mp4",
        tmp_path / "deferred.mp4",
        tmp_path / "other.mp4",
    )
    for p in (plain, deferred, other):
        p.write_bytes(b"synthetic")
    deferred.with_suffix(".nfo").write_text("<movie><tag>無碼破解</tag></movie>", encoding="utf-8")
    other.with_suffix(".nfo").write_text(
        "<movie><tag>ＵＮＣＥＮＳＯＲＥＤ</tag></movie>", encoding="utf-8"
    )
    service = CensorService({"censor_visual_enabled": False}, data_dir=tmp_path / "state")
    worker = CensorScanWorker(service, [str(tmp_path)], CensorOptions(detect_visual=False))
    results = []
    worker.item_done.connect(results.append)
    worker.run()
    assert [r.path for r in results] == [str(plain)]
    assert worker.jobs.pending() == []
    assert [r[0] for r in worker.jobs.deferred()] == [str(deferred), str(other)]
    assert service.store.get(str(deferred)) is None
    manual = CensorScanWorker(
        service, [str(deferred)], CensorOptions(detect_visual=False), manual_deferred=True
    )
    manual.run()
    assert [r[0] for r in manual.jobs.deferred()] == [str(other)]
    assert service.store.get(str(deferred)) is not None
    assert service.store.get(str(other)) is None


def test_manual_choice_is_durable_before_worker_starts(tmp_path):
    from censor.jobs import ScanJobs

    jobs = ScanJobs(tmp_path / "state.db")
    jobs.enqueue(["selected", "other"])
    jobs.defer("selected")
    jobs.defer("other")
    jobs.enqueue_sources(["selected"], manual=True)
    reopened = ScanJobs(jobs.db_path)
    assert reopened.pending() == ["selected"]
    assert reopened.authorized("selected")
    assert not reopened.authorized("other")
    assert [row[0] for row in reopened.deferred()] == ["other"]


def test_deferred_dialog_defaults_unchecked_and_only_checks_selection(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog, QDialogButtonBox, QTableWidget
    from config import ToolkitConfig
    from ui.pages.censorship import CensorshipPage

    page = CensorshipPage(ToolkitConfig())
    paths = [str(tmp_path / "one.mp4"), str(tmp_path / "two.mp4")]
    page._jobs.enqueue(paths)
    for path in paths:
        page._jobs.defer(path)
    calls = []
    monkeypatch.setattr(page, "_start_scan", lambda paths, **kwargs: calls.append((paths, kwargs)))

    def choose(dialog):
        table = dialog.findChild(QTableWidget)
        buttons = dialog.findChild(QDialogButtonBox)
        assert all(table.item(i, 0).checkState() == Qt.Unchecked for i in range(2))
        assert not buttons.button(QDialogButtonBox.Ok).isEnabled()
        table.item(0, 0).setCheckState(Qt.Checked)
        assert buttons.button(QDialogButtonBox.Ok).isEnabled()
        return QDialog.Accepted

    monkeypatch.setattr(QDialog, "exec", choose)
    page._choose_deferred()
    assert calls == [([paths[0]], {"manual_deferred": True})]
    page.deleteLater()
