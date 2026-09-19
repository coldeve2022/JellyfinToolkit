"""字幕文件的「去重 / 长句拆分」后处理 —— 纯逻辑，不依赖 Qt 与任何外部程序。

移植自批量字幕工具里那套后处理，解决的问题是两个很常见的毛病：

1. **重复字幕**：识别引擎在「嗯嗯啊啊」或静音段上容易吐出连续多条几乎一样的
   字幕，看起来像卡住；
2. **超长字幕**：一句话被识别成跨几十秒的一条，画面早就换了字还没走。

做法是先按格式解析成 ``[{start, end, text}, ...]`` 这类结构，清理后再渲染回去。
解析/渲染支持四种格式，与上游引擎的 ``--sub_formats`` 一一对应：

======  ====================================================
格式     说明
======  ====================================================
``srt``  最常见的字幕格式（``00:00:01,000 --> 00:00:03,000``）
``vtt``  WebVTT（时间用小数点，开头有 ``WEBVTT`` 头）
``lrc``  歌词格式，只做相邻重复行去除
``txt``  纯文本，只做相邻重复行去除
======  ====================================================

所有阈值都是**参数**（``max_duration`` / ``similarity``），不写死在代码里。
"""

from __future__ import annotations

import math
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

__all__ = [
    "SUPPORTED_FORMATS", "DEFAULT_MAX_DURATION", "DEFAULT_SIMILARITY",
    "norm_text", "split_sentences", "collapse_text", "clean_blocks",
    "parse_srt", "render_srt", "parse_vtt", "render_vtt",
    "clean_lrc_text", "clean_plain_text", "clean_subtitle_text",
    "clean_file",
]

SUPPORTED_FORMATS = ("srt", "vtt", "lrc", "txt")

#: 单条字幕允许的最长时长（秒）。超过就按句子/字数切开。
DEFAULT_MAX_DURATION = 20.0

#: 相邻两条字幕文本的相似度阈值（0~1），超过视为重复。
DEFAULT_SIMILARITY = 0.88

