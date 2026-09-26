"""版本归档工具的单元测试。

这个工具的价值在于**归档出去的东西以后还能被信任**，所以测试重点是：
版本号排序不能错、索引里给出的路径必须真实存在、未发布快照不能冒充最新。
"""

import importlib.util
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load():
    """按路径加载 scripts/release_flow.py（scripts/ 是纯脚本目录，不放 __init__.py）。"""
    spec = importlib.util.spec_from_file_location(
        "release_flow_probe", ROOT / "scripts" / "release_flow.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rf = _load()


def _make_zip(path: Path, *, nested: bool = True) -> Path:
    """造一个像 onedir 产物的 zip（里含一层顶层目录 + 一个假 exe）。"""
    with zipfile.ZipFile(path, "w") as zf:
        base = "JellyfinToolkit/" if nested else ""
        zf.writestr(base + "JellyfinToolkit.exe", b"MZ" + b"0" * 64)
        zf.writestr(base + "_internal/data.bin", b"x" * 32)
    return path


# ── 版本号排序 ──────────────────────────────────────────────

def test_version_key_sorts_numerically_not_lexically():
    """字符串排序会把 3.10 排在 3.9 前面，那会让索引的"最新"标错。"""
    assert rf.version_key("3.10.0") > rf.version_key("3.9.0")
    assert rf.version_key("3.8.10") > rf.version_key("3.8.9")
    assert rf.version_key("4.0.0") > rf.version_key("3.99.99")
    assert sorted(["3.9.0", "3.10.0", "3.8.1"], key=rf.version_key) == \
        ["3.8.1", "3.9.0", "3.10.0"]


# ── 解压后把多余的一层目录提上来 ────────────────────────────

def test_flatten_promotes_single_top_level_dir(tmp_path):
    """onedir 的 zip 常带一层 JellyfinToolkit\\，不提上来用户要多点两层。"""
    root = tmp_path / "运行"
    (root / "JellyfinToolkit").mkdir(parents=True)
    (root / "JellyfinToolkit" / "JellyfinToolkit.exe").write_bytes(b"MZ")
    (root / "JellyfinToolkit" / "_internal").mkdir()

    rf._flatten_single_dir(root)

    assert (root / "JellyfinToolkit.exe").is_file()
    assert (root / "_internal").is_dir()
    assert not (root / "JellyfinToolkit").exists()


def test_flatten_leaves_multiple_entries_alone(tmp_path):
    root = tmp_path / "运行"
    root.mkdir()
    (root / "a.txt").write_text("x")
    (root / "b").mkdir()
    rf._flatten_single_dir(root)
    assert (root / "a.txt").is_file() and (root / "b").is_dir()


# ── 归档一个版本 ────────────────────────────────────────────

def test_archive_produces_expected_layout(tmp_path):
    zp = _make_zip(tmp_path / "JellyfinToolkit-v9.9.9-win64.zip")
    root = tmp_path / "archive"

    dest = rf.archive_version("9.9.9", zp, root, notes="### 新增\n\n- 测试项")

    assert dest == root / "v9.9.9"
    for name in ("JellyfinToolkit-v9.9.9-win64.zip",
                 "JellyfinToolkit-v9.9.9-win64.zip.sha256",
                 "本版说明.md", "版本信息.json"):
        assert (dest / name).is_file(), name

    # 解压出的可运行副本：exe 必须落在 运行\ 根下（真实可双击）
    meta = json.loads((dest / "版本信息.json").read_text(encoding="utf-8"))
    assert (dest / meta["exe_relative"]).is_file()
    assert meta["exe_relative"] == str(Path("运行") / "JellyfinToolkit.exe")

    # 校验值必须与文件对得上
    assert rf.sha256_of(dest / zp.name) == meta["zip_sha256"]
    assert meta["zip_sha256"] in (dest / f"{zp.name}.sha256").read_text(encoding="utf-8")

    # 说明里带上本版内容与"怎么用"
    notes = (dest / "本版说明.md").read_text(encoding="utf-8")
    assert "测试项" in notes and "双击" in notes


def test_archive_does_not_overwrite_existing_zip(tmp_path):
    """归档的意义就是"当时就是这样"，重复归档不该改掉已有产物。"""
    zp = _make_zip(tmp_path / "JellyfinToolkit-v1.0.0-win64.zip")
    root = tmp_path / "archive"
    dest = rf.archive_version("1.0.0", zp, root, extract=False)
    marker = dest / zp.name
    before = marker.stat().st_mtime_ns

    rf.archive_version("1.0.0", zp, root, extract=False)
    assert marker.stat().st_mtime_ns == before


def test_unpublished_snapshot_has_no_commit(tmp_path):
    """未发布的本地快照不该显示提交号 —— 那会让索引骗人。"""
    zp = _make_zip(tmp_path / "JellyfinToolkit-v2.0.0-win64.zip")
    root = tmp_path / "archive"
    dest = rf.archive_version("2.0.0", zp, root, extract=False,
                              published=False, note="内部快照")
    meta = json.loads((dest / "版本信息.json").read_text(encoding="utf-8"))
    assert meta["published"] is False
    assert meta["git_commit"] == ""
    assert meta["note"] == "内部快照"


# ── 索引 ────────────────────────────────────────────────────

def test_index_marks_latest_and_keeps_unpublished_apart(tmp_path):
    """核心诉求：版本号更高的"未发布快照"不能冒充最新版。"""
    root = tmp_path / "archive"
    for v in ("3.7.0", "3.8.1"):
        rf.archive_version(v, _make_zip(tmp_path / f"z{v}.zip"), root, extract=False)
    # 一个版本号更高、但从未发布的内部快照
    rf.archive_version("4.0.0", _make_zip(tmp_path / "z400.zip"), root,
                       extract=False, published=False, note="内部首发快照")

    text = rf.rebuild_index(root).read_text(encoding="utf-8")

    assert "当前最新：**v3.8.1**" in text
    assert "v4.0.0" in text
    # v4.0.0 必须出现在"未发布"区，且不能带"最新"标记
    pub_part, _, unpub_part = text.partition("未发布的历史快照")
    assert "v4.0.0" not in pub_part.split("## 版本一览")[-1].split("### ⚠️")[0]
    assert "v4.0.0" in unpub_part
    assert "内部首发快照" in unpub_part


def test_index_gives_a_path_that_actually_exists(tmp_path):
    """索引写的「直接打开用最新版」路径必须真实存在 —— 否则用户照着找会扑空。

    （真实踩过：路径少写了一层 `JellyfinToolkit\\`。）
    """
    root = tmp_path / "archive"
    rf.archive_version("3.8.1", _make_zip(tmp_path / "z.zip"), root)
    text = rf.rebuild_index(root).read_text(encoding="utf-8")

    # 把「直接打开用最新版」那一行里的反引号内容提取出来验证
    line = next(l for l in text.splitlines() if "直接打开用最新版" in l and "`" in l)
    rel = line.split("`")[1]
    assert (root / "v3.8.1" / rel).is_file(), f"索引给的路径不存在：{rel}"


def test_index_on_empty_archive_is_safe(tmp_path):
    root = tmp_path / "empty"
    text = rf.rebuild_index(root).read_text(encoding="utf-8")
    assert "归档还是空的" in text

def test_archive_from_github_fails_gracefully_with_bad_proxy(tmp_path):
    """代理不可用时要返回 False 并说明，而不是抛异常把整个流程打断。

    顺便固定"给 --proxy 就走自建下载路径"这个契约 —— 走这条路的原由是实测
    ``gh release download`` 只有约 33 KB/s、而走本地代理 5.5 MB/s，
    差两个数量级（而且 gh 不读 HTTPS_PROXY，没法用环境变量救它）。
    """
    mod = _load()
    # 指向一个必然连不上的端口
    ok = mod.archive_from_github("v3.8.1", tmp_path, extract=False,
                                 proxy="http://127.0.0.1:1")
    assert ok is False
    assert not list(tmp_path.glob("v3.8.1/*.zip")), "失败时不该留下半截产物"


def test_download_treats_416_as_already_complete(tmp_path, monkeypatch):
    """本地已有完整文件时服务端会回 416 —— 这不是失败，别把它当成下载错误。

    实测踩过：同一个版本归档第二遍（或上次被中断后重跑）就会撞上 416，
    当时被当成失败，还留下一个 49 MB 的 _download 残留目录。
    """
    import io
    import json
    import urllib.error
    import urllib.request

    mod = _load()
    payload = json.dumps({"assets": [
        {"name": "x-win64.zip", "url": "https://api.github.com/fake/1"},
    ]}).encode()

    class FakeResp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class FakeOpener:
        def open(self, req, timeout=0):
            url = getattr(req, "full_url", req)
            if str(url).endswith("/releases/tags/v1.2.3"):
                return FakeResp(payload)
            raise urllib.error.HTTPError(str(url), 416, "Range Not Satisfiable", None, None)

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: FakeOpener())
    monkeypatch.setattr(mod, "_gh_token", lambda: "")
    (tmp_path / "x-win64.zip").write_bytes(b"already here")

    assert mod._download_with_proxy("1.2.3", tmp_path, "http://127.0.0.1:1") is True
