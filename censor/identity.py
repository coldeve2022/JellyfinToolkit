"""Cheap file-version identity and deterministic analysis signatures.

The sampled digest detects ordinary replacements, not arbitrary edits outside
the sampled ranges. Full-file hashing is deliberately avoided for large videos.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileIdentity:
    size: int
    mtime_ns: int
    sample_hash: str

    def serialize(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


def identify_file(path: str | Path) -> FileIdentity:
    target = Path(path)
    before = target.stat()
    chunk = 65536
    offsets = sorted({0, max(0, (before.st_size - chunk) // 2), max(0, before.st_size - chunk)})
    digest = hashlib.sha256()
    with target.open("rb") as stream:
        for offset in offsets:
            stream.seek(offset)
            digest.update(offset.to_bytes(8, "little"))
            digest.update(stream.read(chunk))
    after = target.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise OSError("文件在计算身份时发生变化")
    return FileIdentity(after.st_size, after.st_mtime_ns, digest.hexdigest())