_TIME_RE = re.compile(
    r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")
_LRC_RE = re.compile(r"\[(\d+):(\d+)(?:[.:](\d+))?\](.*)")


# ── 基础工具 ────────────────────────────────────────────────

def norm_text(text: str) -> str:
    """归一化用于「是否算重复」的比较：去掉所有非单词字符并转小写。

    注意它**只用于比较**，不会替换原文（否则会破坏字幕里的标点与日文长音符号）。
    """
    return re.sub(r"[\W_]+", "", (text or "").lower())


def split_sentences(text: str) -> list:
    """按中英文句末标点切句（保留标点）。"""
    parts = re.split(r"(?<=[。！？!?])", text or "")
    return [p.strip() for p in parts if p.strip()]


def _timestamp_to_seconds(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _seconds_to_srt_ts(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms // 1000, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{total_ms % 1000:03d}"


def _seconds_to_vtt_ts(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms // 1000, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.{total_ms % 1000:03d}"


# ── 文本压缩 ────────────────────────────────────────────────

def collapse_repeated_units(text: str) -> str:
    """压掉同一个「小片段」连续重复 3 次以上的情况（不区分语言）。"""
    if not text or len(text) < 6:
        return text
    collapsed = text
    max_unit = min(len(collapsed) // 2, 24)
    for unit_len in range(2, max_unit + 1):
        collapsed = re.sub(rf"(.{{{unit_len}}}?)\1{{2,}}", r"\1", collapsed)
    return re.sub(r"(.)\1{3,}", r"\1", collapsed)


def collapse_repeated_phrases(text: str) -> str:
    """按空白分词的语言，压掉连续重复的词组（如 "yes yes yes"）。"""
    if " " not in (text or ""):
        return text
    tokens = text.split()
    out: list = []
    for token in tokens:
        out.append(token)
        changed = True
        while changed:
            changed = False
            n = len(out)
            for length in range(1, min(8, n // 2) + 1):
                if out[-2 * length:-length] == out[-length:]:
                    del out[-length:]
                    changed = True
                    break
    return " ".join(out)


def collapse_text(text: str) -> str:
    """一条字幕文本的完整压缩：压重复单元 → 压重复词组 → 丢重复句子。"""
    text = collapse_repeated_units(text)
    text = collapse_repeated_phrases(text)
    sentences = split_sentences(text)
    if len(sentences) <= 1:
        return text.strip()
    seen = set()
    kept = []
    for sentence in sentences:
        key = norm_text(sentence)
        if key and key in seen:
            continue
        seen.add(key)
        kept.append(sentence)
    joiner = " " if " " in text else ""
    return joiner.join(kept)


# ── 块级清理 ────────────────────────────────────────────────

def _chunk_text_block(block: dict, max_duration: float) -> list:
    """把一条过长且无法按句切分的字幕按字数均分到多个时间片。"""
    text = block["text"].strip()
    duration = block["end"] - block["start"]
    chunk_count = max(1, int(duration // max_duration) or 1)
    chunk_size = max(10, math.ceil(len(text) / chunk_count))
    parts = []
    start = block["start"]
    pos = 0
    while pos < len(text):
        piece = text[pos:pos + chunk_size].strip()
        if not piece:
            break
        weight = len(piece) / max(len(text), 1)
        end = start + duration * weight
        parts.append({"start": start, "end": end, "text": piece})
        start = end
        pos += chunk_size
    if parts:
        parts[-1]["end"] = block["end"]
        return parts
    capped = dict(block)
    capped["end"] = block["start"] + max_duration
    return [capped]


def split_long_block(block: dict, max_duration: float = DEFAULT_MAX_DURATION) -> list:
    """把一条超过 ``max_duration`` 的字幕切开。

    优先**按句子切**（并按时长比例分配，保证时间轴连续）；
    整条没有句末标点时退回按字数切；再不行就把结束时间截断到上限。
    """
    duration = block["end"] - block["start"]
    sentences = split_sentences(block["text"])
    if duration <= max_duration:
        return [block]
    if len(sentences) <= 1:
        text = block["text"].strip()
        if len(text) > 80:
            return _chunk_text_block(block, max_duration)
        capped = dict(block)
        capped["end"] = block["start"] + max_duration
        return [capped]

    total_chars = sum(max(len(s), 1) for s in sentences)
    parts = []
    start = block["start"]
    for sentence in sentences:
        weight = max(len(sentence), 1) / total_chars
        end = start + duration * weight
        parts.append({"start": start, "end": end, "text": sentence})
        start = end
    if parts:
        parts[-1]["end"] = block["end"]
    # 切完仍然过长的（单句本身就很长）继续递归
    result = []
    for part in parts:
        if part["end"] - part["start"] > max_duration:
            result.extend(split_long_block(part, max_duration))
        else:
            result.append(part)
    return result


def clean_blocks(blocks: list,
                 max_duration: float = DEFAULT_MAX_DURATION,
                 similarity: float = DEFAULT_SIMILARITY,
                 recent_window: int = 8) -> list:
    """清理字幕块序列：先去重，再把过长的切开。

    ``recent_window`` 只回看最近 N 条 —— 一部片子前后出现同样的台词是正常的，
    全量去重会误删。
    """
    if not blocks:
        return []
    cleaned: list = []
    recent: list = []
    for block in blocks:
        text = collapse_text(block["text"])
        if not text:
            continue
        key = norm_text(text)
        duplicate = False
        for prev in recent[-recent_window:]:
            prev_key = norm_text(prev["text"])
            if key == prev_key:
                duplicate = True
                break
            if key and prev_key and \
                    SequenceMatcher(None, prev_key, key).ratio() >= similarity:
                duplicate = True
                break
        if duplicate:
            continue
        block = dict(block)
        block["text"] = text
        cleaned.append(block)
        recent.append(block)

    result: list = []
    for block in cleaned:
        result.extend(split_long_block(block, max_duration))
    return result


# ── 各格式的解析 / 渲染 ─────────────────────────────────────

def parse_srt(text: str) -> list:
    """解析 SRT。容忍序号缺失、``.`` 与 ``,`` 两种毫秒分隔符。"""
    blocks = []
    for raw in re.split(r"\n\s*\n", (text or "").strip()):
        lines = raw.strip().splitlines()
        if len(lines) < 3:
            continue
        time_line = next((ln for ln in lines if "-->" in ln), None)
        if time_line is None:
            continue
        m = _TIME_RE.match(time_line.strip())
        if not m:
            continue
        index = lines.index(time_line)
        content = "\n".join(lines[index + 1:]).strip()
        if content:
            blocks.append({
                "start": _timestamp_to_seconds(m.group(1), m.group(2), m.group(3), m.group(4)),
                "end": _timestamp_to_seconds(m.group(5), m.group(6), m.group(7), m.group(8)),
                "text": content,
            })
    return blocks


def render_srt(blocks: list) -> str:
    out = []
    for index, block in enumerate(blocks, 1):
        out.append(str(index))
        out.append(f"{_seconds_to_srt_ts(block['start'])} --> {_seconds_to_srt_ts(block['end'])}")
        out.append(block["text"])
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def parse_vtt(text: str) -> tuple:
    """解析 WebVTT，返回 ``(blocks, header)``（header 原样保留）。"""
    lines = (text or "").strip().splitlines()
    header = "WEBVTT"
    blocks = []
    current = None
    started = False
    for line in lines:
        if not started:
            if line.strip():
                header = line.strip()
                started = True
            continue
        if "-->" in line:
            m = _TIME_RE.match(line.strip())
            if m:
                current = {
                    "start": _timestamp_to_seconds(m.group(1), m.group(2), m.group(3), m.group(4)),
                    "end": _timestamp_to_seconds(m.group(5), m.group(6), m.group(7), m.group(8)),
                    "text": "",
                }
                blocks.append(current)
        elif current is not None and line.strip():
            current["text"] = (current["text"] + "\n" + line.strip()).strip()
    return blocks, header


def render_vtt(blocks: list, header: str = "WEBVTT") -> str:
    out = [header, ""]
    for block in blocks:
        out.append(f"{_seconds_to_vtt_ts(block['start'])} --> {_seconds_to_vtt_ts(block['end'])}")
        out.append(block["text"])
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def clean_lrc_text(text: str) -> str:
    """LRC 只去掉相邻的重复行（时间轴本身不动）。"""
    out = []
    prev_key = None
    for line in (text or "").splitlines():
        m = _LRC_RE.match(line.strip())
        if m:
            key = norm_text(m.group(4))
            if key and key == prev_key:
                continue
            prev_key = key
        out.append(line.rstrip())
    return "\n".join(out) + "\n"


def clean_plain_text(text: str) -> str:
    """纯文本只去掉相邻的重复行。"""
    out = []
    prev_key = None
    for line in (text or "").splitlines():
        key = norm_text(line)
        if key and key == prev_key:
            continue
        prev_key = key
        out.append(line.rstrip())
    return "\n".join(out) + "\n"


def clean_subtitle_text(text: str, fmt: str,
                        max_duration: float = DEFAULT_MAX_DURATION,
                        similarity: float = DEFAULT_SIMILARITY) -> str:
    """按格式清理一段字幕文本，返回清理后的文本（不需要写文件时用它）。"""
    fmt = (fmt or "").lower().lstrip(".")
    if fmt == "srt":
        return render_srt(clean_blocks(parse_srt(text), max_duration, similarity))
    if fmt == "vtt":
        blocks, header = parse_vtt(text)
        return render_vtt(clean_blocks(blocks, max_duration, similarity), header)
    if fmt == "lrc":
        return clean_lrc_text(text)
    if fmt == "txt":
        return clean_plain_text(text)
    return text


def clean_file(path,
               max_duration: float = DEFAULT_MAX_DURATION,
               similarity: float = DEFAULT_SIMILARITY) -> tuple:
    """就地清理一个字幕文件，返回 ``(去掉的条数, 是否发生了变化)``。

    只处理 :data:`SUPPORTED_FORMATS` 里的扩展名；其它扩展名原样返回
    ``(0, False)``。读取用 ``utf-8-sig``（兼容带 BOM 的文件），
    写入统一用 ``utf-8``。
    """
    p = Path(str(path))
    fmt = p.suffix.lower().lstrip(".")
    if fmt not in SUPPORTED_FORMATS or not p.is_file():
        return 0, False
    try:
        original = p.read_text(encoding="utf-8-sig", errors="ignore")
    except OSError:
        return 0, False

    removed = 0
    if fmt == "srt":
        blocks = parse_srt(original)
        cleaned = clean_blocks(blocks, max_duration, similarity)
        new_text = render_srt(cleaned)
        removed = max(0, len(blocks) - len(cleaned))
    elif fmt == "vtt":
        blocks, header = parse_vtt(original)
        cleaned = clean_blocks(blocks, max_duration, similarity)
        new_text = render_vtt(cleaned, header)
        removed = max(0, len(blocks) - len(cleaned))
    else:
        new_text = clean_subtitle_text(original, fmt, max_duration, similarity)

    if new_text.strip() == original.strip():
        return 0, False
    try:
        p.write_text(new_text, encoding="utf-8")
    except OSError:
        return 0, False
    return removed, True


def find_subtitles(video_path, formats=("srt",), output_dir: Optional[str] = None) -> list:
    """找某个视频对应的字幕文件（用于判断"这条到底生成了没有"）。

    默认在视频同目录找；给了 ``output_dir`` 就去那里找（与生成工具的输出目录一致）。
    """
    video = Path(str(video_path))
    stem = video.stem
    folder = Path(str(output_dir)) if output_dir else video.parent
    out = []
    for fmt in formats:
        for candidate in (folder / f"{stem}.{fmt}",
                          folder / f"{stem}.zh.{fmt}"):
            if candidate.is_file():
                out.append(candidate)
    return out
