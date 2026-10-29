"""对项目做一次结构量化分析，输出 Markdown 报告片段。

指标（全部基于 AST，不依赖第三方）：
- 文件规模、函数/类数量
- 圈复杂度近似值（分支点计数：if/for/while/and/or/except/with/assert 三元）
- 最长函数、最复杂函数
- 重复函数（函数体归一化后完全相同 / 高度相似）
- 模块被依赖次数（谁最"重"）
- 疑似未被引用的顶层函数

用法：python tools/dev/code_metrics.py [--json]
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKIP_DIRS = {".venv", "dist", "build", "data", "logs", ".git", "__pycache__",
             "tests", "tools", "docs", "spike", ".desloppify"}

#: 两个重点模块的文件归属（按前缀匹配）
MODULES = {
    "找无码": ("ui/pages/censor_replace.py", "ui/censor_release.py",
               "ui/release_filter_dialog.py", "censor/release_filter.py",
               "censor/releases/"),
    "像素化检测": ("ui/pages/censorship.py", "ui/censor_scan.py", "ui/censor_results.py",
                   "ui/censor_gallery.py", "ui/review_controls.py",
                   "censor/service.py", "censor/store.py", "censor/jobs.py",
                   "censor/sampling.py", "censor/mosaic_cv.py", "censor/models.py",
                   "censor/review_route.py", "censor/tag_signals.py",
                   "censor/identity.py", "censor/file_signals.py",
                   "censor/evidence_refresh.py", "censor/scan_plan.py",
                   "censor/triage.py", "censor/numbering.py", "censor/metadata_sync.py"),
}


def module_of(rel: str) -> str:
    for name, prefixes in MODULES.items():
        if any(rel == p or rel.startswith(p) for p in prefixes):
            return name
    return "其它"


def _complexity(node: ast.AST) -> int:
    """近似圈复杂度：1 + 分支点个数。"""
    score = 1
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler,
                              ast.With, ast.AsyncWith, ast.Assert, ast.IfExp)):
            score += 1
        elif isinstance(child, ast.BoolOp):
            score += max(0, len(child.values) - 1)
        elif isinstance(child, ast.comprehension):
            score += 1 + len(child.ifs)
        elif isinstance(child, ast.Match):
            score += max(0, len(child.cases) - 1)
    return score


def _depth(node: ast.AST, level: int = 0) -> int:
    """最大嵌套深度（只算控制流）。"""
    block_types = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith,
                   ast.Try, ast.Match)
    best = level
    for child in ast.iter_child_nodes(node):
        step = 1 if isinstance(child, block_types) else 0
        best = max(best, _depth(child, level + step))
    return best


def _body_signature(node: ast.AST) -> str:
    """函数体归一化签名：去掉名字/常量，只留结构，用于找"复制粘贴"。"""
    class Normalize(ast.NodeTransformer):
        def visit_Name(self, item):
            return ast.Name(id="_v", ctx=item.ctx)

        def visit_Constant(self, item):
            return ast.Constant(value="_c")

        def visit_arg(self, item):
            return ast.arg(arg="_a", annotation=None)

        def visit_Attribute(self, item):
            return ast.Attribute(value=self.visit(item.value), attr="_attr", ctx=item.ctx)

    clone = Normalize().visit(ast.parse(ast.unparse(node)))
    return ast.dump(clone, annotate_fields=False, include_attributes=False)


def collect() -> dict:
    files = []
    functions = []
    sig_groups = defaultdict(list)
    imported_by = Counter()
    defined_top = {}

    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if any(part in SKIP_DIRS for part in rel.split("/")):
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue
        lines = source.count("\n") + 1
        funcs, classes = [], []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                length = (node.end_lineno or node.lineno) - node.lineno + 1
                entry = {
                    "file": rel, "name": node.name, "line": node.lineno,
                    "lines": length, "complexity": _complexity(node),
                    "depth": _depth(node),
                    "args": len(node.args.args) + len(node.args.kwonlyargs),
                }
                functions.append(entry)
                funcs.append(entry)
                if not node.name.startswith("_"):
                    defined_top.setdefault(node.name, []).append(rel)
                if length >= 8:
                    sig_groups[_body_signature(node)].append(f"{rel}::{node.name}")
            elif isinstance(node, ast.ClassDef):
                classes.append(node.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported_by[node.module.split(".")[0]] += 1
        files.append({"path": rel, "lines": lines, "functions": len(funcs),
                      "classes": len(classes),
                      "max_func_lines": max((f["lines"] for f in funcs), default=0),
                      "max_complexity": max((f["complexity"] for f in funcs), default=0),
                      "module": module_of(rel)})

    # 重复：同一签名出现在 >=2 个不同函数里
    dupes = [{"count": len(names), "names": names}
             for names in sig_groups.values() if len(names) >= 2]
    dupes.sort(key=lambda item: -item["count"])

    # 疑似死代码：顶层函数名在全项目里只出现于定义处
    # 一次性建词频表（原来是 N 个函数 × M 个文件的正则扫描，几百个函数时直接跑死）
    ident: Counter = Counter()
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if any(part in SKIP_DIRS for part in rel.split("/")):
            continue
        try:
            ident.update(re.findall(r"[A-Za-z_]\w*", path.read_text(encoding="utf-8")))
        except OSError:
            continue
    dead = [{"name": name, "file": where[0]}
            for name, where in defined_top.items()
            if name not in ("main", "__main__") and ident[name] <= len(where)]

    return {"files": files, "functions": functions, "dupes": dupes,
            "dead": dead, "imported_by": dict(imported_by)}


def render(data: dict) -> str:
    files, funcs = data["files"], data["functions"]
    total_lines = sum(f["lines"] for f in files)
    out = []
    add = out.append

    add(f"- 统计文件：**{len(files)}** 个，共 **{total_lines:,}** 行")
    add(f"- 函数：**{len(funcs)}** 个；平均圈复杂度 **{sum(f['complexity'] for f in funcs) / max(1, len(funcs)):.2f}**")
    add("")

    add("### 按模块")
    add("")
    add("| 模块 | 文件数 | 行数 | 函数数 | 平均复杂度 | 最大复杂度 | 最长函数(行) |")
    add("| --- | --- | --- | --- | --- | --- | --- |")
    for name in ("找无码", "像素化检测", "其它"):
        part = [f for f in files if f["module"] == name]
        if not part:
            continue
        part_funcs = [f for f in funcs if any(f["file"] == p["path"] for p in part)]
        add("| {} | {} | {:,} | {} | {:.2f} | {} | {} |".format(
            name, len(part), sum(p["lines"] for p in part), len(part_funcs),
            sum(f["complexity"] for f in part_funcs) / max(1, len(part_funcs)),
            max((f["complexity"] for f in part_funcs), default=0),
            max((f["max_func_lines"] for f in part), default=0)))
    add("")

    add("### 最大的 15 个文件")
    add("")
    add("| 行数 | 函数 | 最大函数行 | 最大复杂度 | 路径 |")
    add("| --- | --- | --- | --- | --- |")
    for item in sorted(files, key=lambda f: -f["lines"])[:15]:
        add(f"| {item['lines']} | {item['functions']} | {item['max_func_lines']} | "
            f"{item['max_complexity']} | `{item['path']}` |")
    add("")

    add("### 复杂度最高的 20 个函数")
    add("")
    add("| CC | 行数 | 嵌套 | 参数 | 函数 | 文件:行 |")
    add("| --- | --- | --- | --- | --- | --- |")
    for item in sorted(funcs, key=lambda f: (-f["complexity"], -f["lines"]))[:20]:
        add(f"| {item['complexity']} | {item['lines']} | {item['depth']} | {item['args']} | "
            f"`{item['name']}` | `{item['file']}:{item['line']}` |")
    add("")

    add("### 最长的 20 个函数")
    add("")
    add("| 行数 | CC | 函数 | 文件:行 |")
    add("| --- | --- | --- | --- |")
    for item in sorted(funcs, key=lambda f: -f["lines"])[:20]:
        add(f"| {item['lines']} | {item['complexity']} | `{item['name']}` | "
            f"`{item['file']}:{item['line']}` |")
    add("")

    add(f"### 结构重复（函数体骨架相同的组）：{len(data['dupes'])} 组")
    add("")
    for group in data["dupes"][:12]:
        add(f"- **{group['count']} 处**：" + "、".join(f"`{n}`" for n in group["names"][:6]))
    if not data["dupes"]:
        add("（没有发现完全同构的函数体）")
    add("")

    add(f"### 疑似未被引用（仅出现在定义处）：{len(data['dead'])} 个")
    add("")
    for item in data["dead"][:20]:
        add(f"- `{item['name']}` @ `{item['file']}`")
    if not data["dead"]:
        add("（无）")
    add("")

    add("### 被依赖最多的顶层包")
    add("")
    for name, count in sorted(data["imported_by"].items(), key=lambda kv: -kv[1])[:10]:
        add(f"- `{name}`：被 {count} 处 import")
    return "\n".join(out)


if __name__ == "__main__":
    data = collect()
    if "--json" in sys.argv:
        print(json.dumps(data, ensure_ascii=False, indent=1))
    else:
        print(render(data))
