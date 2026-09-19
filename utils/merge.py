"""分集合并纯函数层 — 番号归一化 / 分集识别 / 版本剥离 / 去重 / 参数一致性 / 合并计划。

依赖：标准库 + utils.library.extract_number（复用现有番号提取）。
与 Qt 完全解耦，可单元测试。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from utils.library import extract_number
from utils.nfo import read_text_any_encoding
from utils.nfo_source import read_nfo_title

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv",
                    ".ts", ".rmvb", ".webm", ".mpg")

# mp4 容器可无损容纳的编解码器（其余回退 mkv/原容器）
MP4_COMPAT_VIDEO = {"h264", "hevc", "h265", "mpeg4", ""}
MP4_COMPAT_AUDIO = {"aac", "mp3", "ac3", "eac3", "alac", "opus", ""}

# Jellyfin/下载器自动生成的附属视频（主题曲/预告/采样/幕后等），不参与分集合并
AUXILIARY_WORDS = {
    "theme", "trailer", "teaser", "preview", "sample", "backdrop",
    "poster", "logo", "banner", "thumb", "thumbnail", "screenshot",
    "clearlogo", "clearart", "landscape", "fanart", "extrafanart",
    "extrathumbs", "interview", "featurette", "bloopers", "credits",
    "deleted", "making",
    "预告", "预告片", "主题曲", "片头", "片尾", "幕后", "花絮", "采访", "特典",
}
AUXILIARY_MULTIWORD = (
    "behind the scenes", "behind-the-scenes", "making of", "making-of",
    "deleted scene", "deleted-scene",
)
AUXILIARY_PATH_KEYWORDS = (
    "trailers", "extras", "trickplay", ".trickplay", "backdrops",
    "extrafanart", "extrathumbs", "behind the scenes", "behind-the-scenes",
)

# ── 数据模型 ──


@dataclass
class PartProbe:
    """ffprobe 探测结果。"""
    ok: bool = True
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    pixel_format: str = ""
    video_codec: str = ""
    audio_codec: str = ""
    sample_rate: int = 0
    channels: int = 0
    video_stream_index: int = -1
    audio_stream_index: int = -1
    has_attached_pic: bool = False
    size: int = 0
    container: str = ""
    video_time_base: str = ""
    sample_aspect_ratio: str = ""
    color_primaries: str = ""
    color_transfer: str = ""
    color_space: str = ""
    color_range: str = ""
    channel_layout: str = ""
    r_fps: float = 0.0
    nb_frames: int = 0
    error: str = ""


@dataclass
class PartFile:
    """一个分集文件。"""
    path: str
    name: str
    stem: str
    number: str
    part_index: int
    part_kind: str
    variants: list = field(default_factory=list)
    probe: PartProbe = field(default_factory=PartProbe)
    title: str = ""


@dataclass
class EpisodeGroup:
    """同番号分组。"""
    number: str
    parts: list
    status: str = "ready"
    issues: list = field(default_factory=list)
    duplicate_paths: list = field(default_factory=list)
    selected_paths: list = field(default_factory=list)


@dataclass
class MergePlan:
    """一次扫描的完整计划。"""
    groups: list
    unrecognized: list
    stats: dict


# ── 番号提取与归一化 ──


def extract_number_ext(name: str) -> Optional[str]:
    """扩展番号提取：在现有 extract_number 基础上支持 FC2-PPV-数字。"""
    if not name:
        return None
    n = re.sub(r"\.(mp4|mkv|avi|mov|flv|wmv|ts|rmvb|webm|mpg)$", "",
               str(name), flags=re.I)
    m = re.search(r"\bfc2\s*[-_ .]?\s*ppv\s*[-_ .]?\s*(\d{5,7})", n, re.I)
    if m:
        return f"FC2-{m.group(1)}"
    return extract_number(n)


def normalize_number(number: str) -> str:
    """归一化番号：去分隔符、转大写，用于分组键。"""
    if not number:
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(number).upper())


def display_number_for_group(group) -> str:
    """输出/备份时使用可读番号，优先保留 FC2-XXXXXX 这类原始连字符。"""
    for p in group.parts:
        if getattr(p, "number", ""):
            return p.number
    return group.number


# ── 版本后缀剥离 ──

_VARIANT_WORDS = {
    "破解", "流出", "无码", "字幕", "restored", "uncensored",
    "1080p", "720p", "2160p", "4k", "hd", "h720", "ch",
}
# 单字母版本标记：U=无码、C=字幕版（与分集字母 A/B/D/E... 区分）
_VARIANT_LETTERS = {"u", "c"}


def _is_variant_token(tok: str) -> bool:
    low = tok.lower()
    # 来源标记：含 @ 或常见域名
    if "@" in tok:
        return True
    if re.fullmatch(r"[\w.-]+\.(me|org|com|net|cc|xyz|club)", low):
        return True
    if low in _VARIANT_WORDS or low in _VARIANT_LETTERS:
        return True
    if re.fullmatch(r"h[\s_-]?720", low):
        return True
    return False


def _split_tokens(s: str) -> list:
    """按常见分隔符拆分文件名主干为 token 序列。"""
    return [t for t in re.split(r"[-_.\s]+", s) if t]


def strip_variant_tokens(stem: str) -> tuple:
    """剥离版本标记（-破解/-流出/-U/-C/1080p/来源等），返回 (清理后串, 命中列表)。"""
    if not stem:
        return "", []
    kept, found = [], []
    for tok in _split_tokens(stem):
        if _is_variant_token(tok):
            found.append(tok)
        else:
            kept.append(tok)
    return " ".join(kept), found


# ── 分集识别 ──


def _strip_number_from_stem(stem: str, number: str) -> str:
    if not number:
        return stem
    return re.sub(re.escape(number), "", stem, count=1, flags=re.I).strip()


def _letter_to_index(s: str) -> int:
    return ord(s.strip().lower()) - ord("a") + 1


_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_numeral(s: str) -> Optional[int]:
    if s in _CN_DIGITS:
        return _CN_DIGITS[s]
    if s == "十":
        return 10
    if re.fullmatch(r"十[一二三四五六七八九]", s):
        return 10 + _CN_DIGITS[s[1]]
    if re.fullmatch(r"[一二三四五六七八九]十", s):
        return _CN_DIGITS[s[0]] * 10
    return None


def _cn_index(t: str) -> Optional[int]:
    m = re.search(r"第\s*(\d{1,3})\s*[集話话]", t)
    if m:
        return int(m.group(1))
    m = re.search(r"第\s*([一二三四五六七八九十百]+)\s*[部集話话]", t)
    if m:
        v = _cn_numeral(m.group(1))
        if v:
            return v
    if re.search(r"(上集|前篇|第一部|上卷|前編)", t):
        return 1
    if re.search(r"(下集|后篇|後篇|第二部|下卷|後編)", t):
        return 2
    if re.search(r"(中集|第三部|中篇)", t):
        return 3
    return None


def parse_part_index(stem: str, number: str) -> tuple:
    """从文件名识别分集序号，返回 (part_index, part_kind)。

    优先级：cd/disc/part > 中文 > 纯数字 > 字母。
    part_index：base=0；cd1/part1/-1/_1/.1/A/第1集=1 ...
    """
    if not stem:
        return 0, "base"
    rem = _strip_number_from_stem(stem, number)
    if not rem:
        return 0, "base"
    tokens = [t for t in _split_tokens(rem) if not _is_variant_token(t)]
    if not tokens:
        return 0, "base"
    # 1) cd / disc / part
    for t in tokens:
        m = re.fullmatch(r"(?:cd|disc|part)\s*(\d{1,3}|[a-h])", t, re.I)
        if m:
            g = m.group(1)
            idx = _letter_to_index(g) if re.fullmatch(r"[a-h]", g, re.I) else int(g)
            return idx, "cd"
    # 2) 中文
    for t in tokens:
        idx = _cn_index(t)
        if idx is not None:
            return idx, "cn"
    # 3) 纯数字
    for t in tokens:
        m = re.fullmatch(r"\(?(\d{1,3})\)?", t)
        if m:
            return int(m.group(1)), "numeric"
    # 4) 单字母 A-H（C/U 已被版本过滤）
    for t in tokens:
        if re.fullmatch(r"[a-h]", t, re.I):
            return _letter_to_index(t), "letter"
    return 0, "base"


# ── 分组 ──


def group_by_number(paths) -> tuple:
    """按番号分组。返回 (groups: {norm_number: [PartFile]}, unrecognized: [path])。"""
    groups = defaultdict(list)
    unrecognized = []
    for p in paths:
        path = str(p)
        name = os.path.basename(path)
        stem, _ext = os.path.splitext(name)
        number = extract_number_ext(stem) or extract_number_ext(name)
        if not number:
            unrecognized.append(path)
            continue
        variants = strip_variant_tokens(stem)[1]
        part_index, part_kind = parse_part_index(stem, number)
        pf = PartFile(path=path, name=name, stem=stem, number=number,
                      part_index=part_index, part_kind=part_kind,
                      variants=variants)
        groups[normalize_number(number)].append(pf)
    return dict(groups), unrecognized


# ── ffprobe 探测 ──


def _creationflags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _parse_rate(raw) -> float:
    if not raw:
        return 0.0
    raw = str(raw)
    if "/" in raw:
        try:
            a, b = raw.split("/")
            b = int(b)
            if b:
                return round(int(a) / b, 4)
        except Exception:  # noqa: BLE001
            pass
    else:
        try:
            return round(float(raw), 4)
        except Exception:  # noqa: BLE001
            pass
    return 0.0


def _is_vfr(probe: PartProbe, tolerance: float = 0.02) -> bool:
    """avg_frame_rate 与 r_frame_rate 明显不一致时视为可变帧率(VFR)。"""
    if probe.fps <= 0 or probe.r_fps <= 0:
        return False
    return abs(probe.fps - probe.r_fps) > max(0.05, probe.fps * tolerance)


def probe_part(ffprobe: str, path: str, timeout: int = 120) -> PartProbe:
    """用 ffprobe 探测单个视频文件，任何失败返回 ok=False。"""
    probe = PartProbe()
    try:
        probe.size = os.path.getsize(path)
        probe.container = os.path.splitext(path)[1].lower().lstrip(".")
    except OSError as e:
        probe.ok = False
        probe.error = str(e)
        return probe
    if not ffprobe:
        probe.ok = False
        probe.error = "未找到 ffprobe"
        return probe
    try:
        r = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=_creationflags(), timeout=timeout)
    except subprocess.TimeoutExpired:
        probe.ok = False
        probe.error = "ffprobe 超时"
        return probe
    except Exception as e:  # noqa: BLE001
        probe.ok = False
        probe.error = str(e)
        return probe
    if r.returncode != 0 or not r.stdout or not r.stdout.strip():
        probe.ok = False
        probe.error = (r.stderr or "ffprobe 无输出").strip()[:200]
        return probe
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        probe.ok = False
        probe.error = f"元数据解析异常: {e}"
        return probe
    for s in data.get("streams", []):
        ct = s.get("codec_type", "")
        if ct == "video":
            if (s.get("disposition") or {}).get("attached_pic"):
                probe.has_attached_pic = True
                continue
            if probe.video_codec:
                continue
            try:
                probe.video_stream_index = int(s.get("index", -1))
            except (TypeError, ValueError):
                probe.video_stream_index = -1
            probe.video_codec = (s.get("codec_name") or "").lower()
            probe.width = s.get("width") or 0
            probe.height = s.get("height") or 0
            probe.fps = _parse_rate(s.get("avg_frame_rate"))
            probe.r_fps = _parse_rate(s.get("r_frame_rate"))
            probe.pixel_format = (s.get("pix_fmt") or "").lower()
            probe.video_time_base = s.get("time_base") or ""
            probe.sample_aspect_ratio = (s.get("sample_aspect_ratio") or "").lower()
            probe.color_primaries = (s.get("color_primaries") or "").lower()
            probe.color_transfer = (s.get("color_transfer") or "").lower()
            probe.color_space = (s.get("color_space") or "").lower()
            probe.color_range = (s.get("color_range") or "").lower()
            try:
                probe.nb_frames = int(s.get("nb_frames") or 0)
            except (TypeError, ValueError):
                probe.nb_frames = 0
        elif ct == "audio":
            if probe.audio_codec:
                continue
            try:
                probe.audio_stream_index = int(s.get("index", -1))
            except (TypeError, ValueError):
                probe.audio_stream_index = -1
            probe.audio_codec = (s.get("codec_name") or "").lower()
            probe.channel_layout = (s.get("channel_layout") or "").lower()
            try:
                probe.sample_rate = int(s.get("sample_rate") or 0)
            except (TypeError, ValueError):
                probe.sample_rate = 0
            try:
                probe.channels = int(s.get("channels") or 0)
            except (TypeError, ValueError):
                probe.channels = 0
    fmt = data.get("format", {})
    try:
        probe.duration = float(fmt.get("duration") or 0)
    except (TypeError, ValueError):
        probe.duration = 0.0
    probe.ok = True
    return probe


def validate_merged_output(ffprobe: str, path: str, parts: list,
                           timeout: int = 120) -> tuple[bool, str]:
    """用 ffprobe 做轻量输出校验：只读元数据，不解码。

    检查输出是否存在视频流、时长是否接近各分集之和、帧率是否接近首分集。
    这能捕获 MP4 concat 时间戳错误导致的 10fps/时长异常，而不会像完整解码那样吃 CPU。
    """
    if not ffprobe or not path:
        return False, "缺少 ffprobe 或输出路径"
    probe = probe_part(ffprobe, path, timeout=timeout)
    if not probe.ok:
        return False, probe.error or "输出探测失败"
    if not probe.video_codec or probe.duration <= 0:
        return False, "输出文件缺少可读视频流或时长为 0"

    known_durations = [p.probe.duration for p in parts if p.probe.duration > 0]
    if len(known_durations) == len(parts) and known_durations:
        expected_duration = sum(known_durations)
        dur_tolerance = max(2.0, expected_duration * 0.02)
        if abs(probe.duration - expected_duration) > dur_tolerance:
            return False, (
                f"输出时长异常：{probe.duration:.2f}s，预期约 {expected_duration:.2f}s"
            )

    first_fps = parts[0].probe.fps if parts else 0.0
    if first_fps > 0 and probe.fps > 0:
        diff = abs(probe.fps - first_fps)
        if diff > max(0.05, first_fps * 0.10):
            return False, (
                f"输出帧率异常：{probe.fps:.3f}fps，预期约 {first_fps:.3f}fps"
            )

    first_video = parts[0].probe.video_codec if parts else ""
    if first_video and probe.video_codec != first_video:
        return False, f"输出视频编码异常：{probe.video_codec}，预期 {first_video}"

    had_audio = any(p.probe.audio_codec for p in parts)
    if had_audio and not probe.audio_codec:
        return False, "输出丢失音频流"

    first_sar = parts[0].probe.sample_aspect_ratio if parts else ""
    if first_sar and probe.sample_aspect_ratio and probe.sample_aspect_ratio != first_sar:
        return False, f"输出像素宽高比异常：{probe.sample_aspect_ratio}，预期 {first_sar}"

    for attr in ("color_primaries", "color_transfer", "color_space", "color_range"):
        expected = getattr(parts[0].probe, attr) if parts else ""
        actual = getattr(probe, attr)
        if expected and actual and actual != expected:
            return False, f"输出色彩元数据异常（{attr}）：{actual}，预期 {expected}"

    return True, "元数据校验通过"


# ── 参数一致性 ──


def check_merge_compat(parts: list) -> tuple:
    """校验分集参数一致性。返回 (是否兼容, 问题列表)。"""
    issues = []
    if len(parts) < 2:
        return True, issues
    ref = parts[0].probe
    for p in parts[1:]:
        pr = p.probe
        if ref.video_codec and pr.video_codec and ref.video_codec != pr.video_codec:
            issues.append(f"{p.name}: 视频编码不同 ({ref.video_codec} vs {pr.video_codec})")
        if ref.width and pr.width and (ref.width != pr.width or ref.height != pr.height):
            issues.append(f"{p.name}: 分辨率不同 ({ref.width}x{ref.height} vs {pr.width}x{pr.height})")
        fps_tol = max(0.01, min(ref.fps, pr.fps) * 0.01) if (ref.fps and pr.fps) else 0.05
        if ref.fps and pr.fps and abs(ref.fps - pr.fps) > fps_tol:
            issues.append(f"{p.name}: 帧率不同 ({ref.fps} vs {pr.fps})")
        if ref.pixel_format and pr.pixel_format and ref.pixel_format != pr.pixel_format:
            issues.append(f"{p.name}: 像素格式不同 ({ref.pixel_format} vs {pr.pixel_format})")
        if ref.sample_aspect_ratio and pr.sample_aspect_ratio and ref.sample_aspect_ratio != pr.sample_aspect_ratio:
            issues.append(f"{p.name}: 像素宽高比不同 ({ref.sample_aspect_ratio} vs {pr.sample_aspect_ratio})")
        if ref.color_primaries and pr.color_primaries and ref.color_primaries != pr.color_primaries:
            issues.append(f"{p.name}: 色彩原色不同 ({ref.color_primaries} vs {pr.color_primaries})")
        if ref.color_transfer and pr.color_transfer and ref.color_transfer != pr.color_transfer:
            issues.append(f"{p.name}: 传输特性不同 ({ref.color_transfer} vs {pr.color_transfer})")
        if ref.color_space and pr.color_space and ref.color_space != pr.color_space:
            issues.append(f"{p.name}: 色彩空间不同 ({ref.color_space} vs {pr.color_space})")
        if ref.color_range and pr.color_range and ref.color_range != pr.color_range:
            issues.append(f"{p.name}: 色彩范围不同 ({ref.color_range} vs {pr.color_range})")
        if ref.audio_codec != pr.audio_codec:
            issues.append(f"{p.name}: 音频编码不同 ({ref.audio_codec or '无'} vs {pr.audio_codec or '无'})")
        if ref.sample_rate and pr.sample_rate and ref.sample_rate != pr.sample_rate:
            issues.append(f"{p.name}: 音频采样率不同 ({ref.sample_rate} vs {pr.sample_rate})")
        if ref.channels and pr.channels and ref.channels != pr.channels:
            issues.append(f"{p.name}: 声道数不同 ({ref.channels} vs {pr.channels})")
        if ref.channel_layout and pr.channel_layout and ref.channel_layout != pr.channel_layout:
            issues.append(f"{p.name}: 声道布局不同 ({ref.channel_layout} vs {pr.channel_layout})")
        if _is_vfr(ref) or _is_vfr(pr):
            issues.append(f"{p.name}: 检测到可变帧率(VFR)，存在音画不同步风险")
    return (not issues), issues


def validate_merge_selection(parts: list, allow_reencode: bool = False) -> tuple[bool, str]:
    """合并前做一次最终安全校验，避免把重复/同序号/不兼容分集拼坏。

    allow_reencode=True 时跳过“参数不一致”校验，改由调用方用转码归一化处理。
    """
    if len(parts) < 2:
        return False, "至少需要 2 个分集才能合并"
    seen: set = set()
    for p in parts:
        if not p.probe.ok:
            return False, f"分集探测失败：{p.name}"
        if not p.probe.video_codec:
            return False, f"缺少可读视频流：{p.name}"
        if p.path in seen:
            return False, f"分集路径重复：{p.name}"
        seen.add(p.path)
    index_counts = Counter(p.part_index for p in parts)
    if any(c > 1 for c in index_counts.values()):
        return False, "存在相同分集序号，请先排除重复/版本文件"
    if not allow_reencode:
        compatible, issues = check_merge_compat(parts)
        if not compatible:
            return False, "；".join(issues[:5])
    return True, ""


# ── 去重（内容级） ──


def quick_file_hash(path: str, head: int = 8 * 1024 * 1024,
                    tail: int = 8 * 1024 * 1024) -> str:
    """快速采样哈希：文件大小 + 首 8MB + 尾 8MB。失败返回空串。"""
    try:
        size = os.path.getsize(path)
        h = hashlib.sha256()
        h.update(str(size).encode())
        with open(path, "rb") as f:
            h.update(f.read(head))
            if size > head:
                f.seek(size - tail)
                h.update(f.read(tail))
        return h.hexdigest()
    except OSError:
        return ""


def pick_sample_timestamps(duration: float, count: int = 9) -> list:
    """均匀分布在 [5%, 95%] 的相对时间点。"""
    if duration <= 0 or count <= 0:
        return []
    if count == 1:
        fracs = [0.5]
    else:
        step = 0.9 / (count - 1)
        fracs = [0.05 + i * step for i in range(count)]
    return [max(0.0, min(duration - 0.01, duration * f)) for f in fracs]


def _dhash64(gray: bytes, w: int, h: int) -> int:
    """把 w*h 灰度原始帧缩为 9x8 后计算 64 位 dHash。"""
    W, H = 9, 8
    cells = []
    for r in range(H):
        y0 = r * h // H
        y1 = max(y0 + 1, (r + 1) * h // H)
        for c in range(W):
            x0 = c * w // W
            x1 = max(x0 + 1, (c + 1) * w // W)
            total = 0
            n = 0
            for y in range(y0, y1):
                base = y * w
                for x in range(x0, x1):
                    total += gray[base + x]
                    n += 1
            cells.append(total / n if n else 0)
    bits = 0
    idx = 0
    for r in range(H):
        for c in range(W - 1):
            if cells[r * W + c] > cells[r * W + c + 1]:
                bits |= (1 << idx)
            idx += 1
    return bits


def extract_frame_hashes(ffmpeg: str, path: str, timestamps: list,
                         size: int = 32, timeout: int = 60) -> Optional[list]:
    """用 ffmpeg 抽帧并计算 dHash。任一帧失败返回 None（视为无法判定）。"""
    if not ffmpeg or not path or not os.path.isfile(path) or not timestamps:
        return None
    hashes = []
    for t in timestamps:
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-accurate_seek", "-ss", f"{t:.3f}", "-i", str(path),
            "-map", "0:v:0", "-frames:v", "1",
            "-vf", f"scale={size}:{size}:flags=area,format=gray",
            "-f", "rawvideo", "-",
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                               creationflags=_creationflags())
        except Exception:  # noqa: BLE001
            return None
        if r.returncode != 0 or not r.stdout:
            return None
        gray = r.stdout[:size * size]
        if len(gray) != size * size:
            return None
        hashes.append(_dhash64(gray, size, size))
    return hashes


if hasattr(int, "bit_count"):
    def popcount(value: int) -> int:
        """二进制里 1 的个数。"""
        return value.bit_count()
else:
    # Python 3.9：int.bit_count() 还不存在（3.10 才加）。
    # 项目声明支持 3.9，所以这里留一条等价回退 —— 实测 CI 的 3.9 job
    # 就是被这一行打成 AttributeError 的。
    def popcount(value: int) -> int:
        """二进制里 1 的个数（3.9 兼容实现）。"""
        return bin(value).count("1")


def hamming_similarity(a: list, b: list, max_distance: int = 10) -> float:
    """两帧哈希序列的相似比例（Hamming 距离 ≤ max_distance 的帧占比）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    similar = sum(1 for x, y in zip(a, b) if popcount(x ^ y) <= max_distance)
    return similar / len(a)


