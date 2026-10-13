"""System contracts missing from the original audit baseline."""
from pathlib import Path
import time
import sys

import pytest

from config import ToolkitConfig, DEFAULT_CONFIG, _coerce_config


@pytest.fixture(autouse=True)
def isolated_config_file(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config, 'CONFIG_FILE', tmp_path / 'config.json')


@pytest.mark.parametrize('value', [True, False, None])
def test_three_state_option_survives_save_load(value):
    cfg = ToolkitConfig(whisper_merge_segments=value)
    cfg.save()
    assert ToolkitConfig.load().whisper_merge_segments is value


def test_user_and_remote_path_context_survives_save_load():
    mappings = [{'remote': '/media/movies', 'local': 'C:/Media/Movies'}]
    cfg = ToolkitConfig(jellyfin_user_id='viewer-id', library_path_mappings=mappings)
    cfg.save()
    loaded = ToolkitConfig.load()
    assert loaded.jellyfin_user_id == 'viewer-id'
    assert loaded.library_path_mappings == mappings


def test_invalid_numeric_config_cannot_enable_unbounded_or_inverted_operations():
    fixed = _coerce_config({'score_th_keep': -1, 'score_th_delete': 200,
                           'ffmpeg_timeout': -9, 'min_video_size_mb': -4,
                           'transcode_crf': 90, 'lada_parallel_workers': -2,
                           'lada_vram_high': 8.0, 'lada_vram_low': 10.0})
    assert 0 <= fixed['score_th_delete'] < fixed['score_th_keep'] <= 100
    assert fixed['ffmpeg_timeout'] > 0
    assert fixed['min_video_size_mb'] >= 0
    assert 0 <= fixed['transcode_crf'] <= 51
    assert fixed['lada_parallel_workers'] >= 1
    assert 0 <= fixed['lada_vram_low'] < fixed['lada_vram_high']


def test_default_media_extensions_are_one_contract():
    from utils.media_types import DEFAULT_VIDEO_EXTENSIONS, DEFAULT_SUBTITLE_EXTENSIONS
    assert set(DEFAULT_CONFIG['video_extensions']) == set(DEFAULT_VIDEO_EXTENSIONS)
    assert set(DEFAULT_CONFIG['subtitle_extensions']) == set(DEFAULT_SUBTITLE_EXTENSIONS)


def test_scan_has_one_scope_and_filters_work_products(tmp_path):
    from utils.file_scan import iter_media_files
    wanted = tmp_path / 'TEST-101.webm'
    wanted.write_bytes(b'media')
    for name in ('TEST-101-trailer.webm', 'TEST-101 合并.norm1.webm', 'TEST-101.partial.webm'):
        (tmp_path / name).write_bytes(b'work')
    excluded = tmp_path / 'excluded'
    excluded.mkdir()
    (excluded / 'TEST-102.webm').write_bytes(b'media')
    result = list(iter_media_files([tmp_path, wanted], ['webm'], ['excluded']))
    assert result == [str(wanted)]


def test_scan_cancel_returns_no_new_paths(tmp_path):
    from utils.file_scan import iter_media_files
    (tmp_path / 'TEST-101.mp4').write_bytes(b'media')
    assert list(iter_media_files([tmp_path], should_stop=lambda: True)) == []


def test_silent_child_timeout_preserves_caller():
    from utils.proc import run_streaming
    started = time.monotonic()
    result = run_streaming([sys.executable, '-c', 'import time; time.sleep(4)'], timeout=0.3)
    assert result['timed_out']
    assert result['code'] is not None
    assert time.monotonic() - started < 3


def test_posix_process_is_started_in_its_own_session(monkeypatch):
    from utils import proc
    captured = {}
    class Child:
        returncode = 0
        pid = 123
        stdout = iter(())
        def poll(self): return 0
    def popen(*args, **kwargs):
        captured.update(kwargs)
        return Child()
    monkeypatch.setattr(proc.subprocess, 'Popen', popen)
    monkeypatch.setattr(proc.os, 'name', 'posix')
    proc.run_streaming(['synthetic-tool'])
    assert captured.get('start_new_session') is True


def test_doctor_reads_persisted_tool_choice(monkeypatch):
    import config
    from utils import tools
    calls = []
    monkeypatch.setattr(ToolkitConfig, 'load', classmethod(lambda cls: ToolkitConfig(merge_ffmpeg_path='fake/ffmpeg')))
    monkeypatch.setattr(tools, 'configure_tools', lambda ffmpeg='', **kw: calls.append(ffmpeg))
    monkeypatch.setattr(tools, 'resolve_tool', lambda *a: None)
    config.doctor()
    assert calls == ['fake/ffmpeg']
