"""界面接线回归测试 —— 覆盖"按钮点一下就崩"的未定义名字类缺陷。

为什么单独一个文件：界面测试通常只测"页面能构造、能导航、能 shutdown"，
**没有任何用例真的去点那些按钮**。而下面这些缺陷正是这么漏过去的：

- ``SettingsPage._browse_ffmpeg()`` 用了 ``sys.platform``，但模块里没 ``import sys``
  → 用户点「FFmpeg 路径 → 浏览」直接 ``NameError``；
- ``SettingsPage._probe_hardware()`` 用了 ``QApplication.setOverrideCursor``，
  但没导入 ``QApplication`` → 点「实测本机能力」直接崩；
- ``NFOPage._ai_check_nfo()`` 用了 ``QMessageBox`` 但没导入 → 点「AI 检查冲突」
  在第一/二/三个分支上直接崩（恰恰是最常走的分支）；
- ``DeletePage._on_ai_recheck_done()`` 用了 ``QColor`` 但没导入
  → AI 复核完成后标黄行时崩。

它们的共同点是：**方法本身能 import、页面能构造、静态看也像对的**，
只有真的调用一次才会暴露。所以这里逐个调用。
"""

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def cfg():
    from config import ToolkitConfig

    return ToolkitConfig()


def _silence(monkeypatch):
    """挡掉所有模态弹窗 —— 离屏模式下模态框会阻塞事件循环。"""
    from PySide6.QtWidgets import QMessageBox

    for name in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(lambda *a, **k: None), raising=False)
    monkeypatch.setattr(QMessageBox, "Yes", QMessageBox.Yes, raising=False)


@pytest.mark.parametrize("module,names", [
    ("ui.pages.settings", ("sys", "QApplication", "QMessageBox")),
    ("ui.pages.nfo_fix", ("os", "QMessageBox")),
    ("ui.pages.delete", ("os", "QColor", "QMessageBox")),
])
def test_ui_modules_expose_the_names_they_use(module, names):
    """轻量守卫：模块用到的基础名字必须真的绑在模块上。

    比逐个按钮更早报警，也比"页面能构造"强 —— 构造不会碰到这些名字。
    """
    mod = importlib.import_module(module)
    missing = [n for n in names if not hasattr(mod, n)]
    assert not missing, f"{module} 缺少 {missing}，调用相关方法会 NameError"


def test_settings_browse_ffmpeg_does_not_raise(qapp, cfg, monkeypatch):
    """点「浏览」选 ffmpeg —— 曾经因为没 import sys 而 NameError。"""
    from PySide6.QtWidgets import QFileDialog

    from ui.pages.settings import SettingsPage

    monkeypatch.setattr(QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: ("", "")))
    page = SettingsPage(cfg)
    page._browse_ffmpeg()          # 旧实现：NameError: name 'sys' is not defined

    # 选中路径时也要能走完（会顺手刷新工具状态）
    monkeypatch.setattr(QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(Path("fake-ffmpeg")), "")))
    page._browse_ffmpeg()
    assert page.input_ffmpeg_path.text().endswith("fake-ffmpeg")


def test_settings_probe_hardware_does_not_raise(qapp, cfg, monkeypatch):
    """点「实测本机能力」—— 曾经因为没导入 QApplication 而 NameError。"""
    from ui.pages.settings import SettingsPage
    from utils import tools

    _silence(monkeypatch)
    monkeypatch.setattr(tools, "resolve_tool", lambda *a, **k: "/fake/ffmpeg")
    monkeypatch.setattr(tools, "probe_hardware",
                        lambda *a, **k: {"encoders": [], "hwaccels": [],
                                         "recommended_encoder": "", "recommended_hwaccel": ""})
    monkeypatch.setattr(tools, "clear_probe_cache", lambda *a, **k: None)

    page = SettingsPage(cfg)
    page._probe_hardware()

    # 没有 ffmpeg 的分支同样要覆盖（会走 QMessageBox.warning 后 return）
    monkeypatch.setattr(tools, "resolve_tool", lambda *a, **k: None)
    page._probe_hardware()


