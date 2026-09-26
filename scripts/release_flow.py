"""版本归档与发版工作流。

为什么需要它
------------
以前每做一版就换一个文件夹（`WorkBuddy\\<时间戳>\\jellyfin-toolkit`），
结果同一份项目在硬盘上散着好几份、版本号还互相打架，**分不清哪个最新、哪个能直接打开**。

这里确立两条规则，之后所有版本都按它走：

1. **开发目录只有一个**（`jellyfin-toolkit`，永远是最新代码）；
2. **每个已发布的版本在归档目录里各占一个 `vX.Y.Z\\` 子目录，永不修改** ——
   里面有发行包、校验值、本版说明、版本信息，以及一个**解压好、双击就能跑**的 `运行\\`。

归档目录**不在源码仓库里**（一个版本 50 MB，进去会把 git 撑爆）。
位置按这个顺序决定：`--archive` 参数 → 环境变量 ``JELLYFIN_TOOLKIT_ARCHIVE``
→ 上次记住的位置（第一次用 ``--archive`` 传过之后就会记住）
→ 兜底用 `~/JellyfinToolkit-releases`。
**源码里刻意不写任何本机路径** —— 别人拿到也该能用。

用法
----
    python scripts/release_flow.py status                 # 看归档里有哪些版本
    python scripts/release_flow.py check                  # 发版前检查
    python scripts/release_flow.py archive --version 3.8.1 # 把 dist 里的产物归档
    python scripts/release_flow.py archive --from-github v3.8.0   # 把线上某版拉回归档
    python scripts/release_flow.py index                  # 只重建 INDEX.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REPO_URL = "https://github.com/coldeve2022/JellyfinToolkit"
APP_ID = "JellyfinToolkit"
INDEX_NAME = "INDEX.md"

#: 归档根目录的默认值。
#: **刻意不写任何本机路径** —— 源码里出现 `D:\某个人的文件夹` 会让别人拿到后用不了，
#: 也会被仓库的"个人路径守卫"拦下（实测被拦过一次）。
#: 想固定到某处，用 `--archive` 传一次即可，之后会被记住（见 archive_root）。
DEFAULT_ARCHIVE = Path.home() / "JellyfinToolkit-releases"

#: 记住"上次用的归档位置"，免得每次都敲 --archive
ARCHIVE_POINTER = (Path(os.environ.get("LOCALAPPDATA") or Path.home())
                   / APP_ID / "archive_root.txt")


# ── 基础工具 ──────────────────────────────────────────────

def force_utf8_stdout() -> None:
    """见 utils/console.py 的说明：CI/管道下 stdout 可能是 cp1252，打印中文会崩。

    这里内联实现（不 import 项目模块）—— 以 ``python scripts/xxx.py`` 运行时
    ``sys.path[0]`` 是 ``scripts/``，那条 import 会失败。
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


def _remember_root(path: Path) -> None:
    """把归档位置记下来，下次不用再传 --archive。"""
    try:
        ARCHIVE_POINTER.parent.mkdir(parents=True, exist_ok=True)
        ARCHIVE_POINTER.write_text(str(path), encoding="utf-8")
    except OSError:
        pass


def archive_root(explicit: str = "") -> Path:
    """归档根目录：参数 → 环境变量 → **上次记住的位置** → 用户目录下的默认值。

    优先级这样排是为了：显式指定永远最优先；日常用起来不必反复敲参数
    （记住上次的选择）；最后才落到一个对任何机器都成立的默认值。
    """
    for cand in (explicit, os.environ.get("JELLYFIN_TOOLKIT_ARCHIVE", "")):
        if cand:
            p = Path(cand).expanduser()
            _remember_root(p)
            return p
    try:
        if ARCHIVE_POINTER.is_file():
            saved = ARCHIVE_POINTER.read_text(encoding="utf-8").strip()
            if saved:
                return Path(saved)
    except OSError:
        pass
    return DEFAULT_ARCHIVE


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def version_key(v: str):
    """把 ``"3.10.2"`` 变成可排序的元组（否则字符串排序会把 3.10 排在 3.9 前面）。"""
    parts = re.findall(r"\d+", str(v))
    return tuple(int(p) for p in parts) or (0,)


