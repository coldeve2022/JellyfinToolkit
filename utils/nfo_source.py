"""NFO 数据源 - 当 jellyfin.db 标签缺失时，从视频旁的刮削 .nfo 读取原始标签作为兜底。

纯函数层：read_nfo_features(path) -> (tags, genres, studios)。
- 仅用于"DB 无标签"的补全，防止刮削同步问题导致信息缺失。
- 标准库 ElementTree，零依赖；容错解析失败。
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from typing import Optional

from utils.nfo import read_text_any_encoding


def find_nfo(video_path: str) -> Optional[str]:
    """优先同目录同名 .nfo（xx.mp4 -> xx.nfo），再退到 movie.nfo / tvshow.nfo。"""
    if not video_path:
        return None
    nfo = os.path.splitext(video_path)[0] + ".nfo"
    if os.path.exists(nfo):
        return nfo
    d = os.path.dirname(video_path)
    for fallback in ("movie.nfo", "tvshow.nfo"):
        candidate = os.path.join(d, fallback)
        if os.path.exists(candidate):
            return candidate
    return None


def read_nfo_features(video_path: str):
    """读取 .nfo 的 <tag>/<genre>/<studio>，返回 (tags, genres, studios) 去重列表。"""
    nfo = find_nfo(video_path)
    if not nfo:
        return [], [], []
    try:
        with open(nfo, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read()
        root = ET.fromstring(txt)
    except Exception:  # noqa: BLE001 - 解析失败不致命，返回空
        return [], [], []

    def _collect(tag: str) -> list:
        out = []
        for el in root.findall(f".//{tag}"):
            v = (el.text or "").strip()
            if v:
                out.append(v)
        return out

    # tag 与 genre 合并为标签兜底（刮削常见结构），去重保序
    seen, tags = set(), []
    for v in _collect("tag") + _collect("genre"):
        if v not in seen:
            seen.add(v)
            tags.append(v)
    studios = _collect("studio")
    return tags, [], studios


def read_nfo_title(video_path: str) -> Optional[str]:
    """读取 .nfo 的 <title>/<originaltitle>，返回可读标题或 None。"""
    nfo = find_nfo(video_path)
    if not nfo:
        return None
    txt = read_text_any_encoding(nfo)
    if not txt:
        return None
    try:
        root = ET.fromstring(txt)
    except Exception:  # noqa: BLE001
        return None
    for tag in ("title", "originaltitle"):
        el = root.find(f".//{tag}")
        v = (el.text or "").strip() if el is not None else ""
        if v:
            return v
    return None