def _quick_hash_cached(path: str, cache: dict) -> str:
    if path not in cache:
        cache[path] = quick_file_hash(path)
    return cache[path]


def _frame_hashes_cached_at(ffmpeg: str, path: str, timestamps: list,
                            cache: dict) -> Optional[list]:
    key = (path, tuple(timestamps))
    if key not in cache:
        cache[key] = extract_frame_hashes(ffmpeg, path, timestamps)
    return cache[key]


def _should_fingerprint(a: PartFile, b: PartFile) -> bool:
    """只有文件名结构上可能重复时才做画面指纹，避免对正常 CD1/CD2 浪费时间与 CPU。

    可能重复的结构：
    - 同序号（两个 base、两个 cd1 等）；
    - base 与 cd1（完整版与第一分集可能是同一内容）；
    - base/numeric/letter 这类无明确 CD 顺序的文件。
    """
    if a.part_index == b.part_index:
        return True
    kinds = {a.part_kind, b.part_kind}
    if kinds <= {"base", "numeric", "letter"}:
        return True
    if kinds == {"base", "cd"} and {a.part_index, b.part_index} == {0, 1}:
        return True
    return False


def _duplicate_pair(a: PartFile, b: PartFile, ffmpeg: str,
                    max_distance: int, sim_threshold: float,
                    frame_count: int, dur_tolerance: float,
                    hash_cache: Optional[dict] = None,
                    frame_cache: Optional[dict] = None) -> bool:
    """判定两个文件是否内容重复（字节级 或 画面指纹级）。"""
    pa, pb = a.probe, b.probe
    # 1) 字节级完全重复（同大小 + 首尾采样哈希一致）
    if pa.size and pa.size == pb.size:
        if hash_cache is not None:
            ha = _quick_hash_cached(a.path, hash_cache)
            hb = _quick_hash_cached(b.path, hash_cache)
        else:
            ha = quick_file_hash(a.path)
            hb = quick_file_hash(b.path)
        if ha and ha == hb:
            return True
    if not _should_fingerprint(a, b):
        return False

    # 2) 内容级：时长接近 + 画面 dHash 相似
    if pa.duration > 0 and pb.duration > 0 and abs(pa.duration - pb.duration) <= dur_tolerance:
        if ffmpeg:
            if frame_cache is not None:
                ts = pick_sample_timestamps(min(pa.duration, pb.duration), frame_count)
                hb_a = _frame_hashes_cached_at(ffmpeg, a.path, ts, frame_cache)
                hb_b = _frame_hashes_cached_at(ffmpeg, b.path, ts, frame_cache)
            else:
                ts = pick_sample_timestamps(min(pa.duration, pb.duration), frame_count)
                hb_a = extract_frame_hashes(ffmpeg, a.path, ts)
                hb_b = extract_frame_hashes(ffmpeg, b.path, ts)
            if hb_a is not None and hb_b is not None:
                return hamming_similarity(hb_a, hb_b, max_distance) >= sim_threshold
    return False