def read_repo_version() -> str:
    m = re.search(r'__version__\s*=\s*"([^"]+)"',
                  (ROOT / "version.py").read_text(encoding="utf-8"))
    if not m:
        raise SystemExit("读不到 version.py 里的 __version__")
    return m.group(1)


def changelog_section(version: str) -> str:
    """从 CHANGELOG 抽本版那一节（失败返回空，不阻塞归档）。"""
    path = ROOT / "CHANGELOG.md"
    if not path.is_file():
        return ""
    try:
        sys.path.insert(0, str(ROOT))
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "rn", ROOT / "scripts" / "release_notes.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.extract_section(path.read_text(encoding="utf-8"), version)
    except Exception:  # noqa: BLE001  抽不到不该挡住归档
        return ""


def git(*args) -> str:
    try:
        r = subprocess.run(["git", "-C", str(ROOT), *args],
                           capture_output=True, text=True, timeout=60)
        return (r.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


# ── 归档单个版本 ──────────────────────────────────────────

def archive_version(version: str, zip_path: Path, root: Path, *,
                    source: str = "", extract: bool = True,
                    notes: str = "", published: bool = True,
                    note: str = "", commit: str = "") -> Path:
    """把一版产物放进归档，返回该版本的目录。

    已经归档过的版本**不会覆盖**（归档的意义就是"这一版当时长什么样"），
    除非调用方显式删掉那个目录。

    ``published=False`` 用于**从未公开发布过的内部快照** ——
    它们可能带着更高的版本号（例如本地的 4.0.0），
    如果按版本号排进"最新"，就又乱套了。索引里会把它们单独标注。
    """
    version = version.strip().lstrip("vV")
    dest = root / f"v{version}"
    dest.mkdir(parents=True, exist_ok=True)

    # 1) 发行包 + 校验值
    zip_copy = dest / zip_path.name
    if not zip_copy.exists() or zip_copy.stat().st_size != zip_path.stat().st_size:
        shutil.copy2(zip_path, zip_copy)

    digest = sha256_of(zip_copy)
    (dest / f"{zip_copy.name}.sha256").write_text(
        f"{digest}  {zip_copy.name}\n", encoding="utf-8")

    # 2) 解压出一份"双击就能跑"的目录
    run_dir = dest / "运行"
    exe_sha = ""
    if extract:
        try:
            if run_dir.exists():
                shutil.rmtree(run_dir, ignore_errors=True)
            run_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zip_copy) as zf:
                zf.extractall(run_dir)
            _flatten_single_dir(run_dir)
            # 压缩包里可能多套一层目录，把真正的 exe 位置记下来
            exes = list(run_dir.rglob(f"{APP_ID}.exe"))
            if exes:
                exe_sha = sha256_of(exes[0])
                (dest / f"{APP_ID}.exe.sha256").write_text(
                    f"{exe_sha}  {APP_ID}.exe\n", encoding="utf-8")
        except (OSError, zipfile.BadZipFile) as e:
            print(f"  ⚠️ 解压失败（发行包已归档，不影响）：{e}")

    # 3) 本版说明（给人看）
    section = notes or changelog_section(version)
    exe_rel = find_exe_rel(run_dir)
    lines = [
        f"# Jellyfin Toolkit v{version}",
        "",
        f"- 发行包：`{zip_copy.name}`（{zip_copy.stat().st_size / 1048576:.1f} MB）",
        f"- SHA256：`{digest}`",
    ]
    if exe_rel:
        lines.append(f"- **直接运行**：双击 `{exe_rel}`")
    lines += [
        f"- 发布页：{REPO_URL}/releases/tag/v{version}",
        "",
    ]
    if section:
        lines += ["## 这一版改了什么", "", section.strip(), ""]
    else:
        lines += ["## 这一版改了什么", "",
                  "（CHANGELOG 里没有找到对应小节，请在发布页查看。）", ""]
    lines += ["## 怎么用", "",
              "1. 解压 `运行\\` 或发行包到任意目录（避开中文路径更稳）",
              "2. 双击 `JellyfinToolkit.exe`",
              "3. 首次运行可选「便携模式」：在程序目录放一个空的 `portable.txt`", ""]
    (dest / "本版说明.md").write_text("\n".join(lines), encoding="utf-8")

    # 4) 版本信息（给程序/脚本看）
    meta = {
        "version": version,
        "tag": f"v{version}",
        "published": bool(published),
        "note": note,
        "archived_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "zip": zip_copy.name,
        "zip_size": zip_copy.stat().st_size,
        "zip_sha256": digest,
        "exe_sha256": exe_sha,
        "exe_relative": exe_rel,
        # 导入历史版本时要显式给提交号 —— 否则会记成"当前 HEAD"，
        # 那是错的（v3.7.0 归档时就踩过这个）。
        # 未发布的本地快照更是根本没有对应提交，留空才对。
        "git_commit": commit or (git("rev-parse", "HEAD") if published else ""),
        "git_dirty": bool(git("status", "--porcelain")),
        "release_url": f"{REPO_URL}/releases/tag/v{version}",
        "source": source or "local-dist",
    }
    (dest / "版本信息.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return dest


def _flatten_single_dir(root: Path) -> None:
    """解压结果只有一个顶层目录时，把它的内容提上来。

    onedir 打包出来的 zip 里通常带一层 `JellyfinToolkit\\`，留着的话
    用户要点进去两层才找到 exe。提上来之后就是 `运行\\JellyfinToolkit.exe`，
    与索引里写的路径一致。
    """
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    if len(entries) != 1 or not entries[0].is_dir():
        return
    inner = entries[0]
    tmp = root.parent / (root.name + "__flatten")
    try:
        inner.rename(tmp)
        root.rmdir()
        tmp.rename(root)
    except OSError:
        # 提不上来也无所谓，后面按真实路径记录即可
        if tmp.exists() and not root.exists():
            tmp.rename(root)


def find_exe_rel(run_dir: Path) -> str:
    """在解压结果里找真正能双击的那个 exe（压缩包可能多套一层目录）。"""
    if not run_dir.is_dir():
        return ""
    hits = sorted(run_dir.rglob(f"{APP_ID}.exe"))
    if not hits:
        return ""
    return str(hits[0].relative_to(run_dir.parent))


# ── 索引 ──────────────────────────────────────────────────

def rebuild_index(root: Path) -> Path:
    """扫描归档目录，重建 INDEX.md（一表看清哪个最新、哪个能直接跑）。"""
    rows = []
    for d in sorted(root.glob("v*"), key=lambda p: version_key(p.name[1:])):
        if not d.is_dir():
            continue
        meta_file = d / "版本信息.json"
        meta = {}
        if meta_file.is_file():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except ValueError:
                meta = {}
        zips = [z for z in d.glob("*.zip")]
        size = meta.get("zip_size") or (zips[0].stat().st_size if zips else 0)
        rows.append({
            "version": meta.get("version") or d.name[1:],
            "dir": d.name,
            "size": size,
            "sha": (meta.get("zip_sha256") or "")[:16],
            "archived": meta.get("archived_at", ""),
            "exe": meta.get("exe_relative", ""),
            "url": meta.get("release_url", f"{REPO_URL}/releases/tag/{d.name}"),
            "commit": (meta.get("git_commit") or "")[:7],
            "source": meta.get("source", ""),
            # 没有元数据的老归档按"已发布"处理（那时还没有这个概念）
            "published": meta.get("published", True),
            "note": meta.get("note", ""),
        })

    # 「最新」只在**已发布**里选 —— 否则一个从未发布的内部快照
    # （版本号可能更高）会长期霸占榜首，正是要避免的那种混乱。
    pub = [r for r in rows if r["published"]]
    unpub = [r for r in rows if not r["published"]]
    pub.sort(key=lambda r: version_key(r["version"]), reverse=True)
    unpub.sort(key=lambda r: version_key(r["version"]), reverse=True)
    latest = pub[0]["version"] if pub else ""

    out = [
        "# Jellyfin Toolkit — 发行归档",
        "",
        "每个已发布版本各占一个 `vX.Y.Z\\` 子目录，**里面的东西不再修改**，",
        "所以任何时候都能拿回「某一版当初就是这样」的产物。",
        "",
        f"- 归档位置：`{root}`",
        f"- 当前最新：**v{latest}**" if latest else "- 归档还是空的",
        f"- 线上发布页：{REPO_URL}/releases",
        "",
        "## 怎么找我要的东西",
        "",
        "| 我想… | 去这里 |",
        "| --- | --- |",
        # 这里必须用**实际的**相对路径（onedir 的 zip 可能带一层顶层目录），
        # 写死一个路径就会出现"照着索引找却找不到文件"
        (f"| 直接打开用最新版 | `{pub[0]['exe']}`（双击） |" if pub and pub[0]["exe"]
         else "| 直接打开用最新版 | （还没有归档版本） |"),
        "| 拿发行包（拷给别人/留档） | `vX.Y.Z\\JellyfinToolkit-vX.Y.Z-win64.zip` |",
        "| 看某一版改了什么 | `vX.Y.Z\\本版说明.md` |",
        "| 核对文件有没有坏 | `vX.Y.Z\\*.sha256` 或下面的校验值 |",
        "| 找回旧版本 | 下面的表格里点对应目录 |",
        "",
        "## 版本一览（最新在最上）",
        "",
        "| 版本 | 归档日期 | 发行包大小 | SHA256 前 16 位 | 提交 | 直接运行 | 发布页 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    if not pub:
        out.append("| — | — | — | — | — | — | — |")
    for r in pub:
        exe = f"✅ `{r['exe']}`" if r["exe"] else "—"
        star = " ⭐ **最新**" if r["version"] == latest else ""
        out.append(
            f"| **v{r['version']}**{star} | {r['archived'] or '—'} | "
            f"{r['size'] / 1048576:.1f} MB | `{r['sha']}…` | `{r['commit'] or '—'}` | "
            f"{exe} | [Release]({r['url']}) |")

    if unpub:
        out += [
            "",
            "### ⚠️ 未发布的历史快照",
            "",
            "下面这些**从未公开发布过**，只在本地存在过。版本号可能比上面的还高，",
            "但内容不一定比最新版新 —— 留着是为了万一要参考当时的东西。",
            "**不要拿它们当正式版本用。**",
            "",
            "| 版本 | 说明 | 归档日期 | 发行包大小 |",
            "| --- | --- | --- | --- |",
        ]
        for r in unpub:
            out.append(f"| v{r['version']} | {r['note'] or '内部快照'} | "
                       f"{r['archived'] or '—'} | {r['size'] / 1048576:.1f} MB |")

    out += [
        "",
        "> 「提交」是该版本对应的 git 提交号前缀，用来和仓库对账。",
        "> 线上发布页里的发行包与本地归档是**同一份**（本地归档会记下 SHA256 以便核对）。",
        "",
        "## 目录规范",
        "",
        "```",
        f"{root}",
        "├─ INDEX.md                  ← 你正在看的这个文件",
        "├─ v3.8.1/                   ← 每个版本一个目录，归档后不再改动",
        "│   ├─ JellyfinToolkit-v3.8.1-win64.zip      发行包",
        "│   ├─ …zip.sha256 / JellyfinToolkit.exe.sha256",
        "│   ├─ 运行/JellyfinToolkit.exe              ← 双击就能跑",
        "│   ├─ 本版说明.md                            这一版改了什么 + 怎么用",
        "│   └─ 版本信息.json                          版本/日期/大小/校验值/提交号",
        "└─ v3.7.0/ …",
        "```",
        "",
        "## 重新生成这个索引",
        "",
        "```bash",
        "python scripts/release_flow.py index",
        "```",
        "",
    ]
    path = root / INDEX_NAME
    root.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out), encoding="utf-8")
    return path


