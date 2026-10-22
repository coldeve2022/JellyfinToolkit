from pathlib import Path
from censor.models import VideoVerdict, CensorState, Verdict


def test_filename_hint_overrides_original_nfo_for_scheduling(tmp_path):
    from censor.jobs import scan_priority
    path = tmp_path / 'movie-無碼破解.mp4'
    path.write_bytes(b'synthetic')
    path.with_suffix('.nfo').write_text('<movie><tag>有码</tag></movie>', encoding='utf-8')
    assert scan_priority(str(path))[0] == 2
    assert '文件名' in scan_priority(str(path))[1]


def test_manual_label_advances_to_next_pending_row(qapp, tmp_path):
    from config import ToolkitConfig
    from ui.pages.censorship import CensorshipPage
    page = CensorshipPage(ToolkitConfig())
    paths = [tmp_path / 'a.mp4', tmp_path / 'b.mp4']
    for path in paths:
        path.write_bytes(b'synthetic')
        page._on_item(VideoVerdict(path=str(path), verdict=Verdict(needs_review=True)))
    page.table.selectRow(next(r for r in range(page.table.rowCount())
                              if page.table.item(r, 0).text() == 'a.mp4'))
    page._on_row_selected()
    page._label(CensorState.UNCENSORED)
    assert Path(page._current.path).name == 'b.mp4'
    assert page._results[str(paths[0])].verdict.needs_review is False
    page.deleteLater()


def test_gallery_displays_every_candidate(qapp, tmp_path):
    from config import ToolkitConfig
    from ui.pages.censorship import CensorshipPage
    from censor.models import FrameCandidate
    page = CensorshipPage(ToolkitConfig())
    result = VideoVerdict(path=str(tmp_path / 'sample.mp4'),
                          candidates=[FrameCandidate(i * 10, 20) for i in range(12)])
    page._show_thumbs(result)
    assert len(page._thumb_labels) == 12
    page.deleteLater()


def test_cracked_filename_cannot_hide_actual_visual_detection():
    from censor.models import Evidence, EvidenceSource
    from censor.fusion import fuse
    from censor.review_route import category
    result = VideoVerdict(path='movie-無碼破解.mp4', visual_score=40,
                          verdict=fuse([Evidence(EvidenceSource.VISUAL, CensorState.CENSORED, .85)]))
    assert category(result) == 'review'


def test_new_result_refreshes_current_evidence(qapp, tmp_path):
    from config import ToolkitConfig
    from ui.pages.censorship import CensorshipPage
    from censor.models import FrameCandidate
    page = CensorshipPage(ToolkitConfig())
    path = str(tmp_path / 'generic.mp4')
    page._on_item(VideoVerdict(path=path))
    page.table.selectRow(0)
    page._on_row_selected()
    updated = VideoVerdict(path=path, candidates=[FrameCandidate(10, 20)], sampled_frames=180)
    page._on_item(updated)
    assert page._current is updated
    assert len(page._thumb_labels) == 1
    assert '180' in page.txt_evidence.toPlainText()
    page.deleteLater()

def test_historical_filename_deferred_is_manually_selectable_without_queue(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog, QTableWidget
    from config import ToolkitConfig
    from ui.pages.censorship import CensorshipPage
    page = CensorshipPage(ToolkitConfig())
    path = str(tmp_path / 'generic-無碼破解.mp4')
    page._on_item(VideoVerdict(path=path, verdict=Verdict(needs_review=True)))
    calls = []
    monkeypatch.setattr(page, '_start_scan', lambda paths, **kwargs: calls.append(paths))
    def choose(dialog):
        table = dialog.findChild(QTableWidget)
        assert table.rowCount() == 1
        assert table.item(0, 0).text() == path
        assert table.item(0, 0).checkState() == Qt.Unchecked
        table.item(0, 0).setCheckState(Qt.Checked)
        return QDialog.Accepted
    monkeypatch.setattr(QDialog, 'exec', choose)
    page._choose_deferred()
    assert calls == [[path]]
    page.deleteLater()


def test_weak_nonzero_score_remains_low_risk_when_visual_evidence_allows_it():
    from censor.models import Evidence, EvidenceSource
    from censor.fusion import fuse
    from censor.review_route import category
    result = VideoVerdict(path='generic.mp4', visual_score=2,
        verdict=fuse([Evidence(EvidenceSource.VISUAL,CensorState.UNKNOWN,0.,review_required=False)]))
    assert category(result) == 'low_risk'
