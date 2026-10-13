"""NFO 文件相关的纯函数 — 内容修正、关联文件名还原。"""

import os
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

# .ts 后缀匹配（作为单词边界出现）
_TS_PATTERN = re.compile(r"\.ts\b", re.IGNORECASE)


def read_text_any_encoding(path: str) -> Optional[str]:
    """按 UTF-8 → GBK 顺序尝试读取文本文件。

    Args:
        path: 文件路径

    Returns:
        读取到的文本；两种编码都失败返回 None
    """
    for enc in ("utf-8", "gbk"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
        except OSError:
            return None
    return None


def update_nfo_censor_tags(path: str) -> tuple[bool, str]:
    """把 NFO 中的有码/无码状态标签统一为「无码破解」，缺失时自动补一个。"""
    from utils.censor_tags import TARGET_TAG, normalize_state_tag

    text = read_text_any_encoding(path)
    if text is None:
        return False, "无法读取 NFO 文件"
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        return False, f"NFO XML 解析失败: {e}"

    changed = False
    for tag in ("tag", "genre"):
        for el in root.findall(f".//{tag}"):
            old = (el.text or "").strip()
            new = normalize_state_tag(old)
            if new != old:
                el.text = new
                changed = True

    existing = {(el.text or "").strip() for el in root.findall(".//tag") + root.findall(".//genre")}
    if TARGET_TAG not in existing:
        el = ET.SubElement(root, "tag")
        el.text = TARGET_TAG
        changed = True

    if not changed:
        return True, "NFO 已包含目标标签，无需修改"

    p = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        os.close(fd)
        body = ET.tostring(root, encoding="unicode")
        from utils.file_ops import safe_write_text

        result = safe_write_text(
            p, '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + body
        )
        if not result["ok"]:
            raise OSError(result["error"])
        tmp.unlink(missing_ok=True)
        return True, f"已更新 NFO: {p.name}"
    except Exception as e:  # noqa: BLE001
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False, f"NFO 写入失败: {e}"


def fix_nfo_content(content: str) -> Optional[str]:
    """将 NFO 内容中的 .ts 引用替换为 .mp4。

    Args:
        content: NFO 文件原始文本

    Returns:
        替换后的文本；内容中没有 .ts 引用时返回 None（无需写入）
    """
    if not _TS_PATTERN.search(content):
        return None
    return _TS_PATTERN.sub(".mp4", content)


def nfo_new_filename(filename: str) -> Optional[str]:
    """将关联文件名中的 .ts 残留替换为 .mp4（如 xxx.ts.nfo → xxx.mp4.nfo）。

    Args:
        filename: 原始文件名

    Returns:
        替换后的文件名；无 .ts 残留时返回 None
    """
    lower = filename.lower()
    if ".ts." in lower or ".ts-" in lower:
        new_name = re.sub(r"\.ts\.", ".mp4.", filename, flags=re.IGNORECASE)
        new_name = re.sub(r"\.ts\-", ".mp4-", new_name, flags=re.IGNORECASE)
        return new_name
    return None


def safe_rename(old_path: str, new_path: str) -> None:
    """Recoverable rename; existing targets are conflicts, never silently replaced."""
    from utils.file_ops import path_key, safe_replace

    if not Path(old_path).is_file():
        raise FileNotFoundError(old_path)
    if path_key(old_path) == path_key(new_path):
        return
    if Path(new_path).exists():
        raise FileExistsError(new_path)
    result = safe_replace(
        old_path, new_path, move_source=True, allow_overwrite=False, kind="rename"
    )
    if not result["ok"]:
        raise OSError(result["error"])


def preview_nfo_fixes(directories, should_stop=None):
    from utils.file_ops import file_hash

    plan = []
    seen = set()
    for directory in directories:
        for root, dirs, files in os.walk(directory):
            dirs[:] = [
                d
                for d in dirs
                if d not in {"quarantine", "media-backups", "_merged_originals"}
                and not d.startswith(".toolkit-")
            ]
            for name in files:
                if should_stop and should_stop():
                    return plan
                source = Path(root) / name
                if source.is_symlink() or str(source) in seen:
                    continue
                seen.add(str(source))
                target = source.with_name(nfo_new_filename(name) or name)
                content = (
                    read_text_any_encoding(str(source)) if source.suffix.lower() == ".nfo" else None
                )
                new_content = fix_nfo_content(content) if content is not None else None
                if target == source and new_content is None:
                    continue
                plan.append(
                    dict(
                        source=str(source),
                        target=str(target),
                        old_hash=file_hash(source),
                        new_content=new_content,
                        status="conflict" if target != source and target.exists() else "ready",
                    )
                )
    return plan


def execute_nfo_fixes(plan, should_stop=None, log=print):
    from utils.file_ops import OperationJournal, file_hash, safe_write_text

    journal = OperationJournal()
    stats = dict(fixed=0, renamed=0, fail=0, cancelled=False, fails=[], operations=[])
    for row in plan:
        if should_stop and should_stop():
            stats["cancelled"] = True
            break
        completed = []
        try:
            if row["status"] != "ready":
                raise FileExistsError("目标已存在，需要审阅冲突")
            if file_hash(row["source"]) != row["old_hash"]:
                raise OSError("文件已改变，请重新创建计划")
            current = row["source"]
            if current != row["target"]:
                from utils.file_ops import safe_replace

                result = safe_replace(
                    current,
                    row["target"],
                    journal=journal,
                    move_source=True,
                    should_stop=should_stop,
                    allow_overwrite=False,
                    kind="nfo_rename",
                    expected_source_hash=row["old_hash"],
                )
                stats["operations"].append(result)
                if not result["ok"]:
                    if result["state"] == "cancelled":
                        stats["cancelled"] = True
                    raise OSError(result["error"])
                completed.append(result)
                current = row["target"]
                stats["renamed"] += 1
            if row["new_content"] is not None:
                result = safe_write_text(
                    current,
                    row["new_content"],
                    journal=journal,
                    should_stop=should_stop,
                    expected_target_hash=row["old_hash"],
                )
                stats["operations"].append(result)
                if not result["ok"]:
                    if result["state"] == "cancelled":
                        stats["cancelled"] = True
                    raise OSError(result["error"])
                completed.append(result)
                stats["fixed"] += 1
            log(f"已修复（可恢复）：{current}")
        except (OSError, KeyError) as exc:
            for operation in reversed(completed):
                restored = journal.restore(operation["operation_id"])
                if not restored["ok"]:
                    log("恢复记录保留：" + restored["error"])
            if completed and row["source"] != row["target"]:
                stats["renamed"] -= 1
            stats["fail"] += 1
            stats["fails"].append((row.get("source", ""), str(exc)))
            log(f"修复失败：{exc}")
            if stats["cancelled"]:
                break
    return stats


def fix_nfo_directory(directory, log=print, should_stop=None):
    result = execute_nfo_fixes(preview_nfo_fixes([directory], should_stop), should_stop, log)
    return result["fixed"], result["renamed"]
