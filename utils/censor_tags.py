"""破解替换后的“有码 → 无码破解”标签同步。

只处理状态类标签（有码/无码/薄码 等），其它内容标签原样保留：
- 命中状态标签时改为「无码破解」；
- 没有任何状态标签时，直接补一个「无码破解」；
- 去重，避免出现“无码”和“无码破解”并存。
"""
from __future__ import annotations

import sqlite3
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

TARGET_TAG = "无码破解"

# 统一成 TARGET_TAG 的状态标签
CENSOR_STATE_TAGS = {
    "有码", "有碼", "有码破解", "无码", "無碼", "無修正",
    "薄码", "薄碼", "薄马赛克", "有马", "有馬",
}


def normalize_state_tag(value) -> str:
    """把状态类标签规范成「无码破解」，其它标签原样返回。"""
    t = str(value or "").strip()
    return TARGET_TAG if t in CENSOR_STATE_TAGS else t


def split_tag_list(raw) -> list[str]:
    """解析 Jellyfin BaseItems.Genres/Tags 的 | 分隔值。"""
    if raw is None:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for part in str(raw).split("|"):
        t = part.strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def join_tag_list(values) -> str:
    return "|".join(values)


def replace_state_tags(values) -> list[str]:
    """对标签列表执行状态标签替换与去重，并确保存在「无码破解」。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        t = normalize_state_tag(raw)
        if not t:
            continue
        if t not in seen:
            seen.add(t)
            out.append(t)
    if TARGET_TAG not in seen:
        out.insert(0, TARGET_TAG)
    return out


def update_jellyfin_db_tags(
    db_path: str | Path,
    video_path: str | Path,
    *,
    backup: bool = True,
    log: Optional[Callable[[str], None]] = None,
) -> tuple[bool, str]:
    """更新 Jellyfin BaseItems 中指定视频的 Genres/Tags 状态标签。

    返回 (是否成功, 说明)。
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    db = Path(db_path)
    if not db.exists():
        return False, f"数据库不存在: {db}"

    backup_path: Optional[Path] = None
    if backup:
        backup_path = db.with_name(
            f"{db.stem}.replace_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}{db.suffix}"
        )
        try:
            shutil.copy2(str(db), str(backup_path))
            _log(f"📦 已备份 Jellyfin 数据库: {backup_path.name}")
        except OSError as e:
            return False, f"数据库备份失败: {e}"

    try:
        conn = sqlite3.connect(str(db), timeout=15)
        conn.execute("PRAGMA busy_timeout=15000")
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT Id, Genres, Tags FROM BaseItems "
                "WHERE Path = ? COLLATE NOCASE LIMIT 1",
                (str(video_path),),
            )
            row = cur.fetchone()
            if not row:
                return False, f"数据库中未找到视频: {Path(video_path).name}"

            item_id, genres, tags = row
            genres_list = split_tag_list(genres)
            tags_list = split_tag_list(tags)
            new_genres = replace_state_tags(genres_list)
            # Tags 字段为空时保持为空，避免和 Genres 重复写入同一个状态标签。
            new_tags = replace_state_tags(tags_list) if tags_list else []
            cur.execute(
                "UPDATE BaseItems SET Genres = ?, Tags = ? WHERE Id = ?",
                (join_tag_list(new_genres), join_tag_list(new_tags), item_id),
            )
            conn.commit()
            return True, f"已更新 Jellyfin 标签: {Path(video_path).name}"
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except sqlite3.Error as e:
        return False, f"Jellyfin 数据库更新失败: {e}"
