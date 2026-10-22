from censor.models import VideoVerdict, ProcessingStatus
from config import ToolkitConfig
from ui.pages.censorship import CensorshipPage


def test_ui_shows_processing_failure_and_real_coverage(qapp, tmp_path):
    page = CensorshipPage(ToolkitConfig())
    result = VideoVerdict(
        path=str(tmp_path / "sample.mp4"),
        status=ProcessingStatus.FAILED,
        error="decoder failed",
        sampled_frames=3,
        analyzed_frames=1,
    )
    result.verdict.needs_review = True
    page._on_item(result)
    assert "失败" in page.table.item(0, 2).text()
    page.table.selectRow(0)
    page._on_row_selected()
    assert "decoder failed" in page.txt_evidence.toPlainText()
    assert "1/3" in page.txt_evidence.toPlainText()
    assert "置信度" not in page.table.horizontalHeaderItem(3).text()
    page.deleteLater()