def detect_duplicates(parts: list, ffmpeg: str = "", options: Optional[dict] = None) -> tuple:
    """组内去重。返回 (保留的 PartFile 列表, 被排除的重复路径列表)。"""
    options = options or {}
    max_distance = int(options.get("merge_dup_hash_distance", 10))
    sim_threshold = float(options.get("merge_dup_sim_threshold", 0.8))
    frame_count = int(options.get("merge_dup_frame_count", 9))
    dur_tolerance = float(options.get("merge_dup_duration_tolerance", 3.0))
    kept: list = []
    duplicates: list = []
    hash_cache: dict = {}
    frame_cache: dict = {}
    for p in parts:
        if any(_duplicate_pair(p, q, ffmpeg, max_distance, sim_threshold,
                               frame_count, dur_tolerance,
                               hash_cache, frame_cache) for q in kept):
            duplicates.append(p.path)
        else:
            kept.append(p)
    return kept, duplicates


# ── 组状态判定 ──


def _has_index_conflict(parts: list) -> bool:
    idx_counts = Counter(p.part_index for p in parts)
    return any(c > 1 for c in idx_counts.values())


def _has_part_gap(parts: list) -> bool:
    indices = sorted({p.part_index for p in parts if p.part_kind != "base"})
    if not indices:
        return False
    has_base = any(p.part_kind == "base" for p in parts)
    if indices[0] > 1 and not has_base:
        return True
    for a, b in zip(indices, indices[1:]):
        if b - a > 1:
            return True
    return False


