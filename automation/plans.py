"""Persist reviewed operation targets, without storing authentication secrets."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import tempfile
from uuid import uuid4


def file_identity(path, should_stop=None):
    p = Path(path)
    if not p.is_file():
        return None
    stat = p.stat()
    digest = hashlib.sha256()
    with p.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            if should_stop and should_stop():
                raise InterruptedError("计划校验已取消")
            digest.update(chunk)
    after = p.stat()
    if (stat.st_size, stat.st_mtime_ns, stat.st_ino) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
    ):
        raise ValueError(f"校验期间文件发生变化：{path}")
    return [stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino, digest.hexdigest()]


def config_identity(cfg):
    payload = asdict(cfg)
    for key in ("jellyfin_api_key", "ai_key", "theme", "onboarding_done"):
        payload.pop(key, None)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _root():
    import config

    return config.DATA_DIR / "operation_plans"


def write_json_atomic(path, payload):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=str(p.parent), prefix=".plan-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, p)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def save_plan(cfg, actions, selected_actions, *, report=None, should_stop=None):
    from datetime import datetime

    plan = {
        "id": uuid4().hex,
        "version": 1,
        "config_identity": config_identity(cfg),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "selected_actions": list(selected_actions),
        "actions": actions,
        "report": deepcopy(report or {}),
    }
    for index, action in enumerate(actions):
        action.setdefault("id", f"{plan['id']}:{index}")
        paths = action.get("inputs") or ([action["path"]] if action.get("path") else [])
        action["file_identities"] = {str(p): file_identity(p, should_stop) for p in paths}
        if action.get("target"):
            action["target_identity"] = file_identity(action["target"], should_stop)
    write_json_atomic(_root() / f"{plan['id']}.json", plan)
    return plan


def load_plan(plan_id, cfg, should_stop=None):
    if not re.fullmatch(r"[0-9a-f]{32}", str(plan_id)):
        raise ValueError("操作计划编号无效")
    with (_root() / f"{plan_id}.json").open(encoding="utf-8") as stream:
        plan = json.load(stream)
    if plan.get("version") != 1 or plan.get("config_identity") != config_identity(cfg):
        raise ValueError("连接、观看用户或规则发生变化，请重新生成并审核计划")
    if (_root() / f"{plan_id}.executing").exists():
        raise ValueError("此计划已执行或正在执行，请重新创建审核计划")
    for action in plan.get("actions", []):
        for path, expected in action.get("file_identities", {}).items():
            if expected is None or file_identity(path, should_stop) != expected:
                raise ValueError(f"计划文件发生变化，请重新审核：{path}")
        if action.get("target") and file_identity(action["target"], should_stop) != action.get(
            "target_identity"
        ):
            raise ValueError(f"计划目标发生变化，请重新审核：{action['target']}")
    return plan


def claim_plan(plan):
    """A durable, exclusive claim also blocks unsafe retries after interruption."""
    path = _root() / f"{plan['id']}.executing"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError("此计划已执行或正在执行，请重新创建审核计划") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(plan["id"])
        stream.flush()
        os.fsync(stream.fileno())
