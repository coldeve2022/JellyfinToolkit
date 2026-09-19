"""utils.merge 分集合并纯函数单元测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import utils.merge as m  # noqa: E402


# ── 番号提取与归一化 ──

def test_extract_number_ext():
    assert m.extract_number_ext("MIAA-743 新人.mp4") == "MIAA-743"
    assert m.extract_number_ext("ABCDE12345.mp4") == "ABCDE-12345"
    assert m.extract_number_ext("FC2-3581881-cd4.mp4") == "FC2-3581881"
    assert m.extract_number_ext("fc2_1234567.mp4") == "FC2-1234567"
    assert m.extract_number_ext("FC2-PPV-3581881-cd1.mp4") == "FC2-3581881"
    assert m.extract_number_ext("MOVIE.mp4") is None


def test_normalize_number():
    assert m.normalize_number("FC2-3581881") == "FC23581881"
    assert m.normalize_number("MIAA-743") == "MIAA743"
    assert m.normalize_number("") == ""


# ── 版本后缀剥离 ──

def test_strip_variant_tokens():
    cleaned, found = m.strip_variant_tokens("MIAA-743-破解-cd1")
    assert "破解" in found and "cd1" in cleaned
    _, found2 = m.strip_variant_tokens("MIAA-743-1080p-C")
    assert {"1080p", "C"} <= set(found2)
    _, found3 = m.strip_variant_tokens("MIAA-743-cd1")
    assert found3 == []


# ── 分集识别 ──

def test_parse_part_index_cd():
    assert m.parse_part_index("MIAA-743-cd1", "MIAA-743") == (1, "cd")
    assert m.parse_part_index("MIAA-743-cd2", "MIAA-743") == (2, "cd")
    assert m.parse_part_index("MIAA-743-CD08", "MIAA-743") == (8, "cd")
    assert m.parse_part_index("MIAA-743-disc1", "MIAA-743") == (1, "cd")
    assert m.parse_part_index("MIAA-743-part2", "MIAA-743") == (2, "cd")
    assert m.parse_part_index("FC2-PPV-3581881-cd4", "FC2-3581881") == (4, "cd")


def test_parse_part_index_letter_and_numeric():
    assert m.parse_part_index("MIAA-743-A", "MIAA-743") == (1, "letter")
    assert m.parse_part_index("MIAA-743B", "MIAA-743") == (2, "letter")
    assert m.parse_part_index("MIAA-743-1", "MIAA-743") == (1, "numeric")
    assert m.parse_part_index("MIAA-743_2", "MIAA-743") == (2, "numeric")
    assert m.parse_part_index("MIAA-743.3", "MIAA-743") == (3, "numeric")
    assert m.parse_part_index("MIAA-743 (2)", "MIAA-743") == (2, "numeric")


def test_parse_part_index_cn_and_base():
    assert m.parse_part_index("MIAA-743第1集", "MIAA-743") == (1, "cn")
    assert m.parse_part_index("MIAA-743上集", "MIAA-743") == (1, "cn")
    assert m.parse_part_index("MIAA-743下集", "MIAA-743") == (2, "cn")
    assert m.parse_part_index("MIAA-743", "MIAA-743") == (0, "base")
    # 版本标记不误判为分集
    assert m.parse_part_index("MIAA-743-C", "MIAA-743") == (0, "base")
    assert m.parse_part_index("MIAA-743-1080p", "MIAA-743") == (0, "base")


# ── 分组 ──

def test_group_by_number(tmp_path):
    a = tmp_path / "MIAA-743-cd1.mp4"; a.write_text("x", encoding="utf-8")
    b = tmp_path / "MIAA743-cd2.mp4"; b.write_text("y", encoding="utf-8")
    c = tmp_path / "FC2-PPV-3581881-cd1.mp4"; c.write_text("z", encoding="utf-8")
    d = tmp_path / "NO_NUMBER.mp4"; d.write_text("w", encoding="utf-8")
    groups, unrecognized = m.group_by_number([str(a), str(b), str(c), str(d)])
    assert m.normalize_number("MIAA-743") in groups
    assert m.normalize_number("FC2-3581881") in groups
    assert len(groups[m.normalize_number("MIAA-743")]) == 2
    assert unrecognized == [str(d)]


# ── 附属视频过滤 ──


def test_is_auxiliary_media():
    assert m.is_auxiliary_media("D:/x/theme.mp4")
    assert m.is_auxiliary_media("D:/x/trailer.mp4")
    assert m.is_auxiliary_media("D:/x/预告片.mp4")
    assert m.is_auxiliary_media("D:/x/MIAA-743-主题曲.mp4")
    assert m.is_auxiliary_media("D:/x/MIAA-743-trailer.mp4")
    assert m.is_auxiliary_media("D:/trailers/MIAA-743.mp4")
    assert m.is_auxiliary_media("D:/x/MIAA-743-sample.mp4")
    assert m.is_auxiliary_media("D:/x/Behind the Scenes.mp4")
    assert not m.is_auxiliary_media("D:/x/MIAA-743.mp4")
    assert not m.is_auxiliary_media("D:/x/MIAA-743-cd1.mp4")
    assert not m.is_auxiliary_media("D:/x/MIAA-743-disc1.mp4")


def test_scan_files_excludes_auxiliary(tmp_path):
    (tmp_path / "MIAA-743.mp4").write_bytes(b"a")
    (tmp_path / "MIAA-743-cd1.mp4").write_bytes(b"b")
    (tmp_path / "MIAA-743-cd2.mp4").write_bytes(b"c")
    (tmp_path / "theme.mp4").write_bytes(b"d")
    (tmp_path / "MIAA-743-trailer.mp4").write_bytes(b"e")
    (tmp_path / "MIAA-743-sample.mp4").write_bytes(b"f")
    files = m.scan_files([str(tmp_path)])
    names = sorted(Path(f).name for f in files)
    assert names == ["MIAA-743-cd1.mp4", "MIAA-743-cd2.mp4", "MIAA-743.mp4"]


# ── 参数一致性 ──

def _probe(**kw):
    base = dict(ok=True, duration=100.0, width=1280, height=720, fps=30.0,
                video_codec="h264", audio_codec="aac", size=1, container="mp4")
    base.update(kw)
    return m.PartProbe(**base)


def _pf(name, probe):
    return m.PartFile(path=f"D:/x/{name}", name=name, stem=Path(name).stem,
                      number="MIAA-743", part_index=1, part_kind="cd", probe=probe)


def test_check_merge_compat():
    ok, issues = m.check_merge_compat([
        _pf("a.mp4", _probe()),
        _pf("b.mp4", _probe()),
    ])
    assert ok and not issues
    ok2, issues2 = m.check_merge_compat([
        _pf("a.mp4", _probe()),
        _pf("b.mp4", _probe(video_codec="hevc")),
    ])
    assert not ok2 and any("编码" in i for i in issues2)
    ok3, _ = m.check_merge_compat([
        _pf("a.mp4", _probe()),
        _pf("b.mp4", _probe(width=1920, height=1080)),
    ])
    assert not ok3


# ── 去重 ──

def test_pick_sample_timestamps():
    ts = m.pick_sample_timestamps(100.0, 9)
    assert len(ts) == 9
    assert 4.9 <= ts[0] <= 5.1
    assert max(ts) < 100.0


def test_hamming_similarity():
    a = [0b0000000000000000000000000000000000000000000000000000000000000000]
    b = [0b0000000000000000000000000000000000000000000000000000000000000101]
    assert m.hamming_similarity(a, b, max_distance=2) == 1.0
    assert m.hamming_similarity(a, b, max_distance=1) == 0.0
    assert m.hamming_similarity([], [1]) == 0.0


def test_should_fingerprint_only_for_plausible_duplicates():
    cd1 = m.PartFile("D:/x/a-cd1.mp4", "a-cd1.mp4", "a-cd1", "ABC-123", 1, "cd")
    cd2 = m.PartFile("D:/x/a-cd2.mp4", "a-cd2.mp4", "a-cd2", "ABC-123", 2, "cd")
    base = m.PartFile("D:/x/a.mp4", "a.mp4", "a", "ABC-123", 0, "base")
    numeric = m.PartFile("D:/x/a (2).mp4", "a (2).mp4", "a (2)", "ABC-123", 2, "numeric")
    assert not m._should_fingerprint(cd1, cd2)
    assert m._should_fingerprint(base, numeric)
    assert m._should_fingerprint(base, base)


def test_is_vfr():
    assert not m._is_vfr(_probe(fps=30.0, r_fps=30.0))
    assert m._is_vfr(_probe(fps=30.0, r_fps=29.0))


def test_check_merge_compat_flags_sar_color_vfr():
    a = _probe(fps=30.0, r_fps=30.0, sample_aspect_ratio="1:1", color_range="tv")
    b = _probe(fps=30.0, r_fps=30.0, sample_aspect_ratio="3:2", color_range="tv")
    ok, issues = m.check_merge_compat([_pf("a.mp4", a), _pf("b.mp4", b)])
    assert not ok and any("宽高比" in i for i in issues)

    a2 = _probe(fps=30.0, r_fps=30.0, color_range="tv")
    b2 = _probe(fps=30.0, r_fps=30.0, color_range="pc")
    ok2, issues2 = m.check_merge_compat([_pf("a.mp4", a2), _pf("b.mp4", b2)])
    assert not ok2 and any("色彩范围" in i for i in issues2)

    a3 = _probe(fps=30.0, r_fps=30.0)
    b3 = _probe(fps=30.0, r_fps=29.0)
    ok3, issues3 = m.check_merge_compat([_pf("a.mp4", a3), _pf("b.mp4", b3)])
    assert not ok3 and any("可变帧率" in i for i in issues3)


def test_validate_merged_output_catches_bad_duration(monkeypatch):
    parts = [_pf("a.mp4", _probe(duration=100.0)),
             _pf("b.mp4", _probe(duration=100.0))]
    monkeypatch.setattr(m, "probe_part",
                        lambda ffprobe, path, timeout=120: _probe(duration=230.0))
    ok, detail = m.validate_merged_output("ffprobe", "D:/x/out.mp4", parts)
    assert not ok
    assert "时长" in detail


def test_detect_duplicates_exact(tmp_path):
    f1 = tmp_path / "MIAA-743-cd1.mp4"; f1.write_bytes(b"same-content-123")
    f2 = tmp_path / "MIAA-743-cd2.mp4"; f2.write_bytes(b"same-content-123")
    f3 = tmp_path / "MIAA-743-cd3.mp4"; f3.write_bytes(b"different-content!!")
    parts = [
        m.PartFile(str(f1), f1.name, f1.stem, "MIAA-743", 1, "cd", probe=_probe(size=f1.stat().st_size)),
        m.PartFile(str(f2), f2.name, f2.stem, "MIAA-743", 2, "cd", probe=_probe(size=f2.stat().st_size)),
        m.PartFile(str(f3), f3.name, f3.stem, "MIAA-743", 3, "cd", probe=_probe(size=f3.stat().st_size)),
    ]
    kept, dups = m.detect_duplicates(parts, ffmpeg="", options={})
    assert len(kept) == 2
    assert len(dups) == 1
    assert dups[0] == str(f2)


def test_detect_duplicates_caches_frame_and_hash(monkeypatch):
    frame_calls = []
    hash_calls = []

    def fake_frames(ffmpeg, path, timestamps):
        frame_calls.append(path)
        values = {"a": 0, "b": (1 << 64) - 1, "c": 0x5555555555555555}
        return [values[Path(path).stem]] * len(timestamps)

    def fake_hash(path):
        hash_calls.append(path)
        return f"h-{Path(path).stem}"

    monkeypatch.setattr(m, "extract_frame_hashes", fake_frames)
    monkeypatch.setattr(m, "quick_file_hash", fake_hash)
    parts = [
        m.PartFile("D:/x/a.mp4", "a.mp4", "a", "MIAA-743", 1, "cd",
                   probe=_probe(size=123)),
        m.PartFile("D:/x/b.mp4", "b.mp4", "b", "MIAA-743", 1, "cd",
                   probe=_probe(size=123)),
        m.PartFile("D:/x/c.mp4", "c.mp4", "c", "MIAA-743", 1, "cd",
                   probe=_probe(size=123)),
    ]
    kept, dups = m.detect_duplicates(parts, ffmpeg="ffmpeg", options={})
    assert len(kept) == 3
    assert dups == []
    assert len(frame_calls) == 3
    assert len(hash_calls) == 3


def test_detect_duplicates_near_duration_uses_shared_timestamps(monkeypatch):
    calls = []

    def fake_frames(ffmpeg, path, timestamps):
        calls.append((path, tuple(round(t, 2) for t in timestamps)))
        return [42] * len(timestamps)

    monkeypatch.setattr(m, "extract_frame_hashes", fake_frames)
    monkeypatch.setattr(m, "quick_file_hash",
                        lambda path: f"h-{Path(path).stem}")
    parts = [
        m.PartFile("D:/x/a.mp4", "a.mp4", "a", "MIAA-743", 0, "base",
                   probe=_probe(size=100, duration=100.0)),
        m.PartFile("D:/x/b.mp4", "b.mp4", "b", "MIAA-743", 0, "base",
                   probe=_probe(size=101, duration=101.0)),
    ]
    kept, dups = m.detect_duplicates(parts, ffmpeg="ffmpeg", options={})
    assert len(kept) == 1
    assert dups == ["D:/x/b.mp4"]
    assert calls[0][1] == calls[1][1]


# ── concat 列表与输出 ──

def test_build_concat_list_escape(tmp_path):
    list_file = tmp_path / "list.txt"
    parts = [
        m.PartFile("D:/a/MIAA-743-cd1.mp4", "MIAA-743-cd1.mp4", "MIAA-743-cd1", "MIAA-743", 1, "cd"),
        m.PartFile("D:/o'brien/MIAA-743-cd2.mp4", "MIAA-743-cd2.mp4", "MIAA-743-cd2", "MIAA-743", 2, "cd"),
    ]
    m.build_concat_list(parts, str(list_file))
    text = list_file.read_text(encoding="utf-8")
    assert "file 'D:/a/MIAA-743-cd1.mp4'" in text
    assert "o'\\''brien" in text


def test_unique_output_path(tmp_path):
    a = tmp_path / "x.mp4"
    a.write_text("1", encoding="utf-8")
    b = m.unique_output_path(a)
    assert b.name == "x (2).mp4"
    b.write_text("2", encoding="utf-8")
    c = m.unique_output_path(a)
    assert c.name == "x (3).mp4"


def test_choose_output_container():
    parts = [_pf("a.mp4", _probe()), _pf("b.mp4", _probe())]
    assert m.choose_output_container(parts, "mp4") == "mp4"
    parts_wmv = [_pf("a.mp4", _probe(video_codec="wmv3")),
                 _pf("b.mp4", _probe(video_codec="wmv3"))]
    assert m.choose_output_container(parts_wmv, "mp4") == "mkv"
    assert m.choose_output_container(parts, "mkv") == "mkv"
    assert m.choose_output_container(parts, "webm") == "mkv"


# ── archive ──

def test_archive_originals(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    a = src_dir / "MIAA-743-cd1.mp4"; a.write_text("1", encoding="utf-8")
    b = src_dir / "MIAA-743-cd2.mp4"; b.write_text("2", encoding="utf-8")
    backup = tmp_path / "_merged_originals" / "MIAA-743"
    moved, failed = m.archive_originals([str(a), str(b)], str(backup))
    assert len(moved) == 2 and not failed
    assert not a.exists() and not b.exists()
    assert (backup / "MIAA-743-cd1.mp4").exists()


def test_discover_pending_merged_outputs(tmp_path):
    cd1 = tmp_path / "FC2-2760723-cd1.mp4"; cd1.write_bytes(b"1")
    cd2 = tmp_path / "FC2-2760723-cd2.mp4"; cd2.write_bytes(b"2")
    merged = tmp_path / "FC2-2760723 合并.mp4"; merged.write_bytes(b"merged")
    base = tmp_path / "FC2-2760723.mp4"; base.write_bytes(b"base")
    parts = [
        m.PartFile(path=str(cd1), name=cd1.name, stem=cd1.stem,
                   number="FC2-2760723", part_index=1, part_kind="cd"),
        m.PartFile(path=str(cd2), name=cd2.name, stem=cd2.stem,
                   number="FC2-2760723", part_index=2, part_kind="cd"),
    ]
    group = m.EpisodeGroup(number="FC22760723", parts=parts)
    found = m.discover_pending_merged_outputs([group])
    assert set(found) == {"FC22760723"}
    info = found["FC22760723"]
    assert info["output"] == str(merged)
    assert set(info["originals"]) == {str(cd1), str(cd2)}


def test_legacy_merged_rename_and_nfo_sync(tmp_path):
    merged = tmp_path / "FC2-2760723 合并.mp4"; merged.write_bytes(b"v")
    nfo = tmp_path / "FC2-2760723 合并.nfo"
    nfo.write_text("<movie><title>FC2-2760723 合并</title></movie>", encoding="utf-8")
    (tmp_path / "FC22760723合并.mkv").write_bytes(b"x")
    (tmp_path / "ABC-123 合併.mp4").write_bytes(b"y")
    (tmp_path / "ABC-124 合并.partial.mp4").write_bytes(b"z")

    items = m.discover_legacy_merged_files([str(tmp_path)])
    by_target = {item["target"]: item for item in items}
    assert str(tmp_path / "FC2-2760723.mp4") in by_target
    assert str(tmp_path / "FC2-2760723.mkv") in by_target
    assert str(tmp_path / "ABC-123.mp4") in by_target
    assert str(tmp_path / "ABC-124.mp4") not in by_target

    ok, detail, final = m.finalize_legacy_merged_output(str(merged))
    assert ok, detail
    assert final == str(tmp_path / "FC2-2760723.mp4")
    assert not merged.exists()
    assert (tmp_path / "FC2-2760723.mp4").exists()
    assert (tmp_path / "FC2-2760723.nfo").exists()
    assert "FC2-2760723 合并" not in (tmp_path / "FC2-2760723.nfo").read_text(encoding="utf-8")


# ── 合并计划（monkeypatch ffprobe） ──

def _fake_probe(ffprobe, path):
    return _probe(size=Path(path).stat().st_size)


@pytest.fixture
def fake_probe(monkeypatch):
    monkeypatch.setattr(m, "probe_part", _fake_probe)


def _mk(tmp_path, names):
    return [str(tmp_path / n) for n in names]


def _write_distinct(paths):
    for i, p in enumerate(paths):
        Path(p).write_bytes((str(i) * 64).encode("utf-8"))


def test_plan_cd_ready(tmp_path, fake_probe):
    paths = _mk(tmp_path, ["MIAA-743-cd1.mp4", "MIAA-743-cd2.mp4"])
    _write_distinct(paths)
    plan = m.build_merge_plan(paths, ffmpeg="", ffprobe="x", options={})
    g = plan.groups[0]
    assert g.status == "ready"
    assert len(g.selected_paths) == 2


def test_plan_cd_gap_needs_review(tmp_path, fake_probe):
    paths = _mk(tmp_path, ["MIAA-743-cd1.mp4", "MIAA-743-cd2.mp4", "MIAA-743-cd8.mp4"])
    _write_distinct(paths)
    plan = m.build_merge_plan(paths, ffmpeg="", ffprobe="x", options={})
    g = plan.groups[0]
    assert g.status == "needs_review"
    assert any("不连续" in i for i in g.issues)


def test_plan_base_plus_cd1(tmp_path, fake_probe):
    paths = _mk(tmp_path, ["MIAA-743.mp4", "MIAA-743-cd1.mp4", "MIAA-743-cd2.mp4"])
    _write_distinct(paths)
    plan = m.build_merge_plan(paths, ffmpeg="", ffprobe="x", options={})
    g = plan.groups[0]
    assert g.status == "needs_review"
    assert any("base" in i for i in g.issues)


def test_plan_variant_skip(tmp_path, fake_probe):
    paths = _mk(tmp_path, ["MIAA-743.mp4", "MIAA-743-破解.mp4"])
    _write_distinct(paths)
    plan = m.build_merge_plan(paths, ffmpeg="", ffprobe="x", options={})
    g = plan.groups[0]
    assert g.status == "skip_variant"


def test_plan_duplicate_skip(tmp_path, fake_probe):
    paths = _mk(tmp_path, ["MIAA-743-cd1.mp4", "MIAA-743-cd2.mp4"])
    for p in paths:
        Path(p).write_bytes(b"same-exact-bytes")
    plan = m.build_merge_plan(paths, ffmpeg="", ffprobe="x", options={})
    g = plan.groups[0]
    assert g.status == "skip_duplicate"
    assert len(g.duplicate_paths) == 1


def test_plan_probe_failed(tmp_path, monkeypatch):
    def bad_probe(ffprobe, path):
        return m.PartProbe(ok=False, error="x")
    monkeypatch.setattr(m, "probe_part", bad_probe)
    paths = _mk(tmp_path, ["MIAA-743-cd1.mp4", "MIAA-743-cd2.mp4"])
    for p in paths:
        Path(p).write_bytes(b"e")
    plan = m.build_merge_plan(paths, ffmpeg="", ffprobe="x", options={})
    assert plan.groups[0].status == "needs_review"


def test_validate_merge_selection_rejects_duplicate_part_index():
    p1 = m.PartFile(path="D:/x/a.mp4", name="a.mp4", stem="a",
                    number="MIAA-743", part_index=1, part_kind="cd",
                    probe=_probe())
    p2 = m.PartFile(path="D:/x/b.mp4", name="b.mp4", stem="b",
                    number="MIAA-743", part_index=1, part_kind="cd",
                    probe=_probe())
    ok, reason = m.validate_merge_selection([p1, p2])
    assert not ok
    assert "相同分集序号" in reason


def test_validate_merge_selection_rejects_missing_video():
    p1 = m.PartFile(path="D:/x/a.mp4", name="a.mp4", stem="a",
                    number="MIAA-743", part_index=1, part_kind="cd",
                    probe=_probe(video_codec=""))
    p2 = m.PartFile(path="D:/x/b.mp4", name="b.mp4", stem="b",
                    number="MIAA-743", part_index=2, part_kind="cd",
                    probe=_probe())
    ok, reason = m.validate_merge_selection([p1, p2])
    assert not ok
    assert "缺少可读视频流" in reason


def test_validate_merge_selection_accepts_clean_parts():
    p1 = m.PartFile(path="D:/x/a.mp4", name="a.mp4", stem="a",
                    number="MIAA-743", part_index=1, part_kind="cd",
                    probe=_probe())
    p2 = m.PartFile(path="D:/x/b.mp4", name="b.mp4", stem="b",
                    number="MIAA-743", part_index=2, part_kind="cd",
                    probe=_probe())
    ok, reason = m.validate_merge_selection([p1, p2])
    assert ok, reason


def test_validate_merge_selection_reencode_skips_param_check():
    p1 = m.PartFile(path="D:/x/a.mp4", name="a.mp4", stem="a",
                    number="MIAA-743", part_index=1, part_kind="cd",
                    probe=_probe(fps=29.97, sample_rate=44100))
    p2 = m.PartFile(path="D:/x/b.mp4", name="b.mp4", stem="b",
                    number="MIAA-743", part_index=2, part_kind="cd",
                    probe=_probe(fps=59.94, sample_rate=48000))
    ok, reason = m.validate_merge_selection([p1, p2])
    assert not ok
    ok2, reason2 = m.validate_merge_selection([p1, p2], allow_reencode=True)
    assert ok2, reason2
