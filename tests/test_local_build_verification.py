"""Local requested builds may verify dirty sources, retaining exact digest binding."""
import json
import sys


def test_dirty_source_verification_requires_explicit_local_option(tmp_path, monkeypatch):
    from scripts import verify_release as verify
    monkeypatch.setattr(verify, 'ROOT', tmp_path)
    monkeypatch.setattr(verify, '_git', lambda root, *args: ' M source.py' if args[0] == 'status' else 'base')
    monkeypatch.setattr(verify, 'source_digest', lambda root: 'exact-dirty-source-digest')
    calls = []
    monkeypatch.setattr(verify.subprocess, 'call', lambda *args, **kwargs: calls.append(args) or 0)
    monkeypatch.setattr(sys, 'argv', ['verify_release.py'])
    assert verify.main() == 1
    assert calls == []
    receipt = tmp_path / 'verified.json'
    monkeypatch.setattr(sys, 'argv', ['verify_release.py', '--allow-dirty', '--receipt', str(receipt)])
    assert verify.main() == 0
    payload = json.loads(receipt.read_text(encoding='utf-8'))
    assert payload['source_dirty'] is True
    assert payload['source_digest'] == 'exact-dirty-source-digest'
    assert set(payload['checks']) == verify.REQUIRED
    assert len(calls) == 4
