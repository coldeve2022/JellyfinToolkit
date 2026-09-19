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

    existing = {
        (el.text or "").strip()
        for el in root.findall(".//tag") + root.findall(".//genre")
    }
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
        with open(tmp, "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n')
            f.write(body)
        os.replace(str(tmp), str(p))
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
    """跨平台安全重命名：目标存在时先删除（Windows os.rename 无法覆盖）。

    Args:
        old_path: 源路径
        new_path: 目标路径

    Raises:
        OSError: 重命名失败
    """
    if os.path.exists(new_path) and new_path != old_path:
        try:
            os.remove(new_path)
        except OSError:
            pass
    os.replace(old_path, new_path)
