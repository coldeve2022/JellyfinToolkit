"""One cancellable discovery contract for local media operations."""

from __future__ import annotations

import os
import re
from pathlib import Path

from utils.media_types import is_video

_WORK_PRODUCT = re.compile(r"(?:\.partial|\.inter|\.norm\d*|\.concat)(?:\.|$)", re.I)


def _is_archived_original(path):
    return any(part.casefold() == "_merged_originals" for part in Path(path).parts)


def iter_media_files(paths, video_exts=None, exclude_keywords=None, should_stop=None, log=None):
    """Yield unique existing main media files; directory failures are reported.

    Exclusions apply to both root inputs and discovered files. A cancellation is
    checked before walking another directory and before yielding another item.
    """
    from utils.library import is_junk_attachment_path

    stopped = should_stop or (lambda: False)
    note = log or (lambda _message: None)
    exclude = [str(k).lower() for k in (exclude_keywords or []) if str(k).strip()]
    seen = set()

    def wanted(path):
        text = str(path)
        if _is_archived_original(path):
            return False
        if any(k in text.lower() for k in exclude):
            return False
        name = Path(text).name
        if (
            _WORK_PRODUCT.search(name)
            or name.startswith(("temp_repair_", "temp_transcode_"))
            or is_junk_attachment_path(text)
            or not is_video(text, video_exts)
        ):
            return False
        key = os.path.normcase(os.path.realpath(text))
        if key in seen:
            return False
        seen.add(key)
        return True

    for raw in paths or []:
        if stopped():
            return
        root = Path(str(raw)).expanduser()
        if _is_archived_original(root):
            note(f"跳过应用合并备份目录中的原始分集：{root}")
            continue
        try:
            if root.is_file():
                if wanted(root):
                    yield str(root)
                continue
            if not root.is_dir():
                note(f"无法访问媒体路径：{root}")
                continue
            if any(k in str(root).lower() for k in exclude):
                continue

            def on_error(error):
                note(f"扫描目录失败：{getattr(error, 'filename', root)}：{error}")

            for directory, subdirs, names in os.walk(root, onerror=on_error):
                if stopped():
                    return
                subdirs[:] = sorted(
                    d
                    for d in subdirs
                    if not any(k in str(Path(directory) / d).lower() for k in exclude)
                    and d.lower()
                    not in {
                        "_tmp",
                        "trickplay",
                        "trailers",
                        "extras",
                        "quarantine",
                        "media-backups",
                        "_merged_originals",
                    }
                    and not d.lower().startswith((".toolkit-merge-", ".toolkit-stage-"))
                )
                for name in sorted(names):
                    if stopped():
                        return
                    path = Path(directory) / name
                    if wanted(path):
                        yield str(path)
        except OSError as error:
            note(f"扫描路径失败：{root}：{error}")
