"""可移植性与跨机健壮性回归测试。

锁住这次修复的几类"换台机器就废掉"的问题：
- 数据目录解析（平台规范 / 便携模式 / 环境变量覆盖）
- 配置里的失效盘符自愈，备份根目录不再写死 H:/
- 硬件编码/硬解必须**功能探测**，不能看 ffmpeg 的编译期列表
- 编码器参数按厂商分支（QSV/AMF 不认 NVENC 的 -preset p4）
- 配置原子写入
- 单一版本来源
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import config as config_mod
from utils import tools
from version import VERSION_TUPLE, __version__


@pytest.fixture()
def restore_data_root():
    """用完把数据根目录还原。

    必须通过 ``set_data_root`` 还原，而不是直接改 ``config`` 的三个属性 ——
    否则其他模块在重定向期间被同步过的属性会残留在临时目录，
    后续用例会看到"数据目录不一致"的假象。
    """
    base = config_mod.BASE_DIR
    yield
    config_mod.set_data_root(base)


# ── 数据目录解析 ───────────────────────────────────────────
def test_set_data_root_rebinds_imported_modules(tmp_path, restore_data_root):
    """``from config import DATA_DIR`` 是导入期绑定，重定向必须一起换掉。

    这是"测试写仓库"的根因：只改 config.DATA_DIR，其他模块仍指向项目目录。
    """
    from utils import merge_archive
    from workers import scorer

    new_root = tmp_path / "redirected"
    config_mod.set_data_root(new_root)

    assert config_mod.DATA_DIR == new_root / "data"
    assert config_mod.CONFIG_FILE == new_root / "config.json"
    # 模块级属性必须被同步（这两个模块用的是 from config import DATA_DIR）
    assert Path(merge_archive.DATA_DIR) == new_root / "data"
    assert Path(scorer.DATA_DIR) == new_root / "data"
    # 派生路径必须是懒解析的（不能是导入时的常量）
    assert merge_archive.journal_file() == new_root / "data" / "merge_archive_journal.json"
    # 反向证明：还原后必须回到原处，不能残留
    config_mod.set_data_root(config_mod.program_dir())
    assert Path(merge_archive.DATA_DIR) == config_mod.DATA_DIR


def test_env_override_wins(monkeypatch, tmp_path):
    """环境变量优先级最高，且要能被识别为"已覆盖"。

    这里**不能**用 ``importlib.reload(config)`` —— reload 会把模块里的
    BASE_DIR/CONFIG_FILE/DATA_DIR 换成新对象，之后 ``set_data_root()``
    靠对象身份做重绑的机制就失效了，会污染同进程后续所有用例。
    直接测解析函数即可。
    """
    target = tmp_path / "env-root"
    monkeypatch.setenv(config_mod.DATA_DIR_ENV, str(target))
    assert config_mod._resolve_base_dir() == target.resolve()
    assert config_mod.used_configured_data_dir() is True

    monkeypatch.delenv(config_mod.DATA_DIR_ENV, raising=False)
    assert config_mod.used_configured_data_dir() is False
    # 没有环境变量时应回退到源码目录（开发模式）
    assert config_mod._resolve_base_dir() == config_mod.program_dir()


def test_portable_mode_env_toggle(monkeypatch):
    monkeypatch.setenv(config_mod.PORTABLE_ENV, "1")
    assert config_mod.portable_mode() is True
    monkeypatch.delenv(config_mod.PORTABLE_ENV, raising=False)


def test_user_data_root_has_no_hardcoded_drive():
    root = config_mod.user_data_root()
    assert root.is_absolute()
    assert "JellyfinToolkit" in str(root)
    # 不允许是某个盘符根，也不允许是项目目录
    assert root != Path(str(root.anchor))


# ── 配置自愈 / 原子写入 ────────────────────────────────────
def test_heal_config_clears_dead_drive():
    data = {"merge_backup_root": "H:/"}
    healed, notes = config_mod.heal_config(data)
    if sys.platform == "win32" and not Path("H:\\").exists():
        assert healed["merge_backup_root"] == ""
        assert notes, "失效盘符必须给出说明"
    else:
        assert healed["merge_backup_root"] == "H:/"


def test_heal_config_keeps_empty():
    healed, notes = config_mod.heal_config({"merge_backup_root": ""})
    assert healed["merge_backup_root"] == ""
    assert notes == []


def test_defaults_contain_no_drive_letter_or_user_dir():
    """默认值必须能在任何一台干净机器上直接跑通。"""
    raw = json.dumps(config_mod.DEFAULT_CONFIG, ensure_ascii=False)
    for bad in ("H:/", "H:\\", "D:\\", "冷", os.environ.get("USERNAME") or "\x00"):
        if bad == "\x00":
            continue
        assert bad not in raw, f"默认配置里出现了本机专属值: {bad}"
    assert config_mod.DEFAULT_CONFIG["merge_backup_root"] == ""
    assert config_mod.DEFAULT_CONFIG["exclude_path_keywords"] == []
    assert config_mod.DEFAULT_CONFIG["ffmpeg_gpu_codec"] == "", "默认不能假定某家 GPU"
    assert config_mod.DEFAULT_CONFIG["ffmpeg_hardware_accel"] == ""


def test_config_save_is_atomic_and_roundtrips(tmp_path, restore_data_root):
    config_mod.set_data_root(tmp_path)
    cfg = config_mod.ToolkitConfig(jellyfin_db_path="C:/x/jellyfin.db",
                                   merge_backup_root="")
    cfg.save()

    assert config_mod.CONFIG_FILE.exists()
    # 不留临时残渣
    assert not list(tmp_path.glob(".config-*.tmp"))

    loaded = config_mod.ToolkitConfig.load()
    assert loaded.jellyfin_db_path == "C:/x/jellyfin.db"
    # 加载时不允许把内部提示字段写进文件
    saved = json.loads(config_mod.CONFIG_FILE.read_text(encoding="utf-8"))
    assert "load_notes" not in saved


def test_config_load_survives_corrupt_file(tmp_path, restore_data_root):
    config_mod.set_data_root(tmp_path)
    config_mod.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    config_mod.CONFIG_FILE.write_text("{ this is not json", encoding="utf-8")
    cfg = config_mod.ToolkitConfig.load()          # 不应抛异常
    assert cfg.merge_backup_root == ""


def test_config_load_survives_wrong_types(tmp_path, restore_data_root):
    config_mod.set_data_root(tmp_path)
    config_mod.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    config_mod.CONFIG_FILE.write_text(json.dumps({
        "score_th_keep": "not-a-number",
        "enhance_match": "yes",
        "video_extensions": "not-a-list",
    }), encoding="utf-8")
    cfg = config_mod.ToolkitConfig.load()
    assert isinstance(cfg.score_th_keep, int)
    assert isinstance(cfg.enhance_match, bool)
    assert isinstance(cfg.video_extensions, list)


# ── 备份根目录回退 ─────────────────────────────────────────
def test_backup_root_falls_back_to_data_dir(tmp_path):
    from types import SimpleNamespace

    from utils.merge_archive import resolve_backup_root

    cfg = SimpleNamespace(merge_backup_root="")
    resolved = resolve_backup_root(cfg)
    assert resolved.is_absolute()
    assert str(config_mod.DATA_DIR) in str(resolved)
    assert resolved.name == "backups"


def test_backup_root_unusable_drive_falls_back(tmp_path):
    from types import SimpleNamespace

    from config import DATA_DIR
    from utils.merge_archive import resolve_backup_root

    cfg = SimpleNamespace(merge_backup_root="Q:/definitely/not/here")
    resolved = resolve_backup_root(cfg)
    assert str(DATA_DIR) in str(resolved)


def test_backup_root_respects_existing_dir(tmp_path):
    from types import SimpleNamespace

    from utils.merge_archive import resolve_backup_root

    target = tmp_path / "bigdisk"
    target.mkdir()
    resolved = resolve_backup_root(SimpleNamespace(merge_backup_root=str(target)))
    assert resolved == target


# ── 编码器参数按厂商分支 ───────────────────────────────────
@pytest.mark.parametrize("codec,expected", [
    ("libx264", ["-c:v", "libx264", "-preset", "medium", "-crf", "21"]),
    ("h264_nvenc", ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "21"]),
    ("h264_qsv", ["-c:v", "h264_qsv", "-preset", "medium",
                  "-global_quality", "21"]),
    ("h264_amf", ["-c:v", "h264_amf", "-quality", "balanced", "-rc", "cqp",
                  "-qp_i", "21", "-qp_p", "21"]),
])
def test_encoder_args_are_vendor_specific(codec, expected):
    """-preset p4 -cq 是 NVENC 专属；QSV/AMF 会直接解析失败。"""
    assert tools.encoder_args(codec, 21) == expected


def test_encoder_args_unknown_only_sets_codec():
    """不认识的编码器不猜参数，交给 ffmpeg 自己的默认值。"""
    assert tools.encoder_args("some_future_codec", 21) == ["-c:v", "some_future_codec"]


def test_encoder_family_detection():
    assert tools.encoder_family("hevc_nvenc") == "nvenc"
    assert tools.encoder_family("h264_qsv") == "qsv"
    assert tools.encoder_family("h264_amf") == "amf"
    assert tools.encoder_family("libx265") == "libx26x"
    assert tools.encoder_family("whatever") == "unknown"


def test_hwaccel_for_encoder_pairs():
    assert tools.hwaccel_for_encoder("h264_nvenc") == "cuda"
    assert tools.hwaccel_for_encoder("h264_qsv") == "qsv"
    assert tools.hwaccel_for_encoder("h264_amf") == "amf"
    assert tools.hwaccel_for_encoder("libx264") == "none"


# ── 功能探测语义 ───────────────────────────────────────────
def test_probe_encoder_none_and_empty_are_true():
    """"不要硬编"必须成立，且要先于其他判断返回。

    否则没有 ffmpeg 的机器会把"纯 CPU"判成"硬解不可用"，再降级一次并弹误导提示。
    """
    assert tools.probe_encoder("", "none") is True
    assert tools.probe_encoder("", "") is True


def test_probe_hwaccel_none_and_empty_are_true():
    assert tools.probe_hwaccel("", "none") is True
    assert tools.probe_hwaccel("", "") is True
    assert tools.probe_hwaccel("/nonexistent/ffmpeg", "none") is True


def test_probe_encoder_without_tool_is_false():
    assert tools.probe_encoder("", "h264_nvenc") is False


def test_compiled_list_is_not_used_as_ground_truth():
    """核心回归：可用性判定不能退化成读 `-encoders` 输出。

    这里用一条能编译但本机跑不起来的编码器来验证：
    如果哪天有人把 probe_encoder 改回"查编译期列表"，这个用例就会失败。
    """
    ffmpeg = tools.resolve_tool("ffmpeg")
    if not ffmpeg:
        pytest.skip("本机没有 ffmpeg")
    code, out = tools._run([ffmpeg, "-hide_banner", "-encoders"], timeout=30)
    if code != 0:
        pytest.skip("无法读取 -encoders")
    # 注意输出行前面有缩进空格，必须 split 后再判断首字符
    compiled = {parts[1] for parts in (line.split() for line in out.splitlines())
                if len(parts) >= 2 and parts[0][:1] in ("V", "A", "S")}
    compiled_hw = {c for c in compiled if c.endswith(("_nvenc", "_qsv", "_amf", "_vaapi"))}
    if not compiled_hw:
        pytest.skip("该 ffmpeg 未编译任何硬件编码器")

    available = set(tools.available_encoders(ffmpeg))
    functional = {c for c in compiled_hw if tools.probe_encoder(ffmpeg, c)}
    hw_available = {c for c in available
                    if c.endswith(("_nvenc", "_qsv", "_amf", "_vaapi", "_videotoolbox"))}

    # 绝不能报告编译期都不存在的编码器
    assert available <= compiled, f"报告了未编译的编码器: {available - compiled}"
    # 候选里的硬件编码器，每一项都必须真的能编出帧
    assert hw_available <= functional, f"候选里有实测不可用的: {hw_available - functional}"
    # 软件兜底永远要在（没有独显的机器全靠它）
    assert "libx264" in available

    # 关键证据：编译期列表里应当存在"本机跑不起来"的编码器。
    # 旧实现直接读 -encoders，就会把它们全报成"可用"。
    broken = sorted(compiled_hw - functional)
    if not broken:
        pytest.skip("本机编译进去的硬件编码器恰好都实测可用，无法构造误判场景")
    for codec in broken:
        assert tools.probe_encoder(ffmpeg, codec) is False, (
            f"{codec} 在编译期列表里但实测不可用，probe_encoder 却返回 True —— "
            "判定已经退化成读 -encoders 了")


def test_probe_cache_invalidates_on_new_version(tmp_path, restore_data_root):
    """缓存键必须含工具路径+大小+mtime+缓存版本，换 ffmpeg 自动失效。"""
    config_mod.set_data_root(tmp_path)
    fake = tmp_path / "ffmpeg.exe"
    fake.write_bytes(b"x" * 32)
    key_a = tools._cache_key(str(fake), "encoder", "h264_nvenc")
    fake.write_bytes(b"x" * 64)          # 大小变了
    key_b = tools._cache_key(str(fake), "encoder", "h264_nvenc")
    assert key_a != key_b, "换 ffmpeg 后缓存键必须变化"
    assert f"v{tools.CACHE_VERSION}" in key_a
    # 不同编码器之间也不能互相命中
    assert tools._cache_key(str(fake), "encoder", "h264_qsv") != key_b


def test_resolve_encode_settings_falls_back_when_unavailable(monkeypatch):
    """指定了本机没有的编码器 → 回退 libx264 并明确提示，而不是直接失败。"""
    monkeypatch.setattr(tools, "probe_encoder", lambda *a, **k: False)
    monkeypatch.setattr(tools, "probe_hwaccel", lambda *a, **k: False)
    logs: list[str] = []
    codec, accel = tools.resolve_encode_settings(
        "/fake/ffmpeg", "h264_qsv", "qsv", log=logs.append)
    assert codec == "libx264"
    assert accel == "none"
    assert any("回退" in m for m in logs)


def test_resolve_encode_settings_auto_picks_best(monkeypatch):
    monkeypatch.setattr(tools, "best_encoder", lambda _t: "h264_nvenc")
    monkeypatch.setattr(tools, "probe_hwaccel", lambda *a, **k: True)
    codec, accel = tools.resolve_encode_settings("/fake/ffmpeg", "", "")
    assert codec == "h264_nvenc"
    assert accel == "cuda"


# ── 工具定位 ───────────────────────────────────────────────
def test_find_binary_checks_program_dir(tmp_path, monkeypatch):
    """只查 PATH 不够：程序目录/bin 子目录也要能找到。"""
    name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / name
    fake.write_bytes(b"#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setattr(tools, "program_dirs", lambda: [bindir])
    monkeypatch.setattr(tools.shutil, "which", lambda _n: None)
    assert tools.find_binary("ffmpeg") == str(fake)


def test_find_binary_accepts_explicit_file(tmp_path):
    exe = tmp_path / "custom-ffmpeg"
    exe.write_bytes(b"x")
    exe.chmod(0o755)
    assert tools.find_binary("ffmpeg", [str(exe)]) == str(exe)


def test_configure_tools_refreshes_cache(monkeypatch):
    """旧实现是模块级 _FFMPEG = find_binary(...)，import 时固化，
    用户改配置后不生效 —— 这里锁住"改配置要立刻生效"。"""
    from workers import ffmpeg as ffmpeg_worker

    tools.resolve_tool("ffmpeg")                    # 先填充缓存
    tools.configure_tools("D:/some/ffmpeg.exe", force=True)
    assert tools.configured_ffmpeg() == "D:/some/ffmpeg.exe"
    # 缓存必须被清掉（否则还会返回旧的解析结果）
    assert "ffmpeg" not in tools._tool_cache
    # 模块属性走 __getattr__，是动态的而不是固化的旧值
    assert ffmpeg_worker._FFMPEG == tools.resolve_tool("ffmpeg")
    tools.configure_tools("", force=True)


def test_ffmpeg_worker_module_attrs_are_dynamic():
    from workers import ffmpeg as ffmpeg_worker

    # 存在即可（值为 None 也合法：本机没装 ffmpeg 时）
    assert ffmpeg_worker._FFPROBE is None or isinstance(ffmpeg_worker._FFPROBE, str)
    with pytest.raises(AttributeError):
        _ = ffmpeg_worker.not_a_real_attribute


# ── 字体探测 ───────────────────────────────────────────────
def test_font_candidates_are_platform_ordered():
    from utils import fonts

    win = fonts._CANDIDATES["win32"]
    assert win[0].startswith("Microsoft YaHei")
    assert "PingFang SC" in fonts._CANDIDATES["darwin"]
    assert any("Noto" in n for n in fonts._CANDIDATES["linux"])


def test_ui_family_never_returns_empty():
    from utils import fonts

    assert fonts.ui_family()
    assert fonts.mono_family()


def test_fonts_module_safe_before_qapplication():
    """QFontDatabase 必须在 QApplication 之后才能调用，否则 Qt 直接报错。"""
    from utils import fonts

    fonts._cache.clear()
    assert isinstance(fonts._family_set(), set)


# ── 版本单一来源 ───────────────────────────────────────────
def test_version_is_single_source():
    from ui.main_window import MainWindow

    assert MainWindow  # 可导入即说明引用的是 version.py
    assert isinstance(VERSION_TUPLE, tuple) and len(VERSION_TUPLE) == 4
    assert __version__.count(".") == 2


def test_doctor_output_forced_utf8_only_when_piped(monkeypatch):
    """管道/重定向时把输出切到 UTF-8，否则 `--doctor > out.txt` 在 GitHub 上是乱码。

    而 Issue 模板恰恰要求用户粘贴这段输出 —— 所以这不只是美观问题。
    交互式控制台必须保持系统编码，否则中文控制台自己会花屏。
    """
    import main as m

    class Fake:
        def __init__(self):
            self.calls = []

        def isatty(self):
            return False

        def reconfigure(self, **kw):
            self.calls.append(kw)

    out, err = Fake(), Fake()
    monkeypatch.setattr(m.sys, "stdout", out)
    monkeypatch.setattr(m.sys, "stderr", err)
    m._force_utf8_when_piped()
    assert out.calls and out.calls[0]["encoding"] == "utf-8"
    assert err.calls and err.calls[0]["encoding"] == "utf-8"

    class Tty(Fake):
        def isatty(self):
            return True

    tty = Tty()
    monkeypatch.setattr(m.sys, "stdout", tty)
    m._force_utf8_when_piped()
    assert tty.calls == [], "交互式控制台不该被改编码"

    # 窗口版 exe 没有控制台时 stdout/stderr 可能是 None，不能因此抛异常
    monkeypatch.setattr(m.sys, "stdout", None)
    monkeypatch.setattr(m.sys, "stderr", None)
    m._force_utf8_when_piped()


def test_pyproject_version_matches_version_py():
    """pyproject 的版本号必须与 version.py 一致。

    版本号一旦有两处来源就一定会写歪（上一版就漂成了 pyproject 写 3.6.0、
    version.py 写 3.7.0），而 PyPI/GitHub 元数据用的正是 pyproject 那一份。
    """
    import re as _re

    import version

    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = _re.search(r'^version\s*=\s*"([^"]+)"', text, _re.M)
    assert m, "pyproject.toml 里没有 version 字段"
    assert m.group(1) == version.__version__, (
        f"pyproject={m.group(1)} 与 version.py={version.__version__} 不一致")
def test_window_title_contains_version():
    from version import window_title

    assert __version__ in window_title()


def test_version_info_resource_matches_version_py():
    """exe 版本资源由 version.py 生成，两边不能漂移。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import importlib

    make_version_info = importlib.import_module("make_version_info")
    text = make_version_info.render()
    assert __version__ in text
    assert str(VERSION_TUPLE) in text
    assert "LegalCopyright" in text