def _has_base_and_cd1(parts: list) -> bool:
    has_base = any(p.part_kind == "base" for p in parts)
    has_cd1 = any(p.part_kind == "cd" and p.part_index == 1 for p in parts)
    return has_base and has_cd1


def _is_variant_conflict(parts: list) -> bool:
    if len(parts) < 2:
        return False
    if len({p.part_index for p in parts}) != 1:
        return False
    variant_sets = {tuple(sorted(v.lower() for v in p.variants)) for p in parts}
    return len(variant_sets) > 1


def _classify_group(group: EpisodeGroup, kept: list, options: dict) -> tuple:
    """计算组状态与问题列表。"""
    issues: list = []
    max_parts = int(options.get("merge_max_parts", 20))
    min_duration = float(options.get("merge_min_part_seconds", 3.0))

    if len(group.parts) < 2:
        return "skip", ["单文件，无需合并"]

    bad = [p.name for p in kept if not p.probe.ok]
    if bad:
        return "needs_review", [f"探测失败: {', '.join(bad)}"]

    no_video = [p.name for p in kept if not p.probe.video_codec]
    if no_video:
        return "needs_review", [f"缺少视频流: {', '.join(no_video)}"]

    if any(p.probe.container == "iso" for p in kept):
        return "skip", ["含 ISO，不参与拼接"]

    if len(kept) < 2:
        if group.duplicate_paths:
            return "skip_duplicate", ["分集内容重复"]
        return "skip", ["去重后不足 2 个分集"]

    if _is_variant_conflict(kept):
        return "skip_variant", ["同番号多版本（版本标记不同），不合并"]

    if _has_index_conflict(kept):
        return "conflict", ["分集序号冲突/无法定序"]

    compatible, compat_issues = check_merge_compat(kept)
    if not compatible:
        return "needs_review", compat_issues

    if _has_part_gap(kept):
        issues.append("分集序号不连续，疑似缺失中间分集")
    if _has_base_and_cd1(kept):
        issues.append("同时存在 base 与 cd1，疑似完整版与第一分集重复")
    if issues:
        return "needs_review", issues

    if len(kept) > max_parts:
        return "needs_review", [f"分集数 {len(kept)} 超过上限 {max_parts}"]

    short = [p.name for p in kept
             if p.probe.duration and p.probe.duration < min_duration]
    if short:
        return "needs_review", [f"疑似预告/广告短分集: {'、'.join(short[:3])}"]

    return "ready", issues


