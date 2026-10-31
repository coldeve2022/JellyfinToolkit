"""转封装替换完成后，必须把原始「恢复版」文件清理掉。

`execute_replacements` 的转封装分支会把 `source` 换成程序生成的临时文件，
然后 `safe_replace(move_source=True)` 删的是**那个临时文件**。原始
`xxx.restored.mp4` 因为 `expected_source_hash=None` 被跳过校验，也没人删它 ——
结果磁盘上同时留下新片和旧恢复文件，下次扫描又会把它当成新片重检一遍。

（审计时的「中高」风险项）
"""

from __future__ import annotations

import utils.replacement as replacement


def test_container_convert_cleanup_removes_original_restored_file(monkeypatch, tmp_path):
    original = tmp_path / "ABC-123.restored.mp4"
    original.write_bytes(b"original restored bytes")
    target = tmp_path / "ABC-123.mp4"
    target.write_bytes(b"library file")

    phone = tmp_path / ".toolkit-stage-converted.mp4"
    phone.write_bytes(b"converted")

    monkeypatch.setattr(replacement, "transcode_container",
                        lambda source, suffix, **k: str(phone))
    monkeypatch.setattr(replacement, "file_hash",
                        lambda path: "S" if str(path) == str(original) else "T")
    monkeypatch.setattr(replacement, "validate_restored_output", lambda *a: (True, ""))

    def fake_safe_replace(source, target, *, move_source=False, **kwargs):
        # 模拟 move_source=True：成功后删源（这里是转封装后的临时文件）
        from pathlib import Path
        if move_source:
            Path(source).unlink(missing_ok=True)
        return {"ok": True, "state": "succeeded", "target": str(target)}

    monkeypatch.setattr(replacement, "safe_replace", fake_safe_replace)

    plan = [dict(source=str(original), target=str(target), status="ready",
                 convert_container=True, source_hash="S", target_hash="T")]
    stats = replacement.execute_replacements(plan, log=lambda *_: None)

    assert stats["ok"] == 1
    assert not phone.exists(), "转封装临时文件应被清理"
    assert not original.exists(), "转封装替换成功后，原始恢复版文件应被删除"


def test_container_convert_failure_keeps_original(monkeypatch, tmp_path):
    original = tmp_path / "DEF-456.restored.mp4"
    original.write_bytes(b"original restored bytes")
    target = tmp_path / "DEF-456.mp4"
    target.write_bytes(b"library file")
    phone = tmp_path / ".toolkit-stage-converted.mp4"
    phone.write_bytes(b"converted")

    monkeypatch.setattr(replacement, "transcode_container",
                        lambda source, suffix, **k: str(phone))
    monkeypatch.setattr(replacement, "file_hash",
                        lambda path: "S" if str(path) == str(original) else "T")
    monkeypatch.setattr(replacement, "validate_restored_output", lambda *a: (True, ""))
    monkeypatch.setattr(replacement, "safe_replace",
                        lambda *a, **k: {"ok": False, "state": "failed", "error": "磁盘写入失败"})

    plan = [dict(source=str(original), target=str(target), status="ready",
                 convert_container=True, source_hash="S", target_hash="T")]
    stats = replacement.execute_replacements(plan, log=lambda *_: None)

    assert stats["fail"] == 1
    assert original.exists(), "替换失败时原始文件必须保留"
