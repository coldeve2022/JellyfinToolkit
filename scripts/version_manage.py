"""版本管理：一眼看清当前状态、按规矩提升版本号、发版前做一致性检查。

为什么需要它
------------
版本混乱通常不是因为"忘了改一个数字"，而是因为**同一个事实被写在了多个地方**：
``version.py``、``pyproject.toml``、``CHANGELOG.md``、git 标签，再加一个文件名里的版本号。
只要有一处没跟上，就会出现"界面写 3.8、文件属性写 3.7、Release 叫 3.9"这种事。

所以这里把规则固化成命令：

- ``status`` —— 当前版本、**四处是否一致**、git 状态与标签、产物有没有
- ``bump``   —— 提升版本号：改两处版本号 + 插一段 CHANGELOG 骨架（不自动提交，让人先看一眼）
- ``check``  —— 只做一致性校验；有问题以退出码 1 结束，可直接给 CI / 发版前用

单一事实来源：**``version.py`` 里的 ``__version__``**。其它地方都是从它派生出来的，
``check`` 就是为了确保它们没跑偏。
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

__all__ = [
    "read_version_py", "read_pyproject_version", "read_changelog_versions",
    "check_consistency", "bump_version_py", "bump_pyproject",
    "insert_changelog_entry", "VERSION_RE",
]

VERSION_RE = re.compile(r'__version__\s*=\s*["\']([^"\']+)["\']')
PYPROJECT_RE = re.compile(r'^version\s*=\s*["\']([^"\']+)["\']', re.M)
CHANGELOG_HEAD_RE = re.compile(r"^##\s+\[?(\d+(?:\.\d+)*)\]?", re.M)


def _force_utf8_stdout() -> None:
    """把 stdout 切到 UTF-8（被管道/重定向时）。

    刻意**不 import 项目模块**：以 ``python scripts/xxx.py`` 运行时
    ``sys.path[0]`` 是 ``scripts/`` 而不是仓库根，import 会失败 ——
    上一版就是这么把编码修复静默丢掉、然后在 CI 的 cp1252 上崩掉的。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            if getattr(stream, "isatty", lambda: False)():
                continue
            enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "_")
            if enc in {"utf8", "utf_8", "u8", "cp65001"}:
                continue
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            continue


# ── 读取各处版本号 ──────────────────────────────────────────

def read_version_py(text: str) -> str:
    m = VERSION_RE.search(text or "")
    return m.group(1) if m else ""


def read_pyproject_version(text: str) -> str:
    m = PYPROJECT_RE.search(text or "")
    return m.group(1) if m else ""


def read_changelog_versions(text: str) -> list:
    """按出现顺序列出 CHANGELOG 里的版本号（最新的在最前面）。"""
    return CHANGELOG_HEAD_RE.findall(text or "")


def parse_version(v: str) -> tuple:
    """``"3.8.1"`` → ``(3, 8, 1)``；解析不了返回空元组。"""
    parts = re.findall(r"\d+", str(v or ""))
    return tuple(int(p) for p in parts) if parts else ()


# ── 一致性校验 ──────────────────────────────────────────────

def check_consistency(version_py: str, pyproject: str, changelog_text: str,
                      git_tag: str = "") -> tuple:
    """返回 ``(errors, warnings)``。

    **errors** 必须为 0 才算通过 —— 这几条都会直接导致发版出问题：

    - 两处版本号不一致 → release workflow 的「标签与 version.py 一致性检查」会红；
    - CHANGELOG 里没有当前版本 → Release 正文会是空的（这正是我们刚踩过的坑）。
    """
    errors: list = []
    warnings: list = []

    if not version_py:
        errors.append("version.py 里读不到 __version__")
    if not pyproject:
        errors.append("pyproject.toml 里读不到 version")
    if version_py and pyproject and version_py != pyproject:
        errors.append(f"版本号不一致：version.py={version_py}，pyproject.toml={pyproject}")

    versions = read_changelog_versions(changelog_text)
    if version_py and version_py not in versions:
        errors.append(f"CHANGELOG.md 里没有 {version_py} 这一节（Release 正文会是空的）")

    if version_py and versions:
        cur = parse_version(version_py)
        newest = parse_version(versions[0])
        if cur and newest and cur < newest:
            warnings.append(
                f"CHANGELOG 最新的版本（{versions[0]}）比当前版本（{version_py}）还新")

    if git_tag:
        tag = git_tag.lstrip("vV")
        if tag and version_py and tag != version_py:
            errors.append(f"git 标签（{git_tag}）与 version.py（{version_py}）不一致")
    return errors, warnings


# ── 提升版本号 ──────────────────────────────────────────────

def bump_version_py(text: str, new: str) -> str:
    return VERSION_RE.sub(f'__version__ = "{new}"', text, count=1)


def bump_pyproject(text: str, new: str) -> str:
    return PYPROJECT_RE.sub(f'version = "{new}"', text, count=1)


def insert_changelog_entry(text: str, version: str, date: str, note: str = "",
                           body: str = "") -> str:
    """在 CHANGELOG 顶部（第一个 ``## [`` 之前）插入新版本小节。

    保留文件原有的开头说明；没有现成小节时插到文件末尾。
    """
    entry = [f"## [{version}] — {date}", ""]
    if note:
        entry += [note, ""]
    entry += (body.rstrip().splitlines() if body else ["### 新增", "", "- ", ""])
    block = "\n".join(entry).rstrip() + "\n\n"

    m = CHANGELOG_HEAD_RE.search(text or "")
    if not m:
        return (text or "").rstrip() + "\n\n" + block
    # 回退到该标题所在行首
    start = text.rfind("\n", 0, m.start()) + 1
    return text[:start] + block + text[start:]