# ── 合并计划 ──


def build_merge_plan(paths, ffmpeg: str = "", ffprobe: str = "",
                     options: Optional[dict] = None,
                     progress: Optional[Callable] = None,
                     should_stop: Optional[Callable] = None) -> MergePlan:
    """扫描 → 分组 → probe → 去重 → 兼容性 → 状态。纯逻辑，可在后台线程调用。"""
    options = options or {}
    groups, unrecognized = group_by_number(paths)
    total = sum(len(v) for v in groups.values())
    done = 0
    plans = []
    for number in sorted(groups):
        if should_stop and should_stop():
            break
        files = groups[number]
        for pf in files:
            if should_stop and should_stop():
                break
            pf.probe = probe_part(ffprobe, pf.path)
            pf.title = read_nfo_title(pf.path) or ""
            done += 1
            if progress and total:
                try:
                    progress(int(done / total * 100))
                except Exception:  # noqa: BLE001
                    pass
        if should_stop and should_stop():
            break
        files.sort(key=lambda p: p.part_index)
        kept, dup_paths = detect_duplicates(files, ffmpeg, options)
        group = EpisodeGroup(number=number, parts=files,
                             duplicate_paths=dup_paths,
                             selected_paths=[p.path for p in kept])
        group.status, group.issues = _classify_group(group, kept, options)
        if group.status in ("needs_review", "conflict"):
            group.selected_paths = []
        plans.append(group)
    return MergePlan(groups=plans, unrecognized=unrecognized,
                     stats=_plan_stats(plans, unrecognized))


