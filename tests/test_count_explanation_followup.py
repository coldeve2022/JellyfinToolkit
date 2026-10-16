"""User-visible count units and release dependency regressions."""

from pathlib import Path
import re

from packaging.requirements import Requirement
from PySide6.QtWidgets import QLabel


def test_all_installation_paths_exclude_known_unsupported_qt_release():
    root = Path(__file__).resolve().parents[1]
    runtime = next(
        line
        for line in (root / "requirements.txt").read_text().splitlines()
        if line.startswith("PySide6")
    )
    project = re.search(
        r'"(PySide6[^\"]+)"', (root / "pyproject.toml").read_text(encoding="utf-8")
    ).group(1)
    for declaration in (runtime, project):
        spec = Requirement(declaration).specifier
        assert "6.11.2" in spec
        assert "6.12.0" not in spec


def test_overview_explains_entries_files_and_work_groups(qapp, monkeypatch):
    from config import ToolkitConfig
    from utils import library_source
    from utils.library import summarize
    from ui.pages.insight import InsightPage

    monkeypatch.setattr(library_source, "describe", lambda cfg: "synthetic library")
    page = InsightPage(ToolkitConfig())
    data = summarize([])
    data.update(entry_count=4, file_count=3, work_count=2, filtered_out=5)
    page._render_summary(data)
    text = "\n".join(label.text() for label in page.findChildren(QLabel))
    assert "主视频条目" in text
    assert "唯一视频路径 3" in text
    assert "作品分组 2" in text
    assert "已排除附属等条目 5" in text


def test_profile_distinguishes_unique_evidence_from_library_entries(qapp, monkeypatch):
    from config import ToolkitConfig
    from utils import library_source
    from utils.profile import build_profile
    from ui.pages.profile import ProfilePage

    monkeypatch.setattr(library_source, "describe", lambda cfg: "synthetic library")
    page = ProfilePage(ToolkitConfig())
    page.profile = build_profile([], {})
    page.profile.update(library_entries=4, physical_videos=3, works=2, n_pos=1, total=3)
    page._render_profile()
    text = "\n".join(label.text() for label in page.findChildren(QLabel))
    assert "喜好正样本" in text
    assert "库条目 4" in text
    assert "唯一视频路径 3" in text