def test_nfo_fix_ai_check_does_not_raise(qapp, monkeypatch):
    """点「AI 检查冲突」—— 曾经因为没导入 QMessageBox 而 NameError。"""
    from config import ToolkitConfig
    from ui.pages.nfo_fix import NFOPage

    _silence(monkeypatch)
    cfg = ToolkitConfig()
    page = NFOPage(cfg)

    # 分支 1：AI 未启用
    cfg.ai_enabled = False
    page._ai_check_nfo()

    # 分支 2：启用了但没选目录
    cfg.ai_enabled = True
    page._dir = ""
    page._ai_check_nfo()


def test_delete_ai_recheck_done_does_not_raise(qapp, cfg, monkeypatch):
    """AI 复核完成后标黄保留行 —— 曾经因为没导入 QColor 而 NameError。"""
    from ui.pages.delete import DeletePage

    _silence(monkeypatch)
    page = DeletePage(cfg)
    page._delete_rows = [{"path": "C:/MediaDemo/A/MIAA-743/MIAA-743.mp4",
                          "num": "MIAA-743", "size": 100}]
    page._ai_keep_nums = {"MIAA-743"}
    page._on_ai_recheck_done()

    # 没有保留项时也要能走完
    page._ai_keep_nums = set()
    page._on_ai_recheck_done()


# ── 系统性守卫 ────────────────────────────────────────────────

def test_all_private_self_calls_are_defined():
    """所有 ``self._xxx()`` 调用都必须有定义。

    这条抓的就是 ``MergePage._backup_root_text`` 那类缺陷：**调用点写了、定义没了**
    （重构时改名或漏拷），静态看完全像对的，只有走到那个分支才 AttributeError。
    之前 3 处调用它，而全仓没有定义 —— 分集合并的「确认替换」流程必崩。

    只查下划线开头的名字：Qt 基类的公开方法（``self.setText`` / ``append`` /
    ``update`` …）不会以下划线开头，所以几乎没有误报；同模块内的继承链会被解析。
    """
    import ast

    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(root).parts
        if parts[0] in ("build", "dist", "tests", "tools") or "__pycache__" in parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}

        def members(cls, seen=None):
            """本类 + 同模块父类里绑定的所有名字。"""
            seen = seen or set()
            if cls.name in seen:
                return set()
            seen.add(cls.name)
            out = set()
            for stmt in cls.body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.add(stmt.name)
                elif isinstance(stmt, ast.Assign):
                    out |= {t.id for t in stmt.targets if isinstance(t, ast.Name)}
                elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    out.add(stmt.target.id)
            for sub in ast.walk(cls):
                if (isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name)
                        and sub.value.id == "self" and isinstance(sub.ctx, ast.Store)):
                    out.add(sub.attr)
            for base in cls.bases:
                if isinstance(base, ast.Name) and base.id in classes:
                    out |= members(classes[base.id], seen)
            return out

        for cls in classes.values():
            known = members(cls)
            for sub in ast.walk(cls):
                if not (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)):
                    continue
                fn = sub.func
                if not (isinstance(fn.value, ast.Name) and fn.value.id == "self"):
                    continue
                name = fn.attr
                if not name.startswith("_") or name.startswith("__"):
                    continue
                if name not in known:
                    offenders.append(
                        f"{path.relative_to(root)}:{sub.lineno}: "
                        f"{cls.name} 调用了未定义的 self.{name}()")
    assert not offenders, "存在调用但未定义的私有方法：\n" + "\n".join(offenders)


def test_merge_page_backup_root_text_resolves(qapp, cfg):
    """分集合并的「备份到哪儿」提示必须能真的算出来。

    这个方法曾经**只有 3 处调用、没有定义**，一点「确认替换原分集」就 AttributeError。
    这里断言它返回的是**解析后**的绝对路径（含原分集子目录），而不是配置原文。
    """
    from ui.pages.merge import MergePage
    from utils.merge_archive import resolve_backup_root

    page = MergePage(cfg)
    text = page._backup_root_text()
    expected = resolve_backup_root(cfg) / cfg.merge_original_subfolder
    assert text == str(expected)
    assert text.endswith(cfg.merge_original_subfolder)
    assert Path(text).is_absolute()