def _plan_stats(plans: list, unrecognized: list) -> dict:
    counts = Counter(g.status for g in plans)
    return {
        "total_groups": len(plans),
        "unrecognized": len(unrecognized),
        "ready": counts.get("ready", 0),
        "needs_review": counts.get("needs_review", 0),
        "skip_variant": counts.get("skip_variant", 0),
        "skip_duplicate": counts.get("skip_duplicate", 0),
        "conflict": counts.get("conflict", 0),
        "skip": counts.get("skip", 0),
        "by_status": dict(counts),
    }


# ── 扫描与输出路径 ──


def is_auxiliary_media(path: str) -> bool:
    """判断是否为 Jellyfin/下载器生成的附属视频（theme/trailer/sample 等）。"""
    sp = str(path)
    stem_low = os.path.splitext(os.path.basename(sp))[0].lower()
    tokens = set(_split_tokens(stem_low))
    if tokens & AUXILIARY_WORDS:
        return True
    if any(ph in stem_low for ph in AUXILIARY_MULTIWORD):
        return True
    plow = sp.lower()
    if any(k in plow for k in AUXILIARY_PATH_KEYWORDS):
        return True
    return False


def scan_files(root_paths, extensions=VIDEO_EXTENSIONS,
               exclude_keywords=None) -> list:
    """递归扫描目录/文件，返回视频路径列表。"""
    exts = {e.lower() for e in extensions}
    exclude = [k.lower() for k in (exclude_keywords or []) if k]
    out = []
    seen = set()
    for root in root_paths:
        p = Path(root)
        if p.is_file():
            candidates = [p]
        elif p.is_dir():
            candidates = p.rglob("*")
        else:
            continue
        for f in candidates:
            if not f.is_file():
                continue
            if f.suffix.lower() not in exts:
                continue
            sp = str(f)
            if is_auxiliary_media(sp):
                continue
            if exclude and any(k in sp.lower() for k in exclude):
                continue
            if sp in seen:
                continue
            seen.add(sp)
            out.append(sp)
    return out