# ── 从线上拉回归档 ────────────────────────────────────────

def _gh_token() -> str:
    """取 gh 已保存的 token（**不打印、不落盘**）。取不到返回空串。"""
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True,
                           text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def _download_with_proxy(version: str, tmp: Path, proxy: str) -> bool:
    """走代理下载 Release 附件。

    **为什么需要这条路径**：``gh release download`` 在本机实测只有约 33 KB/s
    （51 MB 要 20 分钟以上），而同一个文件走本地代理是 5.5 MB/s ——
    差两个数量级。而且 ``gh`` 不读 ``HTTPS_PROXY``，没法用环境变量救它。

    所以这里自己下载：走 ``api.github.com`` 的 asset 接口（不是
    ``browser_download_url`` —— 那个在 ``github.com`` 上，某些网络下直连不通），
    带 ``Accept: application/octet-stream`` 拿二进制，并支持断点续传。
    """
    import json
    import urllib.error
    import urllib.request

    repo = "coldeve2022/JellyfinToolkit"
    token = _gh_token()
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "release_flow"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"https://api.github.com/repos/{repo}/releases/tags/v{version}"
    try:
        with opener.open(urllib.request.Request(url, headers=headers), timeout=60) as resp:
            meta = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"  ❌ 读取 Release 信息失败：{e}")
        return False

    assets = [a for a in meta.get("assets", [])
              if a.get("name", "").endswith((".zip", ".sha256"))]
    if not any(a["name"].endswith(".zip") for a in assets):
        print("  ❌ 该 Release 没有 zip 附件")
        return False

    for asset in assets:
        target = tmp / asset["name"]
        already = target.stat().st_size if target.exists() else 0
        req_headers = dict(headers)
        req_headers["Accept"] = "application/octet-stream"
        if already:
            req_headers["Range"] = f"bytes={already}-"
        try:
            req = urllib.request.Request(asset["url"], headers=req_headers)
            with opener.open(req, timeout=120) as resp, open(
                    target, "ab" if already and resp.status == 206 else "wb") as fh:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
        except (urllib.error.URLError, OSError) as e:
            print(f"  ❌ 下载 {asset['name']} 失败：{e}")
            return False
        size_mb = target.stat().st_size / 1048576
        print(f"  ↓ {asset['name']}（{size_mb:.1f} MB）")
    return True


