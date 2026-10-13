"""Run the release gate and bind its result to the exact source inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {"pytest", "ruff", "compileall", "version"}


def _git(root, *args):
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=True,
    )
    return result.stdout.strip()


def git_commit(root):
    return _git(root, "rev-parse", "HEAD")


def source_files(root):
    return sorted(
        set(
            name
            for name in _git(
                root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
            ).split("\0")
            if name
        )
    )


def source_digest(root):
    digest = hashlib.sha256()
    for name in source_files(root):
        path = Path(root) / name
        if not path.is_file():
            raise ValueError(f"源码文件缺失：{name}")
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def write_receipt(root, receipt, checks):
    if set(checks) != REQUIRED or any(code != 0 for code in checks.values()):
        raise ValueError("完整发布检查尚未通过")
    payload = {
        "version": 1,
        "commit": git_commit(root),
        "source_digest": source_digest(root),
        "checks": checks,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version,
        "platform": sys.platform,
    }
    path = Path(receipt)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def validate_receipt(root, receipt):
    payload = json.loads(Path(receipt).read_text(encoding="utf-8"))
    if payload.get("version") != 1 or set(payload.get("checks", {})) != REQUIRED:
        raise ValueError("发布验证记录不完整")
    if any(code != 0 for code in payload["checks"].values()):
        raise ValueError("发布验证包含失败检查")
    if payload.get("commit") != git_commit(root) or payload.get("source_digest") != source_digest(
        root
    ):
        raise ValueError("源码或提交发生变化，请重新运行完整验证")
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", default=str(ROOT / "build" / "verification.json"))
    args = parser.parse_args()
    if _git(ROOT, "status", "--porcelain"):
        print("发布检查需要已提交的干净源码；先保存并提交所有实现和文档", file=sys.stderr)
        return 1
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8")
    checks = {}
    initial_digest = source_digest(ROOT)
    with tempfile.TemporaryDirectory(prefix="toolkit-release-tests-") as isolated:
        env["JELLYFIN_TOOLKIT_DATA_DIR"] = isolated
        commands = {
            "pytest": [sys.executable, "-m", "pytest", "tests", "-q"],
            "ruff": [sys.executable, "-m", "ruff", "check", "."],
            "compileall": [
                sys.executable,
                "-m",
                "compileall",
                "-q",
                "ui",
                "utils",
                "workers",
                "automation",
                "scripts",
                "tools",
                "tests",
                "main.py",
                "config.py",
                "version.py",
            ],
            "version": [sys.executable, "scripts/version_manage.py", "check"],
        }
        for name, cmd in commands.items():
            checks[name] = subprocess.call(cmd, cwd=str(ROOT), env=env)
            if checks[name]:
                return 1
    if initial_digest != source_digest(ROOT):
        print("检查期间源码变化，拒绝记录为已验证版本", file=sys.stderr)
        return 1
    write_receipt(ROOT, args.receipt, checks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