def unique_output_path(p: Path) -> Path:
    """同名输出自动追加 (2)/(3)...，绝不覆盖。"""
    if not p.exists():
        return p
    stem, ext = p.stem, p.suffix
    i = 2
    while True:
        cand = p.with_name(f"{stem} ({i}){ext}")
        if not cand.exists():
            return cand
        i += 1


def _container_compatible(parts: list, container: str) -> bool:
    """判断指定容器能否无损封装这些分集的视频/音频编码。"""
    container = container.lower()
    if container == "mkv":
        return True
    video_ok = {
        "mp4": MP4_COMPAT_VIDEO,
        "mov": MP4_COMPAT_VIDEO,
        "ts": {"h264", "hevc", "h265", "mpeg2video", "mpeg4", ""},
        "webm": {"vp8", "vp9", "av1", ""},
        "avi": {"h264", "mpeg4", "mpeg2video", ""},
    }.get(container, set())
    audio_ok = {
        "mp4": MP4_COMPAT_AUDIO,
        "mov": MP4_COMPAT_AUDIO,
        "ts": {"aac", "mp3", "ac3", "eac3", ""},
        "webm": {"opus", "vorbis", ""},
        "avi": {"mp3", "ac3", ""},
    }.get(container, set())
    for p in parts:
        vc = p.probe.video_codec or ""
        ac = p.probe.audio_codec or ""
        if vc not in video_ok or ac not in audio_ok:
            return False
    return True


def choose_output_container(parts: list, requested: str = "mp4") -> str:
    """选择输出容器：默认 mp4；编码不兼容时回退原容器（非 mp4）或 mkv。"""
    req = (requested or "mp4").lower()
    if req not in {"mp4", "mkv", "ts", "mov", "avi", "webm"}:
        req = "mp4"
    if req != "mp4":
        return req if _container_compatible(parts, req) else "mkv"
    if _container_compatible(parts, "mp4"):
        return "mp4"
    containers = {p.probe.container for p in parts if p.probe.container}
    if len(containers) == 1:
        c = next(iter(containers))
        if c in ("mkv", "ts", "mov", "avi", "webm"):
            return c
    return "mkv"


def build_concat_list(parts: list, list_path: str) -> None:
    """生成 ffmpeg concat demuxer 列表（UTF-8），单引号按 ffmpeg 规则转义。"""
    build_concat_paths([p.path for p in parts], list_path)


