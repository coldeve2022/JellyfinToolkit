"""is_junk_attachment_path 单元测试：识别 Jellyfin 附属文件(theme/backdrop/fanart/trailer)。

路径一律使用中性的虚构示例，不要写入任何真实的本地路径或人名。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.library import is_junk_attachment_path


def test_theme_video_is_junk():
    """用户报告的场景：theme_video.mp4 应识别为附属文件。"""
    assert is_junk_attachment_path(r"C:\Media\Library\ArtistA\theme_video.mp4")
    assert is_junk_attachment_path("/lib/theme_video.mp4")
    assert is_junk_attachment_path(r"D:\media\demo\theme_video.mp4")


def test_theme_backdrop_fanart_trailer_is_junk():
    for fn in ("theme.mp4", "backdrop.jpg", "fanart.jpg", "fanart2.jpg", "trailer.mp4", "poster.jpg"):
        assert is_junk_attachment_path(f"/lib/{fn}"), fn


def test_backdrops_dir_is_junk():
    assert is_junk_attachment_path(r"C:\data\backdrops\sunset.mp4")
    assert is_junk_attachment_path("/data/backdrop/backdrop.mp4")


def test_named_video_is_not_junk():
    """正常的番号视频不应被排除。"""
    assert not is_junk_attachment_path(r"C:\Media\Library\ArtistA\MIAA-743-C.mp4")
    assert not is_junk_attachment_path(r"D:\a\FC2-123456-cd1.mp4")
    assert not is_junk_attachment_path(r"C:\Media\Library\ArtistA\STARS-043.mp4")


def test_empty_args():
    assert not is_junk_attachment_path("")
    assert not is_junk_attachment_path("", "")


def test_explicit_name_overrides_path():
    """name 参数优先：显式给 theme_video 即使 path 看似正常也判为附属。"""
    assert is_junk_attachment_path("/video/ok.mp4", name="theme_video")
    assert not is_junk_attachment_path("/video/theme.mp4", name="MIAA-743")
