"""外部工具定位 + 硬件能力「功能探测」。

为什么不能看 `ffmpeg -encoders` / `-hwaccels`：
    这两条命令列出的是**这份 ffmpeg 构建时编译进去的东西**，
    与**本机有没有对应硬件**完全无关。官方构建会同时列出
    h264_nvenc / h264_qsv / h264_amf，但在没有 Intel 核显 / AMD 显卡的机器上，
    选 qsv 或 amf 会直接报 "Error creating a MFX session" / "amfrt64.dll failed to open"，
    用户会以为软件坏了。

所以这里一律**真跑一次**：拿 3 帧 320x240 的黑场去编/去解，
能出结果才算可用。判定结果带缓存（键含工具路径 + 大小 + mtime + 缓存版本），
换 ffmpeg 版本会自动失效。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

CACHE_VERSION = 3

# 探测用测试源：尺寸不能太小 —— NVENC 对 <145x49 左右的帧会以
# "Frame Dimension less than the minimum supported dimension" 拒绝，造成假阴性。
_PROBE_SRC = "color=c=black:s=320x240:d=0.2"
_PROBE_FRAMES = "3"

# 候选编码器（按「优 → 劣」排列，用于自动挑选）
ENCODER_CANDIDATES = (
    "hevc_nvenc", "h264_nvenc",
    "hevc_qsv", "h264_qsv",
    "hevc_amf", "h264_amf",
    "hevc_videotoolbox", "h264_videotoolbox",
    "hevc_vaapi", "h264_vaapi",
    "libx265", "libx264",
)

# 候选硬解方式（按平台常见顺序排列）
HWACCEL_CANDIDATES = (
    "cuda", "qsv", "amf", "d3d11va", "dxva2", "d3d12va",
    "videotoolbox", "vaapi", "none",
)

_KNOWN_ENCODERS = set(ENCODER_CANDIDATES)
_KNOWN_HWACCELS = set(HWACCEL_CANDIDATES)

_COMMON_UNIX_BIN_DIRS = ("/usr/bin", "/usr/local/bin", "/opt/homebrew/bin", "/snap/bin")
_WIN_HINT_DIRS = (
    r"C:\ffmpeg\bin",
    r"C:\Program Files\ffmpeg\bin",
    r"C:\Program Files (x86)\ffmpeg\bin",
)


def _creationflags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def program_dirs() -> list[Path]:
    """程序目录（含打包后 exe 所在目录）以及常见的同目录工具子目录。"""
    if getattr(sys, "frozen", False):
        root = Path(sys.executable).resolve().parent
    else:
        root = Path(__file__).resolve().parent.parent
    dirs = [root, root / "bin", root / "ffmpeg", root / "tools" / "ffmpeg"]
    return dirs


def _is_executable(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def _exe_names(name: str) -> list[str]:
    if sys.platform == "win32":
        return [f"{name}.exe", name]
    return [name]


def find_binary(name: str, extra_dirs: Iterable[Path | str] = ()) -> Optional[str]:
    """多路兜底定位外部可执行文件。

    顺序：``extra_dirs``（用户配置）> 程序目录及内置子目录 > PATH > 各平台常见安装位置。
    """
    candidates: list[Path] = []
    for raw in extra_dirs:
        if not raw:
            continue
        p = Path(str(raw))
        if p.is_dir():
            candidates.extend(p / exe for exe in _exe_names(name))
        else:
            candidates.append(p)   # 允许直接给可执行文件路径

    for directory in program_dirs():
        candidates.extend(directory / exe for exe in _exe_names(name))

    which = shutil.which(name) or shutil.which(f"{name}.exe")
    if which:
        candidates.append(Path(which))

    if sys.platform == "win32":
        candidates.extend(Path(d) / f"{name}.exe" for d in _WIN_HINT_DIRS)
    else:
        candidates.extend(Path(d) / name for d in _COMMON_UNIX_BIN_DIRS)

    for candidate in candidates:
        if _is_executable(candidate):
            return str(candidate)
    return None


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    """执行命令，永不抛异常；返回 (returncode, 合并输出)。"""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            creationflags=_creationflags(), timeout=timeout,
        )
        return proc.returncode, _text(proc.stdout) + _text(proc.stderr)
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except (OSError, ValueError):
        return -1, "spawn-failed"


# ── 缓存 ───────────────────────────────────────────
def _cache_file() -> Path:
    try:
        import config
        base = Path(config.DATA_DIR)
    except Exception:  # noqa: BLE001 — 缓存失败不应影响功能
        base = Path(tempfile.gettempdir())
    return base / "tool_probe_cache.json"


def _cache_key(tool: str, kind: str, label: str = "") -> str:
    """缓存键 = 缓存版本 + 类别 + 工具路径 + 大小 + mtime + 细分标签。

    带大小/mtime → 换 ffmpeg 版本自动失效；带 label → nvenc/qsv/amf 各自独立。
    """
    try:
        st = os.stat(tool)
        stamp = f"{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        stamp = "missing"
    return f"v{CACHE_VERSION}|{kind}|{tool}|{stamp}|{label}"


def _cache_read() -> dict:
    path = _cache_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _cache_write(data: dict) -> None:
    path = _cache_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def clear_probe_cache() -> None:
    """清空探测缓存（换显卡/换 ffmpeg 后强制重探）。"""
    _cache_write({})


# ── 功能探测 ───────────────────────────────────────
def _compiled_encoders(tool: str) -> set[str]:
    """编译期编码器列表 —— 仅在 ffmpeg 精简到没有 lavfi 时作兜底。"""
    code, out = _run([tool, "-hide_banner", "-encoders"], timeout=30)
    if code != 0:
        return set()
    found: set[str] = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0][:1] in ("V", "A", "S"):
            found.add(parts[1])
    return found


def probe_encoder(tool: str, codec: str, *, use_cache: bool = True) -> bool:
    """真跑一次，确认 ``codec`` 在本机能编出帧。"""
    # 顺序很重要：none/"" 表示"不要硬编"，任何机器上都必须成立，
    # 必须先于"没有 ffmpeg 就返回 False"。否则纯 CPU 机器会被判成"硬编不可用"，
    # 再降级一次并弹出误导提示。
    if codec in ("none", ""):
        return True
    if not tool:
        return False
    if codec == "libx264":
        return True          # ffmpeg 官方构建必带（且旧行为如此，保持兼容）

    cache = _cache_read() if use_cache else {}
    key = _cache_key(tool, "encoder", codec)
    if key in cache:
        return bool(cache[key])

    cmd = [
        tool, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", _PROBE_SRC,
        "-frames:v", _PROBE_FRAMES,
        "-c:v", codec, "-f", "null", "-",
    ]
    code, out = _run(cmd, timeout=90)
    if code == 0:
        result = True
    elif "lavfi" in out.lower() or "unknown input format" in out.lower():
        # 极简构建没有 lavfi → 真跑必然失败，退回编译期列表，避免把所有硬件编码器误判为不可用
        result = codec in _compiled_encoders(tool)
    else:
        result = False

    if use_cache:
        cache[key] = result
        _cache_write(cache)
    return result


def probe_hwaccel(tool: str, accel: str, *, use_cache: bool = True) -> bool:
    """真跑一次，确认 ``accel`` 在本机能建立解码设备。"""
    if accel in ("none", ""):
        return True          # "不要硬解" 永远成立，先于工具存在性判断
    if not tool:
        return False

    cache = _cache_read() if use_cache else {}
    key = _cache_key(tool, "hwaccel", accel)
    if key in cache:
        return bool(cache[key])



    cmd = [
        tool, "-hide_banner", "-loglevel", "error",
        "-hwaccel", accel,
        "-f", "lavfi", "-i", _PROBE_SRC,
        "-frames:v", "1",
        "-c:v", "libx264", "-f", "null", "-",
    ]
    code, out = _run(cmd, timeout=90)
    if code == 0:
        result = True
    elif "lavfi" in out.lower() or "unknown input format" in out.lower():
        result = accel in _compiled_hwaccels(tool)
    else:
        result = False

    if use_cache:
        cache[key] = result
        _cache_write(cache)
    return result


def _compiled_hwaccels(tool: str) -> set[str]:
    code, out = _run([tool, "-hide_banner", "-hwaccels"], timeout=30)
    if code != 0:
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}


def available_encoders(tool: str, *, use_cache: bool = True) -> list[str]:
    """过滤出本机**真正可用**的候选编码器（按优先级排列）。"""
    return [c for c in ENCODER_CANDIDATES if probe_encoder(tool, c, use_cache=use_cache)]


def available_hwaccels(tool: str, *, use_cache: bool = True) -> list[str]:
    """过滤出本机**真正可用**的硬解方式。"""
    return [a for a in HWACCEL_CANDIDATES if probe_hwaccel(tool, a, use_cache=use_cache)]


def best_encoder(tool: str) -> str:
    """挑一个本机可用且最合适的编码器；全不可用则退回 libx264。"""
    for codec in available_encoders(tool):
        return codec
    return "libx264"


def best_hwaccel(tool: str) -> str:
    """挑一个本机可用的硬解方式；无可用则 "none"。"""
    for accel in available_hwaccels(tool):
        if accel != "none":
            return accel
    return "none"


def hwaccel_for_encoder(codec: str) -> str:
    """按编码器家族给出配套的硬解方式（仅作候选，仍需实测）。"""
    if codec.endswith("_nvenc"):
        return "cuda"
    if codec.endswith("_qsv"):
        return "qsv"
    if codec.endswith("_amf"):
        return "amf"
    if codec.endswith("_videotoolbox"):
        return "videotoolbox"
    if codec.endswith("_vaapi"):
        return "vaapi"
    return "none"


def encoder_family(codec: str) -> str:
    """把编码器名归到参数家族：libx264 / nvenc / qsv / amf / 其他。"""
    if codec in ("libx264", "libx265"):
        return "libx26x"
    if codec.endswith("_nvenc"):
        return "nvenc"
    if codec.endswith("_qsv"):
        return "qsv"
    if codec.endswith("_amf"):
        return "amf"
    return "unknown"


def encoder_args(codec: str, quality: Optional[int] = None) -> list[str]:
    """按**厂商**生成编码参数。

    各家的质量参数完全不通用：``-preset p4 -cq N`` 是 NVENC 专属，
    Intel QSV 会直接报 ``Unable to parse "preset" option value "p4"``，
    AMD AMF 也不认。未知编码器只给 ``-c:v``，让 ffmpeg 用自己的默认值 —— 不猜。
    """
    codec = (codec or "libx264").strip()
    family = encoder_family(codec)
    q = quality if quality is not None else 23
    if family == "libx26x":
        return ["-c:v", codec, "-preset", "medium", "-crf", str(q)]
    if family == "nvenc":
        return ["-c:v", codec, "-preset", "p4", "-cq", str(q)]
    if family == "qsv":
        return ["-c:v", codec, "-preset", "medium", "-global_quality", str(q)]
    if family == "amf":
        return ["-c:v", codec, "-quality", "balanced", "-rc", "cqp",
                "-qp_i", str(q), "-qp_p", str(q)]
    return ["-c:v", codec]


def resolve_encode_settings(
    ffmpeg: str,
    codec: str,
    accel: str,
    log: Optional[Callable[[str], None]] = None,
) -> tuple[str, str]:
    """把配置里的编码器/硬解解析成本机**实测可用**的组合。

    - ``codec`` 为空或 "auto" → 自动挑最优可用编码器；
    - 指定了但本机不可用 → 回退 libx264 并提示；
    - 硬解为空或 "auto" → 跟随编码器家族，并实测；不可用退 none。
    """
    def _log(message: str) -> None:
        if log:
            log(message)

    requested = (codec or "").strip()
    if requested in ("", "auto"):
        resolved = best_encoder(ffmpeg) if ffmpeg else "libx264"
        if resolved != "libx264":
            _log(f"🔍 自动探测到可用硬件编码器: {resolved}")
    elif probe_encoder(ffmpeg, requested):
        resolved = requested
    else:
        _log(f"⚠️ 编码器 {requested} 在本机不可用（无对应 GPU 或驱动），"
             f"已自动回退为 CPU 编码 libx264")
        resolved = "libx264"

    wanted_accel = (accel or "").strip()
    if wanted_accel in ("", "auto"):
        candidate = hwaccel_for_encoder(resolved)
    else:
        candidate = wanted_accel

    if candidate in ("none", ""):
        return resolved, "none"
    if probe_hwaccel(ffmpeg, candidate):
        return resolved, candidate
    if wanted_accel not in ("", "auto"):
        _log(f"⚠️ 硬解 {wanted_accel} 在本机不可用，已关闭硬件解码")
    return resolved, "none"


def probe_hardware(ffmpeg: str) -> dict:
    """汇总本机实测结果，供启动自检/``--doctor``/设置页展示。"""
    if not ffmpeg:
        return {"encoders": [], "hwaccels": [], "recommended_encoder": "",
                "recommended_hwaccel": "", "note": "未找到 ffmpeg"}
    encoders = available_encoders(ffmpeg)
    hwaccels = available_hwaccels(ffmpeg)
    return {
        "encoders": encoders,
        "hwaccels": hwaccels,
        "recommended_encoder": encoders[0] if encoders else "libx264",
        "recommended_hwaccel": next((a for a in hwaccels if a != "none"), "none"),
        "note": "",
    }


# ── 工具解析（带进程内缓存，配置变更后可刷新）────────────────
_tool_cache: dict[str, str] = {}
_configured: dict[str, str] = {}
_configured_stamp: float = 0.0


def configure_tools(ffmpeg_path: str = "", *, force: bool = False) -> None:
    """注入用户配置的 ffmpeg 路径并让缓存失效。

    模块级 ``_FFMPEG = find_binary(...)`` 会在 import 时固化，
    用户之后改了配置也不生效 —— 所以统一走这里。
    """
    global _configured_stamp
    ffmpeg_path = (ffmpeg_path or "").strip()
    if ffmpeg_path == _configured.get("ffmpeg", "") and not force:
        return
    _configured["ffmpeg"] = ffmpeg_path
    _configured_stamp = time.time()
    _tool_cache.clear()


def configured_ffmpeg() -> str:
    """当前生效的用户配置 ffmpeg 路径（可能为空）。"""
    return _configured.get("ffmpeg", "")


def resolve_tool(name: str, *, force: bool = False) -> Optional[str]:
    """定位 ffmpeg / ffprobe：用户配置优先，其次程序目录/PATH/常见位置。"""
    if not force and name in _tool_cache:
        return _tool_cache[name] or None

    extra: list[str] = []
    if name in ("ffmpeg", "ffprobe"):
        cfg_path = configured_ffmpeg()
        if cfg_path:
            p = Path(cfg_path)
            if name == "ffmpeg":
                extra.append(cfg_path)
            elif p.name.lower().startswith("ffmpeg") and p.parent.exists():
                sibling = p.with_name("ffprobe.exe" if sys.platform == "win32" else "ffprobe")
                extra.append(str(sibling))
    found = find_binary(name, extra) or ""
    _tool_cache[name] = found
    return found or None
