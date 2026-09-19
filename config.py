"""Jellyfin Toolkit 配置管理 — 统一所有工具的配置入口。

数据目录策略（跨平台、可移植）：
- 冻结(exe)运行：默认写入用户数据目录（Windows ``%APPDATA%\\JellyfinToolkit``，
  macOS ``~/Library/Application Support/JellyfinToolkit``，Linux ``$XDG_CONFIG_HOME``
  或 ``~/.config/JellyfinToolkit``）；
- 便携模式：程序目录存在空文件 ``portable.txt`` 时，数据写到程序目录下的 ``AppData/``，
  整个文件夹可以拷到 U 盘直接跑；
- 源码运行：默认写项目目录，便于开发；
- 环境变量 ``JELLYFIN_TOOLKIT_DATA_DIR`` 优先级最高（CI / 测试用）。

配置里任何「本机专属」的值都不得作为默认值：默认值必须能在任意一台干净机器上
直接跑通，用户自己的路径一律由用户在设置页填写。
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, List

from version import APP_ID

# ── 默认配置 ────────────────────────────────────────────────
# 注意：下列默认值**不得**出现盘符、用户目录、特定硬件型号。
DEFAULT_CONFIG: dict[str, Any] = {
    # 媒体库数据源（Jellyfin / Emby 通用）
    #   library_source: auto = 有可读的库文件就用库文件，否则走服务器 API
    #                   db   = 只读本地数据库（jellyfin.db / library.db）
    #                   api  = 只走 REST API（Emby 或服务器在别的机器时推荐）
    "library_source": "auto",
    #   server_type: auto / jellyfin / emby —— 留 auto 时由 /System/Info/Public 自动识别
    "server_type": "auto",
    # 数据库路径留空 = **自动检测**本机常见安装位置（Jellyfin / Emby 都扫）。
    # 不能写成某个固定盘符：别人机器上没那个盘，功能会直接失效。
    "jellyfin_db_path": "",
    "jellyfin_data_dir": "",
    "uncensored_keywords": [
        "fc2", "1pondo", "carib", "10mu", "pacopaco", "heydouga",
        "tokyo hot", "tokyohot", "heyzo", "muramura", "luxu", "gachinco",
        "无码", "流出", "破解", "uncensored", "hack", "decensored"
    ],
    "subtitle_extensions": [".srt", ".vtt", ".ass", ".ssa", ".sub"],
    "video_extensions": [".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".ts", ".rmvb"],
    "min_video_size_mb": 10,
    "theme": "dark",
    # "" = 自动探测本机真正可用的编码器（推荐）；也可显式写 h264_nvenc / h264_amf / h264_qsv / libx264
    "ffmpeg_gpu_codec": "",
    # "" = 自动；可选 cuda / dxva2 / d3d11va / qsv / vaapi / videotoolbox / none
    "ffmpeg_hardware_accel": "",
    "transcode_crf": 23,
    # 单文件 ffmpeg 处理超时（秒）：超过则 kill 并保留原文件（防损坏文件无限挂起）
    "ffmpeg_timeout": 900,
    "restored_suffix_marker": "restored",
    # 清理范围：路径含以下关键词的目录默认不参与分析。留空表示不过滤
    "exclude_path_keywords": [],
    # 评分阈值（百分比）：命中度 >= 此 -> 可能喜欢；<= 此 -> 建议删除
    "score_th_keep": 55,
    "score_th_delete": 25,
    # 增强匹配（可对比前后）：相似锚点 + 题材共现 + AI 语义注入 + 反馈循环。默认开
    "enhance_match": True,
    # 评分引擎：v2 = 分桶画像+置信度加权+邻域传播（默认，更准）；
    #          v1 = 基础命中度（保留用于对比优化前后）
    "engine": "v2",
    # 新手引导是否已完成（首次运行自动进入）
    "onboarding_done": False,
    # 本地 LLM 二次校验（OpenAI 兼容，如 Ollama/LM Studio）。默认启用
    "ai_enabled": True,
    "ai_base_url": "http://localhost:11434/v1",
    "ai_model": "qwen3:8b",
    "ai_key": "",
    # 媒体服务器 REST API 同步（清洗脏标签 / 触发全库刷新 / API 采集）。
    # Jellyfin 与 Emby 用同一套接口与同一个认证头，填 API Key 后两者都能用
    "jellyfin_server": "http://localhost:8096",
    "jellyfin_api_key": "",
    # 分集合并
    "merge_ffmpeg_path": "",
    "merge_output_container": "mp4",
    "merge_original_subfolder": "_merged_originals",
    # 确认替换后的备份根目录（建议指向大容量盘）。
    # 留空 = 自动回退到数据目录下的 backups/（永远可写，不需要额外盘符）
    "merge_backup_root": "",
    # 替换原分集时，是否把合并文件从「番号 合并.mp4」改名为「番号.mp4」
    "merge_rename_to_number": True,
    "merge_dup_frame_count": 9,
    "merge_dup_hash_distance": 10,
    "merge_dup_sim_threshold": 0.8,
    "merge_max_parts": 20,
    "merge_min_part_seconds": 3.0,
    "merge_timeout_per_file": 3600,
}

PORTABLE_MARKER = "portable.txt"
PORTABLE_ENV = "JELLYFIN_TOOLKIT_PORTABLE"
DATA_DIR_ENV = "JELLYFIN_TOOLKIT_DATA_DIR"


# ── 数据目录解析 ────────────────────────────────────────────
def program_dir() -> Path:
    """程序所在目录（冻结后为 exe 目录，源码模式为项目根）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def portable_mode() -> bool:
    """便携模式：程序目录有 ``portable.txt``，或设置了 ``JELLYFIN_TOOLKIT_PORTABLE``。"""
    if os.environ.get(PORTABLE_ENV):
        return True
    try:
        return (program_dir() / PORTABLE_MARKER).exists()
    except OSError:
        return False


