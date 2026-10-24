"""Inventory project Python sources without importing or reading user data."""
from __future__ import annotations

import ast
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKIP = {'.git', '.venv', 'venv', 'env', 'build', 'dist', 'data', 'AppData',
        '.local', 'demo', '__pycache__', '.pytest_cache', '.ruff_cache', 'assets'}


def inventory(root=ROOT):
    modules, errors = [], []
    duplicates = defaultdict(list)
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = sorted(name for name in subdirs
                            if name not in SKIP and not name.startswith('_backup_'))
        for name in sorted(files):
            if not name.endswith(('.py', '.pyw')):
                continue
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            try:
                source = path.read_text(encoding='utf-8-sig')
                tree = ast.parse(source, filename=relative)
            except (OSError, UnicodeError, SyntaxError) as exc:
                errors.append({'path': relative, 'error_type': type(exc).__name__})
                continue
            functions = [node for node in ast.walk(tree)
                         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
            modules.append({'path': relative, 'lines': len(source.splitlines()),
                            'functions': len(functions)})
            if relative.startswith(('tests/', 'tools/')):
                continue
            for node in functions:
                if node.end_lineno - node.lineno < 10:
                    continue
                body = list(node.body)
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    body.pop(0)
                key = hashlib.sha256(repr([ast.dump(item) for item in body]).encode()).hexdigest()
                duplicates[key].append({'path': relative, 'line': node.lineno, 'name': node.name})
    return {'scope': 'Python/pyw syntax and exact function-body duplicates; excludes runtime data and dependencies',
            'modules': len(modules), 'lines': sum(item['lines'] for item in modules),
            'syntax_errors': errors, 'inventory': modules,
            'exact_duplicate_groups': [group for group in duplicates.values() if len(group) > 1]}


if __name__ == '__main__':
    report = inventory()
    target = ROOT / 'docs' / '通用审计第二轮-2026-10-23-inventory.json'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key != 'inventory'},
                     ensure_ascii=False, indent=2))