def test_spec_has_icon_version_and_no_upx():
    spec = (Path(__file__).resolve().parents[1] / "JellyfinToolkit.spec").read_text(
        encoding="utf-8")
    assert "upx=False" in spec, "UPX 压缩会被国产安全软件误报"
    assert "assets" in spec and "icon.ico" in spec
    assert "version_info.txt" in spec
    assert "console=False" in spec


# ── 源码级隐私守卫 ─────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[1]

# 只匹配真正属于本机/个人的痕迹。
# 注意：不要匹配 "喜好画像报表" 这类**通用默认文件名**（profile.py 里只是
# 导出对话框的默认名），否则会误报。
SENSITIVE_PATTERNS = [
    r"C:\\Users\\",
    r"\.workbuddy",
    r"迅雷",
    r"常用jellyfin整理小工具",
]

# 这些是以"个人真实数据"形式存在的文件，绝不允许出现在仓库里
FORBIDDEN_FILES = [
    "config.json",
    "subtitle_state.json",
    "喜好画像报表.csv",
    "data/smart.db",
    "data/trash_journal.db",
]

SKIP_DIRS = {"build", "dist", "demo", "docs", "assets", ".git",
             "__pycache__", ".pytest_cache", ".venv"}

# 本文件自身列举了这些关键词，扫描时要跳过，否则自己举报自己
SELF = "test_portability.py"