def build_concat_paths(paths: list, list_path: str) -> None:
    """从路径列表生成 ffmpeg concat demuxer 列表（UTF-8），单引号按 ffmpeg 规则转义。"""
    lines = []
    for path in paths:
        esc = path.replace("'", "'\\''")
        lines.append(f"file '{esc}'")
    with open(list_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def archive_originals(original_paths, backup_dir: str) -> tuple:
    """把原分集移动到备份目录（同盘 rename，瞬时）。返回 (已移动, 失败原因)。"""
    moved, failed = [], []
    try:
        Path(backup_dir).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return [], [str(e)]
    for src in original_paths:
        sp = Path(src)
        if not sp.exists():
            continue
        dst = unique_output_path(Path(backup_dir) / sp.name)
        try:
            os.replace(str(sp), str(dst))
            moved.append(str(dst))
        except OSError as e:
            failed.append(f"{sp.name}: {e}")
    return moved, failed


def discover_pending_merged_outputs(groups, extensions=VIDEO_EXTENSIONS) -> dict:
    """从分集所在目录发现已经生成、但还没替换原文件的「番号 合并.容器」。

    返回 {归一化番号: {"output": 合并文件路径, "originals": 原分集路径列表,
    "display_number": 显示番号}}，用于重启软件后继续替换流程。
    """
    exts = {str(e).lower() for e in extensions}
    discovered: dict = {}
    for group in groups:
        if len(group.parts) < 2:
            continue
        label = display_number_for_group(group) or group.number
        pattern = re.compile(re.escape(label) + r" 合并(?: \(\d+\))?")

        dirs = {Path(p.path).parent for p in group.parts}
        candidates = []
        seen = set()
        for directory in dirs:
            try:
                entries = list(directory.iterdir())
            except OSError:
                continue
            for entry in entries:
                try:
                    if not entry.is_file():
                        continue
                except OSError:
                    continue
                if entry.suffix.lower() not in exts:
                    continue
                if not pattern.fullmatch(entry.stem):
                    continue
                key = str(entry)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    mtime = 0.0
                candidates.append((mtime, entry))

        if not candidates:
            continue
        candidates.sort(key=lambda item: item[0], reverse=True)
        output = str(candidates[0][1])

        selected_paths = getattr(group, "selected_paths", None) or []
        if len(selected_paths) >= 2:
            originals = list(selected_paths)
        else:
            duplicates = getattr(group, "duplicate_paths", []) or []
            originals = [p.path for p in group.parts if p.path not in duplicates]

        discovered[group.number] = {
            "output": output,
            "originals": originals,
            "display_number": label,
        }
    return discovered


def discover_legacy_merged_files(root_paths, extensions=VIDEO_EXTENSIONS) -> list:
    """扫描目录，找出名称中含「合并」且主体是有效番号的旧合并视频。

    返回 [{"path", "stem", "base_stem", "display_number", "target"}]。
    """
    exts = {str(e).lower() for e in extensions}
    items = []
    seen = set()
    for root in root_paths:
        p = Path(root)
        if p.is_file():
            candidates = [p]
        elif p.is_dir():
            candidates = p.rglob("*")
        else:
            continue
        for candidate in candidates:
            try:
                if not candidate.is_file():
                    continue
            except OSError:
                continue
            if candidate.suffix.lower() not in exts:
                continue
            stem = candidate.stem
            key = str(candidate)
            if key in seen:
                continue
            lowered = stem.lower()
            if any(tag in lowered for tag in (".partial", ".norm", ".concat", ".inter")):
                continue
            mark = -1
            for candidate_marker in ("合并", "合併"):
                idx = stem.find(candidate_marker)
                if idx > 0 and (mark < 0 or idx < mark):
                    mark = idx
            if mark <= 0:
                continue
            seen.add(key)
            base_stem = stem[:mark].strip(" _-")
            base_stem = re.sub(r"\s*\(\d+\)$", "", base_stem).strip()
            if not base_stem:
                continue
            number = extract_number_ext(base_stem) or base_stem
            items.append({
                "path": key,
                "stem": stem,
                "base_stem": base_stem,
                "display_number": number,
                "target": str(candidate.with_name(number + candidate.suffix)),
            })
    return items


def finalize_legacy_merged_output(video_path: str) -> tuple[bool, str, str]:
    """把旧的「番号 合并.容器」重命名为「番号.容器」，并同步同名 NFO/字幕 sidecar。

    返回 (success, message, final_path)。不移动/删除原分集，仅做文件名与 NFO 同步。
    """
    sp = Path(video_path)
    if not sp.exists():
        return False, "文件不存在", ""
    stem = sp.stem
    if any(tag in stem.lower() for tag in (".partial", ".norm", ".concat", ".inter")):
        return False, "临时文件不是旧合并结果", ""
    mark = -1
    for candidate_marker in ("合并", "合併"):
        idx = stem.find(candidate_marker)
        if idx > 0 and (mark < 0 or idx < mark):
            mark = idx
    if mark <= 0:
        return False, "文件名不含「合并」标识", ""
    base_stem = stem[:mark].strip(" _-")
    base_stem = re.sub(r"\s*\(\d+\)$", "", base_stem).strip()
    number = extract_number_ext(base_stem) or base_stem
    target = sp.with_name(number + sp.suffix)
    if target == sp:
        return False, "目标文件名与原文件相同", ""
    if target.exists():
        return False, f"同名文件已存在，需走替换流程：{target.name}", ""

    sidecars = []
    try:
        for f in sp.parent.iterdir():
            if f == sp or not f.is_file():
                continue
            suffix = f.suffix.lower()
            if suffix in {".nfo", ".srt", ".ass", ".ssa", ".sub", ".vtt",
                          ".jpg", ".jpeg", ".png", ".webp"}:
                if f.stem == stem or f.stem.startswith(stem + "."):
                    sidecars.append(f)
    except OSError:
        pass

    try:
        os.replace(str(sp), str(target))
    except OSError as e:
        return False, f"重命名视频失败：{e}", ""

    for side in sidecars:
        remainder = side.stem[len(stem):]
        new_side = sp.with_name(target.stem + remainder + side.suffix)
        try:
            if side.suffix.lower() == ".nfo":
                text = read_text_any_encoding(str(side)) or ""
                if text:
                    if stem in text:
                        text = text.replace(stem, target.stem)
                    if base_stem and base_stem != target.stem and base_stem in text:
                        text = text.replace(base_stem, target.stem)
                    new_side.write_text(text, encoding="utf-8")
                    try:
                        os.remove(str(side))
                    except OSError:
                        pass
                else:
                    os.replace(str(side), str(new_side))
            else:
                os.replace(str(side), str(new_side))
        except OSError as e:
            # 视频已改名成功，sidecar 失败仅记录为非致命错误。
            return True, f"已重命名视频，但 {side.name} 同步失败：{e}", str(target)
    return True, "", str(target)
