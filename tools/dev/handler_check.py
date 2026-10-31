"""静态检查：按钮/信号连到不存在的方法，以及引用了从未赋值的属性。

用法::

    python tools/dev/handler_check.py                  # 扫 ui/ workers/ automation/
    python tools/dev/handler_check.py --all            # 扫全项目（含 censor/ utils/）
    python tools/dev/handler_check.py --only-missing   # 只看"连到不存在的方法"
    python tools/dev/handler_check.py --json

为什么不用运行期检测：PySide6 的 Python 信号连接**不会**进 Qt 的 receiver 表
（``QObject.receivers("clicked()")`` 恒为 0，已实测），``SignalInstance`` 也不提供
反查控件的能力。所以"这个按钮到底连没连上"只能静态判定。

两类问题：

1. **missing_handler**（P0，一定会炸）
   ``self.btn_x.clicked.connect(self._on_x)`` 但类里（含项目内基类）没有 ``_on_x``。
   点击时抛 ``AttributeError``，被 Qt 吞掉 → 用户看到"点了没反应"。

2. **never_assigned**（P1，大概率会炸）
   某个 ``self.foo`` 在方法里被读，但整个类（含项目内基类）从未 ``self.foo = ...``。
   点击/刷新时抛 ``AttributeError``。``setattr(self, "foo", ...)`` 会被识别并豁免。
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

DEFAULT_TARGETS = ["ui", "workers", "automation"]
ALL_TARGETS = ["ui", "workers", "automation", "censor", "utils"]

# 这些基类来自第三方/标准库，方法集不参与分析
EXTERNAL_BASES = {
    "QWidget", "QMainWindow", "QDialog", "QObject", "QFrame", "QThread", "QPushButton",
    "QLabel", "QTableWidget", "QTreeWidget", "QListWidget", "QComboBox", "QAbstractItemView",
    "QAbstractTableModel", "QAbstractListModel", "QStyledItemDelegate", "QGraphicsView",
    "QSplitter", "QTabWidget", "QScrollArea", "QGroupBox", "QStackedWidget", "QMenu",
    "QDialogButtonBox", "QWizardPage", "QGraphicsObject", "QAbstractButton", "object",
}


class ClassInfo:
    __slots__ = ("name", "module", "path", "lineno", "bases", "methods", "assigned", "dynamic")

    def __init__(self, name, module, path, lineno, bases):
        self.name = name
        self.module = module
        self.path = path
        self.lineno = lineno
        self.bases = bases
        self.methods = set()
        self.assigned = set()
        #: 类里出现 ``setattr(self, <变量>, ...)`` 或自定义 ``__getattr__`` 时，
        #: 静态分析无法证明某属性缺失，这类类整体不报 never_assigned。
        self.dynamic = False


class ModuleInfo:
    def __init__(self, path: Path, module: str):
        self.path = path
        self.module = module
        self.classes: dict[str, ClassInfo] = {}
        self.connects: list[tuple[int, str, str]] = []  # (行号, 所属类, 处理函数名)
        self.reads: list[tuple[int, str, str]] = []  # (行号, 所属类, 属性名)


def _module_name(path: Path, root: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _assigned_names(node: ast.AST) -> set[str]:
    """收集节点内所有 ``self.X = ...`` 与 ``setattr(self, "X", ...)``。"""
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = child.targets if isinstance(child, ast.Assign) else [child.target]
            for target in targets:
                found |= _target_names(target)
        elif isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name) and func.id == "setattr" and len(child.args) >= 2:
                obj, name = child.args[0], child.args[1]
                if isinstance(obj, ast.Name) and obj.id == "self" and isinstance(name, ast.Constant):
                    if isinstance(name.value, str):
                        found.add(name.value)
        elif isinstance(child, ast.Attribute) and isinstance(child.ctx, ast.Store):
            if isinstance(child.value, ast.Name) and child.value.id == "self":
                found.add(child.attr)
    return found


def _target_names(target: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(target):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            if isinstance(node.value, ast.Name) and node.value.id == "self":
                names.add(node.attr)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for element in node.elts:
                names |= _target_names(element)
    return names


def _handler_name(func: ast.AST) -> str | None:
    """``self._on_x`` → ``_on_x``；lambda / 其它形式返回 None（不检查）。"""
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id == "self":
            return func.attr
    return None


def _class_body_attrs(node: ast.ClassDef) -> set[str]:
    """类体里的 ``X = ...`` / ``X: T = ...``，例如 ``log = Signal(str)``。"""
    found: set[str] = set()
    for item in node.body:
        if isinstance(item, ast.Assign):
            for target in item.targets:
                for sub in ast.walk(target):
                    if isinstance(sub, ast.Name):
                        found.add(sub.id)
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            found.add(item.target.id)
    return found


def _has_dynamic_attrs(node: ast.ClassDef) -> bool:
    """类内是否有难以静态追踪的属性注入方式。

    三种：
    1. ``setattr(self, <非字面量>, ...)`` —— 名字是循环变量
    2. 自定义 ``__getattr__`` / ``__setattr__``
    3. 调用模块级函数并把自己传进去：``build_review_workspace(self)``
       —— 属性被那个函数赋上了（真实存在，但不在本类里）。这类"UI 构建外包"
       见 ``ui/pages/review.py:128``；它同时意味着 IDE 静态分析、类型检查全部失效。
    """
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name) and func.id == "setattr" and len(child.args) >= 2:
                obj, name = child.args[0], child.args[1]
                if isinstance(obj, ast.Name) and obj.id == "self" and not isinstance(name, ast.Constant):
                    return True
            if isinstance(func, ast.Name) and child.args:
                first = child.args[0]
                if isinstance(first, ast.Name) and first.id == "self":
                    return True
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name in (
            "__getattr__", "__setattr__",
        ):
            return True
    return False


def parse_module(path: Path, root: Path) -> ModuleInfo:
    module = ModuleInfo(path, _module_name(path, root))
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    except SyntaxError as exc:
        print(f"[语法错误] {path}: {exc}", file=sys.stderr)
        return module

    # 顶层函数也算（有些页面把处理函数写成模块级函数再 partial）
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases = []
            for base in node.bases:
                if isinstance(base, ast.Name):
                    bases.append(base.id)
                elif isinstance(base, ast.Attribute):
                    bases.append(base.attr)
            info = ClassInfo(node.name, module.module, path, node.lineno, bases)
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    info.methods.add(item.name)
            info.assigned = _assigned_names(node) | _class_body_attrs(node)
            info.dynamic = _has_dynamic_attrs(node)
            module.classes[node.name] = info

    # 第二次遍历：找出 connect 与 self.X 读取，并归属到所在类
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        owner = node.name
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                # 只认 `<对象>.<信号>.connect(处理函数)` 这一形状：
                # func.value 必须是 Attribute（如 self.btn.clicked / events.media_changed）。
                # 不这么卡的话 `sqlite3.connect(self.db_path)` 会被误当成信号连接，
                # 于是把普通的路径属性报成"不存在的方法"（第一版踩过）。
                if child.func.attr == "connect" and child.args:
                    if not isinstance(child.func.value, ast.Attribute):
                        continue
                    handler = _handler_name(child.args[0])
                    if handler:
                        module.connects.append((child.lineno, owner, handler))
            if isinstance(child, ast.Attribute) and isinstance(child.ctx, ast.Load):
                if isinstance(child.value, ast.Name) and child.value.id == "self":
                    module.reads.append((child.lineno, owner, child.attr))
    return module


def build_hierarchy(modules: list[ModuleInfo]) -> dict[str, ClassInfo]:
    registry: dict[str, ClassInfo] = {}
    for module in modules:
        for name, info in module.classes.items():
            registry.setdefault(name, info)
    return registry


_QT_MEMBER_CACHE: dict[str, set[str]] = {}


def qt_members(class_name: str) -> set[str]:
    """取 Qt 基类的真实成员集（用运行期 ``dir()``，不靠猜）。

    不这么做的话，``self.setText(...)`` / ``self.accept()`` 这类继承自 Qt 的
    成员会被当成"从未赋值"或"方法不存在"，噪声会淹没真问题 —— 第一版实测
    汇报了 355 条，其中 340+ 是这种误报。
    """
    if class_name in _QT_MEMBER_CACHE:
        return _QT_MEMBER_CACHE[class_name]
    members: set[str] = set()
    try:
        from PySide6 import QtCore, QtGui, QtWidgets

        for holder in (QtWidgets, QtGui, QtCore):
            cls = getattr(holder, class_name, None)
            if cls is not None:
                members = set(dir(cls))
                break
    except ImportError:
        members = set()
    if not members:
        # 标准库基类（枚举 / 异常等）也要认，否则 Enum 成员的 .value 会被误报
        import builtins
        import enum

        for holder in (enum, builtins):
            cls = getattr(holder, class_name, None)
            if cls is not None:
                members = set(dir(cls))
                break
    _QT_MEMBER_CACHE[class_name] = members
    return members


def external_members(name: str, registry: dict[str, ClassInfo], seen=None) -> set[str]:
    """递归汇总某个类所有外部基类（Qt / object）的成员。"""
    seen = seen or set()
    if name in seen:
        return set()
    seen.add(name)
    if name not in registry:
        return qt_members(name)
    info = registry[name]
    found: set[str] = set()
    for base in info.bases:
        found |= external_members(base, registry, seen)
    return found


def resolve_members(name: str, registry: dict[str, ClassInfo], seen=None) -> tuple[set, set]:
    """返回（方法集, 已赋值属性集），沿项目内基类递归。"""
    seen = seen or set()
    if name in seen or name not in registry:
        return set(), set()
    seen.add(name)
    info = registry[name]
    methods, attrs = set(info.methods), set(info.assigned)
    for base in info.bases:
        if base in EXTERNAL_BASES:
            continue
        m, a = resolve_members(base, registry, seen)
        methods |= m
        attrs |= a
    return methods, attrs


def has_dynamic_attrs(name: str, registry: dict[str, ClassInfo], seen=None) -> bool:
    """本类或其项目内基类是否有动态赋值（有则静态判定不可靠）。"""
    seen = seen or set()
    if name in seen or name not in registry:
        return False
    seen.add(name)
    info = registry[name]
    if info.dynamic:
        return True
    return any(has_dynamic_attrs(base, registry, seen) for base in info.bases)


#: 枚举基类：成员属性（.value/.name）由 Enum 元类在运行期生成，不适用本检查
ENUM_BASES = {"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"}


def _unjudgeable(name: str, registry: dict[str, ClassInfo], seen=None) -> str:
    """返回"无法静态判定"的原因；空串表示可以判定。

    两种情况放弃判定，宁可不报也不制造噪声：
    - 基类是枚举（``.value`` 由元类生成，``dir(Enum)`` 里压根没有）
    - 基类既不在项目内、也查不到成员（如 ``html.parser.HTMLParser``）
    """
    seen = seen or set()
    if name in seen:
        return ""
    seen.add(name)
    if name in ENUM_BASES:
        return "enum"
    if name not in registry:
        return "" if qt_members(name) else f"未知基类 {name}"
    for base in registry[name].bases:
        reason = _unjudgeable(base, registry, seen)
        if reason:
            return reason
    return ""


def check(targets: list[str], only_missing: bool = False) -> dict:
    files: list[Path] = []
    for target in targets:
        base = ROOT / target
        if base.is_dir():
            files.extend(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)
    modules = [parse_module(path, ROOT) for path in sorted(files)]
    registry = build_hierarchy(modules)

    # 模块级函数（connect 到模块函数的情况）
    missing, never_assigned = [], []
    for module in modules:
        for lineno, owner, handler in module.connects:
            owner_methods, _ = resolve_members(owner, registry)
            if handler in owner_methods:
                continue
            if handler in external_members(owner, registry):
                continue  # Qt 基类自带（如 QDialog.accept）
            missing.append({
                "file": str(module.path.relative_to(ROOT)).replace("\\", "/"),
                "line": lineno,
                "class": owner,
                "handler": handler,
            })
        if not only_missing:
            for lineno, owner, attr in module.reads:
                owner_methods, owner_attrs = resolve_members(owner, registry)
                if attr in owner_attrs or attr in owner_methods:
                    continue
                if attr in _EXEMPT_ATTRS:
                    continue
                if attr in external_members(owner, registry):
                    continue  # 继承自 Qt 的方法/属性，不是本类漏赋值
                if has_dynamic_attrs(owner, registry):
                    continue  # 类里有 setattr(self, 变量, ...)，静态判定不可靠
                if _unjudgeable(owner, registry):
                    continue  # 枚举 / 未知基类，宁可不报
                never_assigned.append({
                    "file": str(module.path.relative_to(ROOT)).replace("\\", "/"),
                    "line": lineno,
                    "class": owner,
                    "attr": attr,
                })
    # 去重（同一属性多处读取只报第一次）
    seen = set()
    deduped = []
    for row in sorted(never_assigned, key=lambda r: (r["file"], r["line"])):
        key = (row["file"], row["class"], row["attr"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)

    # 统计有多少类因为"静态不可判定"被跳过 —— 不写这个数，"0 问题"就是骗人的
    skipped = {}
    for module in modules:
        for name in module.classes:
            if has_dynamic_attrs(name, registry):
                skipped.setdefault("动态属性（setattr/外部构建函数）", []).append(name)
            else:
                reason = _unjudgeable(name, registry)
                if reason:
                    skipped.setdefault(reason, []).append(name)
    return {"missing_handler": missing, "never_assigned": deduped,
            "skipped": {k: sorted(set(v)) for k, v in skipped.items()},
            "class_count": sum(len(m.classes) for m in modules)}


#: Qt / 常见框架注入的属性，不可能是本类漏赋值
_EXEMPT_ATTRS = {
    "cfg",  # 由构造注入
    "parent", "objectName", "window", "layout",
}


def resolve_members_for_module(module, registry):
    methods, attrs = set(), set()
    for name in module.classes:
        m, a = resolve_members(name, registry)
        methods |= m
        attrs |= a
    return methods, attrs


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="静态检查按钮处理函数与属性赋值")
    parser.add_argument("--all", action="store_true", help="扫全项目（含 censor/ utils/）")
    parser.add_argument("--only-missing", action="store_true", help="只看连到不存在的方法")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = check(ALL_TARGETS if args.all else DEFAULT_TARGETS, only_missing=args.only_missing)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["missing_handler"] else 0

    missing = report["missing_handler"]
    print(f"== 连到不存在的方法：{len(missing)} 处 ==")
    for row in missing:
        print(f"  P0 {row['file']}:{row['line']}  {row['class']} → self.{row['handler']} 不存在")
    if not args.only_missing:
        rows = report["never_assigned"]
        print(f"\n== 引用但从未赋值的属性：{len(rows)} 处（需人工确认是否动态注入）==")
        for row in rows:
            print(f"  P1 {row['file']}:{row['line']}  {row['class']}.{row['attr']}")
    total = report.get("class_count", 0)
    skipped = report.get("skipped", {})
    skipped_n = sum(len(v) for v in skipped.values())
    print(f"\n== 覆盖范围：{total} 个类，其中 {skipped_n} 个无法静态判定（已跳过）==")
    for reason, names in skipped.items():
        print(f"  {reason}: {len(names)} 个 —— {', '.join(names[:6])}"
              + (" …" if len(names) > 6 else ""))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