# ── git / 产物状态 ──────────────────────────────────────────

def _git(args, cwd) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (r.stdout or "").strip()


def gather_status(root: Path) -> dict:
    """收集当前状态供 ``status`` 子命令展示。"""
    vp = root / "version.py"
    pj = root / "pyproject.toml"
    cl = root / "CHANGELOG.md"

    version = read_version_py(vp.read_text(encoding="utf-8")) if vp.is_file() else ""
    pyproject = read_pyproject_version(pj.read_text(encoding="utf-8")) if pj.is_file() else ""
    changelog = cl.read_text(encoding="utf-8") if cl.is_file() else ""

    dirty = [l for l in _git(["status", "--porcelain"], root).splitlines() if l.strip()]
    tags = [t for t in _git(["tag", "-l"], root).splitlines() if t.strip()]
    head = _git(["rev-parse", "--short", "HEAD"], root)
    branch = _git(["branch", "--show-current"], root)

    dist = root / "dist"
    zips = sorted(dist.glob("*.zip")) if dist.is_dir() else []

    errors, warnings = check_consistency(version, pyproject, changelog)
    return {"version": version, "pyproject": pyproject, "head": head, "branch": branch,
            "dirty": dirty, "tags": tags, "dist_zips": [z.name for z in zips],
            "errors": errors, "warnings": warnings,
            "changelog_versions": read_changelog_versions(changelog)}


def _print_status(st: dict) -> None:
    print("=" * 62)
    print(f"  当前版本   {st['version'] or '(读不到)'}")
    print(f"  分支/提交  {st['branch']} @ {st['head']}")
    print(f"  已打标签   {', '.join(st['tags']) or '(无)'}")
    print(f"  未提交改动 {len(st['dirty'])} 处")
    print(f"  本地产物   {', '.join(st['dist_zips']) or '(dist/ 下没有 zip)'}")
    print(f"  CHANGELOG  {', '.join(st['changelog_versions'][:5]) or '(空)'}")
    print("=" * 62)
    for w in st["warnings"]:
        print(f"  ⚠️  {w}")
    for e in st["errors"]:
        print(f"  ❌ {e}")
    if not st["errors"] and not st["warnings"]:
        print("  ✅ 版本号、CHANGELOG、标签都一致")
    print()
    if st["dirty"]:
        print("  未提交的文件：")
        for l in st["dirty"][:10]:
            print("    " + l)


# ── 命令行 ──────────────────────────────────────────────────

def main() -> int:
    _force_utf8_stdout()
    ap = argparse.ArgumentParser(description="版本管理（状态 / 提升版本 / 一致性检查）")
    ap.add_argument("--root", default=str(ROOT), help="项目根目录")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="显示当前版本状态与一致性问题")
    sub.add_parser("check", help="只做一致性校验，有问题则退出码 1")

    b = sub.add_parser("bump", help="提升版本号并插入 CHANGELOG 骨架（不自动提交）")
    b.add_argument("new_version", help="新版本号，如 3.8.2")
    b.add_argument("--note", default="", help="CHANGELOG 开头的一句话说明")
    b.add_argument("--date", default="", help="日期，默认今天")

    args = ap.parse_args()
    root = Path(args.root)

    if args.cmd == "status":
        _print_status(gather_status(root))
        return 0

    if args.cmd == "check":
        st = gather_status(root)
        for e in st["errors"]:
            print(f"❌ {e}")
        for w in st["warnings"]:
            print(f"⚠️  {w}")
        if st["errors"]:
            return 1
        print("✅ 一致性检查通过")
        return 0

    # bump
    new = args.new_version.strip().lstrip("vV")
    if not parse_version(new):
        print(f"版本号格式不对：{new!r}（示例：3.8.2）")
        return 2
    st = gather_status(root)
    if st["version"] and parse_version(new) <= parse_version(st["version"]):
        print(f"新版本 {new} 必须大于当前版本 {st['version']}")
        return 2

    for rel, fn in (("version.py", bump_version_py), ("pyproject.toml", bump_pyproject)):
        p = root / rel
        t = p.read_text(encoding="utf-8")
        new_t = fn(t, new)
        if new_t == t:
            print(f"  ⚠️  {rel} 没有变化（可能格式和预期不同，请手工确认）")
        else:
            p.write_text(new_t, encoding="utf-8")
            print(f"  ✅ {rel} → {new}")

    cl = root / "CHANGELOG.md"
    t = cl.read_text(encoding="utf-8")
    date = args.date or datetime.date.today().isoformat()
    cl.write_text(insert_changelog_entry(t, new, date, args.note), encoding="utf-8")
    print(f"  ✅ CHANGELOG.md 已插入 [{new}] 小节（请把「- 」那条改成实际改动）")

    print()
    print("  接下来（先写 CHANGELOG，再提交、打标签）：")
    print(f"    1. 编辑 CHANGELOG.md 里的 [{new}] 小节")
    print("    2. git add -A && git commit -m \"v" + new + ": …\"")
    print("    3. git push origin main")
    print(f"    4. git tag -a v{new} -m \"v{new}\" && git push origin v{new}")
    print("    5. 等 Actions 变绿（它会自动构建 + 校验 + 生成 Release）")
    print()
    print("  详见 docs/版本管理与发版流程.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
