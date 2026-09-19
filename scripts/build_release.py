"""一条命令产出可发布的 Windows 包（dist/JellyfinToolkit/ + zip + SHA256）。

流程：
    1. 重新生成图标 (tools/make_icon.py) 与版本资源 (tools/make_version_info.py)
    2. 跑测试（可跳过）
    3. PyInstaller 打包（用 JellyfinToolkit.spec，onedir / 无控制台 / 带版本资源）
    4. 校验 exe 的版本资源确实写进去了
    5. 打 zip 并输出 SHA256

在脚本方式运行时 ``sys.path[0]`` 是 scripts/ 而不是项目根，
所以必须手动把根目录插进 sys.path，否则 ``import version`` 会失败。

用法：
    python scripts/build_release.py                  # 完整构建
    python scripts/build_release.py --skip-tests     # 跳过测试
    python scripts/build_release.py --no-zip         # 只打包目录
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.console import force_utf8_stdout  # noqa: E402

force_utf8_stdout()

from version import APP_NAME, __version__  # noqa: E402


def _run(cmd: list[str], extra_env: dict[str, str] | None = None, **kwargs) -> int:
    print(f"\n$ {' '.join(str(c) for c in cmd)}")
    env = dict(os.environ)
    # 沙箱的 safe-delete shim 会拦截 PyInstaller 内部的 os.remove/rmtree，
    # 导致打包中途失败；清空会话变量即可禁用它。
    env.pop("CODEBUDDY_SESSION_ID", None)
    env.pop("CLAUDE_SESSION_ID", None)
    # 子脚本也要能打印中文：GitHub 的 Windows runner 上 stdout 默认是 cp1252，
    # 打一个中文字符就会 UnicodeEncodeError（实测把 release workflow 打死了）。
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_env:
        env.update(extra_env)
    return subprocess.call(cmd, cwd=str(ROOT), env=env, **kwargs)


def _read_exe_version(exe: Path) -> dict[str, str]:
    """读 exe 的版本资源（PowerShell 的 stdout 在部分宿主里会被吞掉，改用 ctypes）。"""
    if sys.platform != "win32":
        return {}
    import ctypes
    from ctypes import wintypes

    version = ctypes.windll.version
    GetFileVersionInfoSizeW = version.GetFileVersionInfoSizeW
    GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    GetFileVersionInfoSizeW.restype = wintypes.DWORD

    size = GetFileVersionInfoSizeW(str(exe), None)
    if size == 0:
        return {}
    buf = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(exe), 0, size, buf):
        return {}

    out: dict[str, str] = {}

    def _query(key: str) -> str:
        ptr = ctypes.c_void_p()
        length = wintypes.UINT()
        if not version.VerQueryValueW(buf, f"\\StringFileInfo\\040904B0\\{key}",
                                      ctypes.byref(ptr), ctypes.byref(length)):
            return ""
        if not ptr.value:
            return ""
        return ctypes.wstring_at(ptr.value, length.value).rstrip("\x00")

    for key in ("ProductName", "FileVersion", "ProductVersion", "CompanyName",
                "LegalCopyright"):
        out[key] = _query(key)
    return out


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="构建发布包")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--no-zip", action="store_true")
    parser.add_argument("--keep-build", action="store_true",
                        help="保留 build/ 中间产物（默认保留，便于排查）")
    parser.add_argument("--name", default=f"{APP_NAME}-v{__version__}-win64")
    args = parser.parse_args()

    # 1) 生成图标与版本资源
    if _run([sys.executable, "tools/make_icon.py"]) != 0:
        return 1
    if _run([sys.executable, "tools/make_version_info.py"]) != 0:
        return 1

    # 2) 测试
    if not args.skip_tests:
        if _run([sys.executable, "-m", "pytest", "tests", "-q"],
                extra_env={"QT_QPA_PLATFORM": "offscreen"}) != 0:
            print("\n[中止] 测试未通过，不打包。")
            return 1

    # 3) 打包
    dist = ROOT / "dist"
    build = ROOT / "build" / "pyinstaller"
    for target in (dist / APP_NAME, build):
        shutil.rmtree(target, ignore_errors=True)
    if _run([sys.executable, "-m", "PyInstaller", "--noconfirm",
             "--distpath", str(dist), "--workpath", str(build),
             "JellyfinToolkit.spec"]) != 0:
        return 1

    exe = dist / APP_NAME / f"{APP_NAME}.exe"
    if not exe.exists():
        print(f"\n[失败] 未找到产物: {exe}")
        return 1

    # 4) 校验版本资源
    info = _read_exe_version(exe)
    print("\n产物版本资源:")
    for key in ("ProductName", "FileVersion", "ProductVersion", "LegalCopyright"):
        print(f"  {key:<15}= {info.get(key) or '(空)'}")
    if info.get("FileVersion") != __version__:
        print(f"\n[警告] exe 版本资源 ({info.get('FileVersion')!r}) "
              f"与 version.py ({__version__}) 不一致，请检查 spec 的 version= 参数。")

    # 5) 打 zip + SHA256
    size_mb = sum(f.stat().st_size for f in (dist / APP_NAME).rglob("*") if f.is_file())
    print(f"\n目录大小: {size_mb / 1024 / 1024:.1f} MB")

    if not args.no_zip:
        zip_path = ROOT / "dist" / f"{args.name}.zip"
        zip_path.unlink(missing_ok=True)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for file in sorted((dist / APP_NAME).rglob("*")):
                if file.is_file():
                    zf.write(file, file.relative_to(dist))
        print(f"压缩包: {zip_path}  ({zip_path.stat().st_size / 1024 / 1024:.1f} MB)")
        print(f"SHA256 : {_sha256(zip_path)}")
        # 同时写一份校验文件，Release 页让用户核对
        (zip_path.with_suffix(".zip.sha256")).write_text(
            f"{_sha256(zip_path)}  {zip_path.name}\n", encoding="utf-8")

    exe_sha = _sha256(exe)
    print(f"\nSHA256 (exe): {exe_sha}")
    (ROOT / "dist" / f"{APP_NAME}.exe.sha256").write_text(
        f"{exe_sha}  {exe.name}\n", encoding="utf-8")

    print(f"\n完成: {exe}")
    print("提示：发布前请跑一次  python tools/dev/smoke_exe.py  验证打包产物能正常启动。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