def test_no_personal_paths_in_source():
    """源码里不允许出现本机个人路径 —— 这是上传 GitHub 的硬红线。"""
    import re

    offenders: list[str] = []
    for path in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if path.name == SELF:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in SENSITIVE_PATTERNS:
            for match in re.finditer(pattern, text):
                line = text[:match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(ROOT)}:{line}: {pattern}")
    assert not offenders, "源码中发现个人路径残留：\n" + "\n".join(offenders)


def test_no_personal_data_files_present():
    """个人数据文件不得留在工作副本里。"""
    offenders = [name for name in FORBIDDEN_FILES if (ROOT / name).exists()]
    assert not offenders, (
        "仓库里存在个人数据文件，必须删除后再提交：\n" + "\n".join(offenders))


def test_all_python_files_compile():
    """整仓语法自检：改坏文件比漏改文件更糟。"""
    import py_compile

    failures: list[str] = []
    for path in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        try:
            py_compile.compile(str(path), doraise=True, cfile=str(path) + ".pyc-check")
        except py_compile.PyCompileError as e:
            failures.append(f"{path.relative_to(ROOT)}: {e}")
        finally:
            Path(str(path) + ".pyc-check").unlink(missing_ok=True)
    assert not failures, "存在无法编译的文件：\n" + "\n".join(failures)


def test_gitignore_exists_and_covers_personal_data():
    gitignore = ROOT / ".gitignore"
    assert gitignore.exists(), "没有 .gitignore 就等于所有个人数据都会被 git add -A 提交"
    text = gitignore.read_text(encoding="utf-8")
    for must in ("config.json", "data/", ".venv", "dist/", "build/", "__pycache__/"):
        assert must in text, f".gitignore 缺少 {must}"


def test_demo_seeder_produces_neutral_paths(tmp_path):
    """演示数据必须落在中性路径，否则 README 截图会泄露构建环境。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "seed_demo", ROOT / "tools" / "dev" / "seed_demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    db = module.seed(tmp_path / "demo", 12)
    assert db.exists()
    assert "MediaDemo" in module.DEMO_ROOT
    assert os.environ.get("USERNAME", "\x00") not in module.DEMO_ROOT
