"""合并替换的安全归档与精确回滚。

与 Qt 解耦的纯函数层：
- 把原分集及其同名 sidecar（nfo/字幕/图片）备份到可配置的大容量盘；
- 合并产物改名为 `番号.<容器>`，并同步一份可用的 NFO；
- 记录 JSON 日志，支持按单个文件恢复。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import DATA_DIR
from utils.merge import unique_output_path
from utils.nfo_source import find_nfo


def journal_file() -> Path:
    """归档日志路径。

    不写成模块级常量：``from config import DATA_DIR`` 是**导入期绑定**，
    一旦运行时重定向数据目录（测试 / 便携模式切换），模块级常量会指向旧路径。
    """
    return DATA_DIR / "merge_archive_journal.json"


# 与视频同名、需要跟着视频一起备份的附加文件
SIDECAR_EXTENSIONS = {
    ".nfo", ".srt", ".ass", ".ssa", ".sub", ".vtt",
    ".jpg", ".jpeg", ".png", ".webp",
}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".vtt"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ── 日志 ─────────────────────────────
def load_journal() -> list[dict]:
    path = journal_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def save_journal(entries: list[dict]) -> None:
    path = journal_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))


def add_journal_entries(entries: list[dict]) -> None:
    if not entries:
        return
    all_entries = load_journal()
    all_entries.extend(entries)
    save_journal(all_entries)


def mark_entry_rolled_back(entry_id: str) -> None:
    all_entries = load_journal()
    for e in all_entries:
        if e.get("id") == entry_id:
            e["rolled_back"] = True
            break
    save_journal(all_entries)


def rollbackable_entries(entries: list[dict] | None = None) -> list[dict]:
    all_entries = entries if entries is not None else load_journal()
    return [
        e for e in all_entries
        if not e.get("rolled_back")
        and e.get("kind") in {"media", "sidecar", "nfo_backup"}
        and e.get("backup_path")
        and Path(e["backup_path"]).exists()
    ]


# ── 备份路径 ─────────────────────────
def fallback_backup_root() -> Path:
    """未配置备份盘时的落点：数据目录下的 backups/（一定可写，不依赖任何盘符）。"""
    return Path(DATA_DIR) / "backups"


def _root_usable(path: Path) -> bool:
    """目标根目录是否可用（父目录存在且可写）。"""
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return probe.is_dir() and os.access(str(probe), os.W_OK)


def resolve_backup_root(cfg) -> Path:
    """解析「替换原分集」的备份根目录。

    历史实现的默认值是写死的固定盘符 —— 在没有该盘的机器上 mkdir 直接失败，
    整个「确认替换」功能报废。现在：留空/盘符不存在/不可写 → 一律回退到
    数据目录下的 backups/，保证任何机器上都能用。
    """
    raw = str(getattr(cfg, "merge_backup_root", "") or "").strip()
    if not raw:
        return fallback_backup_root()
    if re.fullmatch(r"[A-Za-z]:", raw):
        raw += "/"
    elif re.fullmatch(r"[A-Za-z]", raw):
        raw += ":/"
    p = Path(raw)
    if not p.is_absolute():
        # 相对路径按「数据目录下」解释，不再挂到某个不存在的盘符上
        p = fallback_backup_root() / p
    return p if _root_usable(p) else fallback_backup_root()


def group_backup_dir(cfg, number: str) -> Path:
    return resolve_backup_root(cfg) / cfg.merge_original_subfolder / number


def move_file_to_backup(src: str, dst_dir: Path) -> tuple[bool, str, str]:
    """移动单个文件到备份目录，返回 (ok, error, backup_path)。"""
    sp = Path(src)
    if not sp.exists():
        return False, f"文件不存在: {src}", ""
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, str(e), ""
    dst = unique_output_path(dst_dir / sp.name)
    try:
        shutil.move(str(sp), str(dst))
        return True, "", str(dst)
    except OSError as e:
        return False, str(e), ""


def iter_sidecars(video_path: str):
    """找到与视频同目录的 sidecar 文件。

    包括：
    - 同主文件名（`video.nfo` / `video.srt` / `video.jpg`）；
    - 带语言/来源后缀的字幕（`video.zh.srt` / `video.chi.ass` / `video.default.srt`）。
    """
    sp = Path(video_path)
    stem = sp.stem
    try:
        for f in sp.parent.iterdir():
            if not f.is_file() or f == sp:
                continue
            suffix = f.suffix.lower()
            if suffix in SIDECAR_EXTENSIONS and f.stem == stem:
                yield str(f)
            elif suffix in SUBTITLE_EXTENSIONS and f.stem.startswith(stem + "."):
                yield str(f)
    except OSError:
        return


def choose_source_nfo(original_paths: list[str], number: str) -> Optional[str]:
    """选择合并后应保留的 NFO：优先 `番号.nfo`，其次第一个分集的 NFO。"""
    if not original_paths:
        return None
    first_dir = Path(original_paths[0]).parent
    preferred = first_dir / f"{number}.nfo"
    if preferred.exists():
        return str(preferred)
    for p in original_paths:
        nfo = find_nfo(p)
        if nfo and Path(nfo).name.lower() not in ("movie.nfo", "tvshow.nfo"):
            return nfo
    return find_nfo(original_paths[0])


def _make_entry(number: str, kind: str, original_path: str,
                backup_path: str, final_path: str = "") -> dict:
    return {
        "id": uuid.uuid4().hex,
        "number": number,
        "kind": kind,
        "original_path": original_path,
        "backup_path": backup_path,
        "final_path": final_path,
        "moved_at": _now(),
        "rolled_back": False,
    }


# ── 主流程 ─────────────────────────
def archive_group_for_merge(cfg, number: str, original_paths: list[str],
                            merged_output: str) -> tuple[bool, list[dict], list[str], Optional[str]]:
    """确认替换一个分集组。

    返回 (success, journal_entries, errors, final_output)。
    若原视频移动失败，会尝试把已移动的原视频移回，并返回 success=False。
    """
    entries: list[dict] = []
    errors: list[str] = []
    rename_to_number = bool(getattr(cfg, "merge_rename_to_number", True))
    output = Path(merged_output)
    group_dir = group_backup_dir(cfg, number)
    # 必须在移动原视频之前解析 NFO 来源；移动后原路径将不再存在，
    # 导致 choose_source_nfo 无法通过原视频路径找到对应 NFO。
    source_nfo = choose_source_nfo(original_paths, number)

    # 1) 先把原视频移到备份盘。若中途失败，回滚已经移动的原视频。
    moved_media: list[tuple[str, str]] = []
    for src in original_paths:
        ok, err, backup = move_file_to_backup(src, group_dir)
        if not ok:
            for original, bp in moved_media:
                try:
                    shutil.move(bp, original)
                except OSError:
                    pass
            return False, [], [f"{Path(src).name}: {err}"], None
        entries.append(_make_entry(number, "media", src, backup))
        moved_media.append((src, backup))

    # 原分集已经移走后再计算最终文件名，避免 number.mp4 这类 base 原文件造成误加 (2)。
    if rename_to_number:
        final = unique_output_path(output.parent / f"{number}{output.suffix}")
    else:
        final = output

    # 3) 合并文件改名为 `番号.<容器>`。
    if not output.exists():
        for original, bp in moved_media:
            try:
                shutil.move(bp, original)
            except OSError:
                pass
        return False, [], ["合并输出文件不存在"], None
    if final != output:
        try:
            shutil.move(str(output), str(final))
        except OSError as e:
            for original, bp in moved_media:
                try:
                    shutil.move(bp, original)
                except OSError:
                    pass
            return False, [], [str(e)], None
    entries.append(_make_entry(number, "merged_output", str(output), "", str(final)))

    # 4) 同步 NFO：优先 `番号.nfo`，否则把第一个分集的 NFO 内容复制到最终 NFO。
    final_nfo = final.with_suffix(".nfo")
    if source_nfo:
        source_path = Path(source_nfo)
        if source_path != final_nfo:
            if final_nfo.exists():
                ok, err, backup = move_file_to_backup(str(final_nfo), group_dir)
                if ok:
                    entries.append(_make_entry(number, "nfo_backup", str(final_nfo), backup))
                else:
                    errors.append(f"原目标 NFO 备份失败: {err}")
            if source_path.exists() and not final_nfo.exists():
                try:
                    shutil.copy2(source_path, final_nfo)
                    entries.append(_make_entry(number, "nfo_final", str(source_nfo), "", str(final_nfo)))
                except OSError as e:
                    errors.append(f"复制最终 NFO 失败: {e}")

    protected_nfo = {final_nfo.resolve()}

    # 5) 移动同名 sidecar（在 NFO 复制之后，避免把 NFO 源文件先移走）。
    for src in original_paths:
        for side in iter_sidecars(src):
            if Path(side).resolve() in protected_nfo:
                continue
            ok, err, backup = move_file_to_backup(side, group_dir)
            if ok:
                entries.append(_make_entry(number, "sidecar", side, backup))
            else:
                errors.append(f"{Path(side).name}: {err}")

    return True, entries, errors, str(final)


def restore_entry(entry: dict) -> tuple[bool, str]:
    """把单个备份文件恢复到原始路径。返回 (ok, detail)。"""
    backup = Path(entry.get("backup_path") or "")
    original = Path(entry.get("original_path") or "")
    if not backup.exists():
        return False, "备份文件不存在"
    try:
        original.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, str(e)
    target = unique_output_path(original)
    try:
        shutil.move(str(backup), str(target))
        return True, str(target)
    except OSError as e:
        return False, str(e)