def user_data_root() -> Path:
    """按平台规范给出用户数据根目录。"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        root = Path(xdg) if xdg else Path.home() / ".config"
    return root / APP_ID


def _resolve_base_dir() -> Path:
    """决定配置/数据根目录。优先级：环境变量 > 便携模式 > 平台规范/项目目录。"""
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    if portable_mode():
        return program_dir() / "AppData"
    if getattr(sys, "frozen", False):
        return user_data_root()
    # 源码模式：写项目目录，方便开发与随手查看
    return program_dir()

BASE_DIR = _resolve_base_dir()
CONFIG_FILE = BASE_DIR / "config.json"
DATA_DIR = BASE_DIR / "data"


def set_data_root(root: str | Path, *, rebind_modules: bool = True) -> Path:
    """运行时重定向数据根目录，并把已导入模块里绑定的旧值一起换掉。

    很多模块用 ``from config import DATA_DIR``，属于**导入期绑定**；
    只改 ``config.DATA_DIR`` 对它们无效。这里按对象身份（identity）遍历
    ``sys.modules``，把仍然指向旧对象的同名属性重绑到新路径。

    Returns:
        新的数据根目录
    """
    global BASE_DIR, CONFIG_FILE, DATA_DIR
    old = {"BASE_DIR": BASE_DIR, "CONFIG_FILE": CONFIG_FILE, "DATA_DIR": DATA_DIR}

    new_base = Path(root).expanduser().resolve()
    BASE_DIR = new_base
    CONFIG_FILE = new_base / "config.json"
    DATA_DIR = new_base / "data"
    new = {"BASE_DIR": BASE_DIR, "CONFIG_FILE": CONFIG_FILE, "DATA_DIR": DATA_DIR}

    if rebind_modules:
        for module in list(sys.modules.values()):
            namespace = getattr(module, "__dict__", None)
            if not isinstance(namespace, dict):
                continue
            for name, old_value in old.items():
                if namespace.get(name) is old_value:
                    namespace[name] = new[name]
    return DATA_DIR


def used_configured_data_dir() -> bool:
    """当前数据目录是否来自环境变量覆盖（用于界面提示）。"""
    return bool(os.environ.get(DATA_DIR_ENV))


# ── 配置收敛 / 自愈 ─────────────────────────────────────────
_DRIVE_RE = re.compile(r"^([A-Za-z]):([\\/]|$)")


def _drive_exists(path: str) -> bool:
    """Windows 盘符路径是否可达。

    **非 Windows 一律返回 True（即"不判断"）**：POSIX 上"带盘符的路径"只是一个
    叫 "C:" 之类的普通相对目录名，根本无从判断它是否"失效"。替用户做决定的风险是
    **把填好的配置静默清掉**，比留着不管糟得多；真读不到时
    ``utils.library_source.resolve()`` 会给出明确提示并回退到服务器 API。
    """
    match = _DRIVE_RE.match(path.strip())
    if not match:
        return True
    if sys.platform != "win32":
        return True
    return Path(f"{match.group(1).upper()}:\\").exists()


def _coerce_config(data: dict) -> dict:
    """把磁盘 JSON 里的值收敛到期望类型；非法值回退到默认。"""
    out = dict(DEFAULT_CONFIG)
    for key, default in DEFAULT_CONFIG.items():
        value = data.get(key)
        if value is None:
            continue
        if isinstance(default, bool):
            out[key] = value if isinstance(value, bool) else default
        elif isinstance(default, int):
            try:
                out[key] = int(value)
            except (TypeError, ValueError):
                out[key] = default
        elif isinstance(default, list):
            out[key] = [str(x) for x in value] if isinstance(value, list) else default
        elif isinstance(default, float):
            try:
                out[key] = float(value)
            except (TypeError, ValueError):
                out[key] = default
        else:
            out[key] = value if isinstance(value, str) else default
    return out


def heal_config(data: dict) -> tuple[dict, list[str]]:
    """把「换台机器就失效」的配置项就地自愈，返回 (配置, 说明列表)。

    目前处理：
    - 备份根目录指向的盘符在本机不存在 → 清空，回退到数据目录；
    - 数据库路径 / 数据目录指向的盘符在本机不存在 → 清空，交给「自动检测」重新定位。
    """
    notes: list[str] = []
    backup_root = str(data.get("merge_backup_root") or "").strip()
    if backup_root and not _drive_exists(backup_root):
        data["merge_backup_root"] = ""
        notes.append(
            f"配置里的备份根目录 {backup_root} 在本机不存在，已改为数据目录下的 backups/")
    for key, label in (("jellyfin_db_path", "数据库路径"),
                       ("jellyfin_data_dir", "数据目录")):
        value = str(data.get(key) or "").strip()
        if value and not _drive_exists(value):
            data[key] = ""
            notes.append(f"配置里的{label} {value} 所在盘符在本机不存在，已清空并改为自动检测")
    return data, notes


@dataclass
class ToolkitConfig:
    """全局配置数据类，支持 JSON 持久化。"""
    library_source: str = DEFAULT_CONFIG["library_source"]
    server_type: str = DEFAULT_CONFIG["server_type"]
    jellyfin_db_path: str = DEFAULT_CONFIG["jellyfin_db_path"]
    jellyfin_data_dir: str = DEFAULT_CONFIG["jellyfin_data_dir"]
    uncensored_keywords: List[str] = field(default_factory=lambda: list(DEFAULT_CONFIG["uncensored_keywords"]))
    subtitle_extensions: List[str] = field(default_factory=lambda: list(DEFAULT_CONFIG["subtitle_extensions"]))
    video_extensions: List[str] = field(default_factory=lambda: list(DEFAULT_CONFIG["video_extensions"]))
    min_video_size_mb: int = DEFAULT_CONFIG["min_video_size_mb"]
    theme: str = DEFAULT_CONFIG["theme"]
    ffmpeg_gpu_codec: str = DEFAULT_CONFIG["ffmpeg_gpu_codec"]
    ffmpeg_hardware_accel: str = DEFAULT_CONFIG["ffmpeg_hardware_accel"]
    transcode_crf: int = DEFAULT_CONFIG["transcode_crf"]
    ffmpeg_timeout: int = DEFAULT_CONFIG["ffmpeg_timeout"]
    restored_suffix_marker: str = DEFAULT_CONFIG["restored_suffix_marker"]
    exclude_path_keywords: List[str] = field(default_factory=lambda: list(DEFAULT_CONFIG["exclude_path_keywords"]))
    score_th_keep: int = DEFAULT_CONFIG["score_th_keep"]
    score_th_delete: int = DEFAULT_CONFIG["score_th_delete"]
    enhance_match: bool = DEFAULT_CONFIG["enhance_match"]
    engine: str = DEFAULT_CONFIG["engine"]
    onboarding_done: bool = DEFAULT_CONFIG["onboarding_done"]
    ai_enabled: bool = DEFAULT_CONFIG["ai_enabled"]
    ai_base_url: str = DEFAULT_CONFIG["ai_base_url"]
    ai_model: str = DEFAULT_CONFIG["ai_model"]
    ai_key: str = DEFAULT_CONFIG["ai_key"]
    jellyfin_server: str = DEFAULT_CONFIG["jellyfin_server"]
    jellyfin_api_key: str = DEFAULT_CONFIG["jellyfin_api_key"]
    merge_ffmpeg_path: str = DEFAULT_CONFIG["merge_ffmpeg_path"]
    merge_output_container: str = DEFAULT_CONFIG["merge_output_container"]
    merge_original_subfolder: str = DEFAULT_CONFIG["merge_original_subfolder"]
    merge_backup_root: str = DEFAULT_CONFIG["merge_backup_root"]
    merge_rename_to_number: bool = DEFAULT_CONFIG["merge_rename_to_number"]
    merge_dup_frame_count: int = DEFAULT_CONFIG["merge_dup_frame_count"]
    merge_dup_hash_distance: int = DEFAULT_CONFIG["merge_dup_hash_distance"]
    merge_dup_sim_threshold: float = DEFAULT_CONFIG["merge_dup_sim_threshold"]
    merge_max_parts: int = DEFAULT_CONFIG["merge_max_parts"]
    merge_min_part_seconds: float = DEFAULT_CONFIG["merge_min_part_seconds"]
    merge_timeout_per_file: int = DEFAULT_CONFIG["merge_timeout_per_file"]

    # 非持久化：加载时的自愈提示，供界面显示一次
    load_notes: List[str] = field(default_factory=list, compare=False, repr=False)

    @classmethod
    def load(cls) -> "ToolkitConfig":
        """从 JSON 文件加载配置，不存在或损坏时使用默认值。"""
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    merged = _coerce_config(data)
                    merged, notes = heal_config(merged)
                    cfg = cls(**{k: merged[k] for k in DEFAULT_CONFIG})
                    cfg.load_notes = notes
                    return cfg
            except (json.JSONDecodeError, KeyError, OSError, TypeError, ValueError):
                pass
        return cls()

    def save(self) -> None:
        """原子写入配置：先写临时文件再 os.replace，避免断电/被杀进程留下坏配置。"""
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: v for k, v in asdict(self).items() if k in DEFAULT_CONFIG}
        fd, tmp = tempfile.mkstemp(
            dir=str(CONFIG_FILE.parent), prefix=".config-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, CONFIG_FILE)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def find_ffmpeg() -> str | None:
    """定位 ffmpeg（用户配置 > 程序目录 > PATH > 常见安装位置）。"""
    from utils.tools import find_binary

    return find_binary("ffmpeg")


def doctor() -> dict[str, Any]:
    """环境自检：返回结构化结果，供 CLI ``--doctor`` 与设置页「环境自检」共用。"""
    from utils.tools import probe_hardware, resolve_tool

    ffmpeg = resolve_tool("ffmpeg")
    ffprobe = resolve_tool("ffprobe")
    info: dict[str, Any] = {
        "data_dir": str(DATA_DIR),
        "config_file": str(CONFIG_FILE),
        "portable": portable_mode(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": sys.version.split()[0],
        "ffmpeg": ffmpeg or "",
        "ffprobe": ffprobe or "",
    }
    if ffmpeg:
        info.update(probe_hardware(ffmpeg))
    return info
