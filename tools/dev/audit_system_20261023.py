"""Isolated, offline reproductions for the 2026-10-23 system audit.

Run from the repository root with the project's Python. Only synthetic files
under .local/audit-20261023 are created; no library or server is accessed.
These observations describe existing defects, not a replacement test suite.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
if sys.platform == "win32":
    os.environ["QT_QPA_FONTDIR"] = r"C:\Windows\Fonts"
os.environ["JELLYFIN_TOOLKIT_OFFLINE"] = "1"
WORK = ROOT / ".local" / "audit-20261023"
WORK.mkdir(parents=True, exist_ok=True)
os.environ["TEMP"] = os.environ["TMP"] = str(WORK)

import config
config.set_data_root(WORK / "runtime", rebind_modules=True)
from config import ToolkitConfig
from censor import CensorService, CensorOptions, CensorState
from censor.identity import identify_file
from censor.models import VideoVerdict
from censor.releases.base import ReleaseSource
from censor.releases.models import Availability, ReleaseCandidate, ReleaseReport, MagnetLink
from censor.releases.service import ReleaseService, ReleaseOptions
from censor.releases.store import ReleaseStore
from censor.releases.transport import HttpTransport, TransportConfig, TransportError
from censor.releases.javdb import JavDbSource
from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import QApplication
from ui.pages.censor_replace import CensorReplacePage, NumberRow
from ui.censor_release import ReleasePanel


OBSERVATIONS = []


def record(key, description, **values):
    OBSERVATIONS.append(dict(id=key, description=description, **values))


class OneRequest(ReleaseSource):
    name = "fake"

    def search(self, query, **kwargs):
        self.transport.get("https://synthetic.invalid/" + query.number)
        return [ReleaseCandidate(source=self.name, number=query.number)]


class FailingSource(ReleaseSource):
    name = "fake"

    def __init__(self):
        super().__init__(object())
        self.calls = 0

    def search(self, query, **kwargs):
        self.calls += 1
        raise TransportError("synthetic timeout")


def service(tag, source, cfg=None):
    return ReleaseService(cfg or {"release_sources": ["fake"]},
                          store=ReleaseStore(WORK / (tag + ".db")),
                          factory=lambda _: source)


def confirmed_movie(tag):
    path = WORK / (tag + "-123.mp4")
    path.write_bytes(b"synthetic video version one")
    svc = CensorService(ToolkitConfig(), data_dir=WORK / tag)
    stat = path.stat()
    item = VideoVerdict(path=str(path), number=tag + "-123",
                        size=stat.st_size, mtime=stat.st_mtime,
                        file_identity=identify_file(path).serialize())
    svc.store.put(item)
    svc.label(str(path), CensorState.CENSORED, base=item)
    return path, svc


def core_probes():
    # The fake request function is the only HTTP execution path here.
    transport = HttpTransport(TransportConfig(max_requests=2, retries=0, min_interval=0))
    transport._once = lambda *_: b"synthetic"
    svc = service("budget", OneRequest(transport))
    with patch("censor.releases.transport.offline", return_value=False):
        reports = [svc.lookup(f"DEMO-{i}", options=ReleaseOptions(force=True)) for i in range(3)]
    record("R01", "Request budget persists across independent lookups",
           sources=[r.sources for r in reports], requests=transport._requests)

    transport = HttpTransport(TransportConfig(max_requests=2, retries=0, min_interval=0))
    transport._once = lambda *_: b"synthetic"
    svc = service("cancel", OneRequest(transport))
    good = ReleaseReport(number="DEMO123", availability=Availability.CONFIRMED,
                         queried_at=time.time())
    svc.store.put(good, svc.signature())
    svc.queue(["DEMO123"])
    cancelled = svc.lookup("DEMO123", options=ReleaseOptions(force=True), should_stop=lambda: True)
    record("R02", "Cancellation replaces successful cache and completes pending queue",
           result=cancelled.availability.value, note=cancelled.note,
           sources=cancelled.sources, cached=svc.cached("DEMO123").availability.value,
           queue_stats=svc.store.queue_stats())

    failing = FailingSource()
    svc = service("failure", failing)
    svc.store.delete("DEMO123")
    first, second = svc.lookup("DEMO123"), svc.lookup("DEMO123")
    record("R03", "Transient failure is reused as a fresh report",
           attempts=failing.calls, first=first.note, second_elapsed=second.elapsed)

    path, svc = confirmed_movie("STALE")
    path.write_bytes(b"synthetic NEW and different video file")
    current = identify_file(path).serialize()
    record("R04", "Replacement list includes confirmation for an older file version",
           current_label=svc.store.get_label(str(path), file_identity=current),
           confirmed_count=len(svc.store.confirmed_censored()))

    transport = HttpTransport(TransportConfig(retries=0, min_interval=0))
    transport._once = lambda *_: b"synthetic"
    svc = service("disabled", OneRequest(transport),
                  {"release_sources": ["fake"], "release_enabled": False})
    with patch("censor.releases.transport.offline", return_value=False):
        svc.lookup("DISABLED123", options=ReleaseOptions(force=True))
    record("R05", "Disabled release feature still executes transport", requests=transport._requests)

    candidate = JavDbSource()._parse_detail(
        "<strong>番號:</strong>DEMO-123</div>",
        {"number": "DEMO-123", "title": "DEMO-123 Uncensored", "href": "/v/synthetic"},
        __import__("censor.releases.models", fromlist=["ReleaseQuery"]).ReleaseQuery("DEMO-123"))
    from censor.releases.service import decide_availability
    record("R06", "Title-only evidence receives hard confirmation in JavDB parser",
           confidence=candidate.uncensored_confidence, reason=candidate.uncensored_reason,
           availability=decide_availability([candidate]).value)

    cfg = ToolkitConfig()
    cfg.censor_visual_enabled = False
    movie = WORK / "CACHE-123.mp4"
    movie.write_bytes(b"synthetic video")
    svc = CensorService(cfg, data_dir=WORK / "cache-signature")
    opts = CensorOptions(detect_visual=False)
    result = svc.probe(movie, opts)
    before = svc.cached_result(movie, opts) is not None
    cfg.censor_workers += 1
    after = svc.cached_result(movie, opts) is not None
    record("R07", "Changing worker count invalidates completed analysis",
           status=result.status.value, before=before, after=after)

    from censor.releases.javbus import JavBusSource
    from censor.releases.models import ReleaseQuery
    class FixtureTransport:
        def get(self, *args, **kwargs):
            return (ROOT / "tests/fixtures/javbus_detail_piyo170.html").read_text(encoding="utf-8")
    source = JavBusSource(FixtureTransport())
    source._magnets = lambda *args: []
    returned = source.search(ReleaseQuery("DEMO-999"))
    record("R15", "JavBus accepts a different identifier from the returned page",
           requested="DEMO-999", returned=[r.number for r in returned])

    from censor.releases.supjav import SupjavSource
    record("R16", "Supjav identifier fallback has no left boundary",
           title="XNCRA-026", wanted="NCRA026",
           matched=SupjavSource._number_matches("XNCRA-026", "XNCRA-026", "NCRA026"))

    store = ReleaseStore(WORK / "corrupt.db")
    store.put(ReleaseReport(number="CORRUPT123"))
    with store._connect() as conn:
        conn.execute("UPDATE release_report SET report='[]' WHERE number='CORRUPT123'")
    try:
        value = store.get("CORRUPT123")
        error = None
    except Exception as exc:
        value = None
        error = type(exc).__name__ + ": " + str(exc)
    record("R17", "Structurally invalid JSON escapes cache loader", value=value, error=error)

    record("R18", "Title negation is ignored by release keyword classifiers",
           javdb=JavDbSource._judge_uncensored("DEMO-123", "", (), "Not Uncensored"),
           supjav=SupjavSource._judge("Not Uncensored DEMO-123"))

    def broken_factory(_):
        raise RuntimeError("synthetic plugin initialization failure")
    svc = ReleaseService({"release_sources": ["broken"]}, data_dir=WORK / "broken-plugin",
                          factory=broken_factory)
    report = svc.lookup("DEMO123", options=ReleaseOptions(force=True))
    record("R19", "Source construction errors are reported as nonexistent identifiers",
           sources=report.sources, note=report.note)

    svc = CensorService(ToolkitConfig(), data_dir=WORK / "tool-cache")
    with patch("utils.tools.resolve_tool", side_effect=lambda name: "old/" + name):
        original_tools = svc.tools()
    with patch("utils.tools.resolve_tool", side_effect=lambda name: "new/" + name):
        tools_after_configuration_change = svc.tools()
    record("R20", "Service keeps previously resolved tools after resolver configuration changes",
           original_tools=original_tools, after_change=tools_after_configuration_change)


def ui_probes(app):
    widgets = []
    cfg = ToolkitConfig()
    page = CensorReplacePage(cfg)
    widgets.append(page)
    record("R08", "Replacement page has no event or show-time refresh hook",
           has_show_event_override="showEvent" in CensorReplacePage.__dict__)

    expired = ReleaseReport(number="OLD123", availability=Availability.NONE,
                            queried_at=time.time() - 8 * 86400)
    page._rows = {"OLD123": NumberRow("OLD123", ["synthetic.mp4"], expired)}
    calls = []

    def would_construct_worker(*args, **kwargs):
        calls.append("worker constructed")
        raise AssertionError("Unexpected worker construction in stale-cache probe")

    with patch("ui.pages.censor_replace.ReleaseLookupWorker", would_construct_worker):
        page._lookup(force=False)
    record("R09", "Lookup-new button skips expired reports",
           targets_started=calls, summary=page.lbl_summary.text())

    from itertools import product
    mismatches = []
    checks = 0
    for state_a, state_b, new_state, order in product(
            Availability, Availability, Availability, (Qt.AscendingOrder, Qt.DescendingOrder)):
        a = NumberRow("A123", ["a.mp4"], ReleaseReport(
            number="A123", availability=state_a, candidates=(ReleaseCandidate("fake"),)))
        b = NumberRow("B123", ["b.mp4"], ReleaseReport(
            number="B123", availability=state_b, candidates=(ReleaseCandidate("fake"),) * 2))
        page._rows = {"A123": a, "B123": b}
        page._fill_table()
        page.table.sortItems(2, order)
        page._on_report(ReleaseReport(number="B123", availability=new_state,
                                      candidates=tuple(ReleaseCandidate("fake") for _ in range(7))))
        actual = {page.table.item(r, 0).text(): page.table.item(r, 3).text()
                  for r in range(page.table.rowCount())}
        checks += 1
        if actual != {"A123": "1", "B123": "7"}:
            mismatches.append(dict(states=[state_a.value, state_b.value, new_state.value],
                                   order=order.name, actual=actual))
    record("R10", "Sorting while replacing row cells", checks=checks,
           mismatches=len(mismatches), examples=mismatches[:3])

    path, svc = confirmed_movie("VISIBLE")
    page2 = CensorReplacePage(cfg)
    widgets.append(page2)
    page2._censor = svc
    page2.refresh()
    page2.show()
    app.processEvents()
    before = page2.table.rowCount()
    svc.label(path, CensorState.UNCENSORED)
    page2.hide()
    page2.show()
    app.processEvents()
    record("R11", "Reopening replacement page does not update changed manual labels",
           before=before, after=page2.table.rowCount(),
           current_store_count=len(svc.store.confirmed_censored()))

    panel = ReleasePanel()
    widgets.append(panel)
    for i in range(10):
        panel.show_report(ReleaseReport(number=str(i), candidates=(ReleaseCandidate("fake", number=str(i)),)))
    record("R12", "Candidate index retains previously displayed reports",
           visible_rows=panel.table.rowCount(), retained_candidates=len(panel._candidates_index))

    report = ReleaseReport(number="ONLYCENSORED123", availability=Availability.NONE,
                           candidates=(ReleaseCandidate("fake", number="ONLYCENSORED123",
                               magnets=(MagnetLink("magnet:?xt=urn:btih:synthetic", size_mb=2048),)),))
    panel.show_report(report)
    record("R13", "Copy-magnet action offers a censored candidate as fallback",
           availability=report.availability.value, button_enabled=panel.btn_magnet.isEnabled(),
           fallback_uncensored=panel._fallback().uncensored)

    page.resize(900, 650)
    page.show()
    app.processEvents()
    record("R14", "Replacement layout minimum size at desktop content width",
           requested_width=900, actual_width=page.width(), minimum_hint=page.minimumSizeHint().width())
    page.grab().save(str(WORK / "replacement-page-900.png"))
    for widget in widgets:
        widget.hide()
        widget.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


def inventory():
    files = [p for directory in ("censor", "workers", "utils", "ui", "automation")
             for p in (ROOT / directory).rglob("*.py")]
    files += [ROOT / name for name in ("main.py", "config.py", "auto_pipeline.py", "auto_orchestrator.py")]
    modules = []
    for path in sorted(files):
        source = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        modules.append(dict(path=str(path.relative_to(ROOT)).replace("\\", "/"),
                            lines=len(source.splitlines()),
                            classes=sum(isinstance(n, ast.ClassDef) for n in ast.walk(tree)),
                            functions=sum(isinstance(n, ast.FunctionDef) for n in ast.walk(tree))))
    return modules


def performance_probes():
    """Small synthetic baseline; timings do not predict long-film throughput."""
    import platform
    import shutil
    import statistics
    import subprocess
    import numpy as np
    from censor.evaluation import build_cases, evaluate_cases
    from censor.mosaic_cv import MosaicParams, analyze_frame
    from censor.sampling import FrameSampler, SampledFrame
    params = MosaicParams.from_config(ToolkitConfig())
    report = evaluate_cases(build_cases(), params)
    rng = np.random.default_rng(20261023)
    image = rng.integers(0, 256, size=(720, 1280), dtype=np.uint8)
    frame = SampledFrame(0, 1280, 720, image.tobytes(), 1)
    timings = []
    for _ in range(3):
        start = time.perf_counter()
        analyze_frame(frame, params)
        timings.append(time.perf_counter() - start)
    report["performance"] = dict(
        python=platform.python_version(),
        cpu_count=os.cpu_count(),
        noise_1280x720_runs=timings,
        noise_1280x720_median_seconds=statistics.median(timings),
        default_raw_frame_buffer_bytes=60 * 1280 * 720,
        note="Synthetic single-frame timings; not real-library throughput or accuracy.",
    )
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        video = WORK / "synthetic-performance.mp4"
        subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                        "testsrc2=size=640x360:rate=12:duration=8", "-c:v", "libx264",
                        "-threads", "1", str(video)], capture_output=True, check=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        sampler = FrameSampler(ffmpeg, ffprobe, width=1280, height=720, pixel_format="gray")
        original = subprocess.Popen
        commands = []
        def count_process(*args, **kwargs):
            commands.append(args[0])
            return original(*args, **kwargs)
        start = time.perf_counter()
        with patch("censor.sampling.subprocess.Popen", count_process):
            frames = sampler.sample(video, count=24)
        report["performance"]["sampling"] = dict(
            synthetic_duration_seconds=8, samples=len(frames),
            elapsed_seconds=time.perf_counter() - start,
            subprocess_count=len(commands),
            sampling_threads_explicit=any("-threads" in cmd for cmd in commands),
        )
    (WORK / "performance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    print(json.dumps(dict(confusion=report["confusion"], samples=report["sample_count"],
                          performance=report["performance"]), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    app = QApplication.instance() or QApplication([])
    from utils.fonts import ui_font
    font = ui_font(10)
    if font is not None:
        app.setFont(font)
    core_probes()
    ui_probes(app)
    result = dict(date="2026-10-23", offline=True, modules=inventory(), observations=OBSERVATIONS)
    output = WORK / "observations.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(OBSERVATIONS, ensure_ascii=False, indent=2))
    print("Evidence:", output)
    if "--performance" in sys.argv:
        performance_probes()