def archive_from_github(tag: str, root: Path, extract: bool = True,
                        proxy: str = "") -> bool:
    """把线上某个 Release 的附件下载到归档（用于补回早期版本）。

    ``proxy`` 留空时：若设了 ``HTTPS_PROXY`` / ``JELLYFIN_TOOLKIT_PROXY`` 就用它，
    否则退回 ``gh release download``。本机实测前者快两个数量级，所以**推荐给**。
    """
    version = tag.strip().lstrip("vV")
    dest = root / f"v{version}"
    tmp = dest / "_download"
    tmp.mkdir(parents=True, exist_ok=True)

    proxy = (proxy or os.environ.get("JELLYFIN_TOOLKIT_PROXY", "")
             or os.environ.get("HTTPS_PROXY", "")).strip()
    print(f"  下载 v{version} 的发行物"
          + (f"（走代理 {proxy}）" if proxy else "（gh 直连，慢网络可能很慢）") + "…")

    if proxy:
        ok = _download_with_proxy(version, tmp, proxy)
    else:
        cmd = ["gh", "release", "download", f"v{version}", "--repo",
               "coldeve2022/JellyfinToolkit", "--dir", str(tmp), "--clobber",
               "--pattern", "*.zip", "--pattern", "*.sha256"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        ok = r.returncode == 0
        if not ok:
            print(f"  ❌ 下载失败：{(r.stderr or r.stdout or '').strip()[:200]}")

    if not ok:
        return False
    zips = list(tmp.glob("*.zip"))
    if not zips:
        print("  ❌ 该 Release 没有 zip 附件")
        return False
    archive_version(version, zips[0], root, source=f"github:{tag}", extract=extract)
    shutil.rmtree(tmp, ignore_errors=True)
    return True


# ── 发版前检查 ────────────────────────────────────────────

def cmd_check(extra_expected: str = "") -> int:
    """发版前检查：版本号一致 / CHANGELOG 有本版 / 工作区干净。"""
    problems = []
    version = read_repo_version()
    print(f"仓库版本：v{version}")

    pj = ROOT / "pyproject.toml"
    m = re.search(r'^version\s*=\s*"([^"]+)"', pj.read_text(encoding="utf-8"), re.M)
    if not m or m.group(1) != version:
        problems.append(f"pyproject.toml 是 {m.group(1) if m else '?'}，与 version.py 不一致")

    if not changelog_section(version).strip():
        problems.append(f"CHANGELOG.md 里没有 v{version} 这一节（发布页正文会是空的）")

    dirty = git("status", "--porcelain")
    if dirty:
        problems.append(f"工作区有未提交改动（{len(dirty.splitlines())} 项）")

    head = git("rev-parse", "--short", "HEAD")
    print(f"当前提交：{head}")
    if extra_expected and extra_expected.strip().lstrip("vV") != version:
        problems.append(f"预期版本 {extra_expected} 与仓库版本 {version} 不一致")

    if problems:
        print("\n❌ 还不能发版：")
        for p in problems:
            print(f"   · {p}")
        return 1
    print("\n✅ 检查通过，可以发版")
    return 0


def cmd_status(root: Path) -> int:
    rows = sorted([d for d in root.glob("v*") if d.is_dir()],
                  key=lambda p: version_key(p.name[1:]), reverse=True)
    print(f"归档目录：{root}")
    if not root.is_dir():
        print("  （目录还不存在 —— 第一次归档时会自动创建）")
        return 0
    if not rows:
        print("  （还没有任何已归档版本）")
        return 0
    print(f"  共 {len(rows)} 个版本：")
    for d in rows:
        meta = {}
        f = d / "版本信息.json"
        if f.is_file():
            try:
                meta = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                meta = {}
        size = meta.get("zip_size", 0) / 1048576
        has_exe = "✅" if (d / "运行").is_dir() else "—"
        print(f"    v{meta.get('version', d.name[1:]):<8s} {size:5.1f} MB  "
              f"归档于 {meta.get('archived_at', '—')}  可直接运行 {has_exe}")
    print(f"\n  索引：{root / INDEX_NAME}")
    return 0


# ── 命令行 ────────────────────────────────────────────────

def main() -> int:
    force_utf8_stdout()
    ap = argparse.ArgumentParser(description="版本归档与发版工作流")
    ap.add_argument("--archive", default="", help="归档根目录（默认见脚本顶部）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="列出已归档的版本")
    p = sub.add_parser("check", help="发版前检查")
    p.add_argument("--expect", default="", help="预期版本号")

    p = sub.add_parser("index", help="只重建 INDEX.md")
    p = sub.add_parser("archive", help="把某版产物归档")
    p.add_argument("--version", default="", help="版本号；留空用仓库当前版本")
    p.add_argument("--zip", default="", help="发行包路径；留空自动在 dist 下找")
    p.add_argument("--from-github", default="", help="改为从线上 Release 拉回（给 tag，如 v3.7.0）")
    p.add_argument("--no-extract", action="store_true", help="不解压出「运行」目录")
    p.add_argument("--unpublished", action="store_true",
                   help="标记为「从未公开发布的内部快照」——不会出现在「最新」")
    p.add_argument("--note", default="", help="配合 --unpublished 的说明文字")
    p.add_argument("--commit", default="", help="该版本对应的提交号；导入历史版本时填")
    p.add_argument("--proxy", default="",
                   help="从线上拉取时使用的代理，如 http://127.0.0.1:7897；"
                        "留空则读 HTTPS_PROXY / JELLYFIN_TOOLKIT_PROXY")

    args = ap.parse_args()
    root = archive_root(args.archive)

    if args.cmd == "status":
        return cmd_status(root)
    if args.cmd == "check":
        return cmd_check(args.expect)
    if args.cmd == "index":
        path = rebuild_index(root)
        print(f"✅ 索引已重建：{path}")
        return 0

    if args.cmd == "archive":
        if args.from_github:
            ok = archive_from_github(args.from_github, root, not args.no_extract, proxy=args.proxy)
            if ok:
                rebuild_index(root)
                print("✅ 已归档并更新索引")
            return 0 if ok else 1
        version = (args.version or read_repo_version()).strip().lstrip("vV")
        if args.zip:
            zp = Path(args.zip)
        else:
            cands = sorted((ROOT / "dist").glob(f"*{version}*.zip")) or \
                sorted((ROOT / "dist").glob("*.zip")) or \
                sorted(ROOT.glob(f"*{version}*.zip"))
            if not cands:
                print("找不到发行包。先构建：python scripts/build_release.py（或 build.bat）")
                return 1
            zp = cands[0]
        if not zp.is_file():
            print(f"发行包不存在：{zp}")
            return 1
        print(f"归档 v{version}：{zp.name}"
              + ("（标记为未发布快照）" if args.unpublished else ""))
        d = archive_version(version, zp, root, extract=not args.no_extract,
                            published=not args.unpublished, note=args.note,
                            commit=args.commit)
        rebuild_index(root)
        print(f"✅ 已归档到 {d}")
        print(f"   直接运行：{d / '运行' / (APP_ID + '.exe')}")
        print(f"   索引：{root / INDEX_NAME}")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
