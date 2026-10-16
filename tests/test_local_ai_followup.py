"""Local AI discovery/start boundaries; no real models, keys, or user configuration."""

import errno
import io
import json
import threading
import time
import urllib.error

import pytest


def client():
    from utils import local_ai

    return local_ai


class Reply(io.BytesIO):
    def __init__(self, value):
        super().__init__(value if isinstance(value, bytes) else json.dumps(value).encode())


class Transport:
    def __init__(self, value):
        self.value, self.requests = value, []

    def open(self, request, timeout):
        self.requests.append(request)
        if isinstance(self.value, Exception):
            raise self.value
        return Reply(self.value)


@pytest.mark.parametrize(
    "address, endpoint, chat",
    [
        (
            "http://localhost:11434/v1/",
            "http://localhost:11434/v1/models",
            "http://localhost:11434/v1/chat/completions",
        ),
        (
            "http://127.0.0.1:11434",
            "http://127.0.0.1:11434/api/tags",
            "http://127.0.0.1:11434/v1/chat/completions",
        ),
        (
            "http://[::1]:11434/api/tags",
            "http://[::1]:11434/api/tags",
            "http://[::1]:11434/v1/chat/completions",
        ),
        (
            "https://example.invalid/custom/v1/",
            "https://example.invalid/custom/v1/models",
            "https://example.invalid/custom/v1/chat/completions",
        ),
        (
            "http://localhost:1234/v1/models",
            "http://localhost:1234/v1/models",
            "http://localhost:1234/v1/chat/completions",
        ),
        (
            "https://gateway.example/api",
            "https://gateway.example/api/models",
            "https://gateway.example/api/chat/completions",
        ),
    ],
)
def test_endpoint_normalization_preserves_custom_prefix(address, endpoint, chat):
    target = client().parse_endpoint(address)
    assert target.models_url == endpoint and target.chat_url == chat


@pytest.mark.parametrize(
    "address",
    [
        "file:///secret",
        "http://user:secret@localhost:11434/v1",
        "http://localhost:11434/v1?key=secret",
        "",
        "http://localhost:broken/v1",
    ],
)
def test_invalid_addresses_fail_without_echoing_secrets(address):
    result = client().discover_models(address)
    assert result["kind"] == "invalid_url" and "secret" not in result["error"]


@pytest.mark.parametrize(
    "address, payload, names",
    [
        ("http://localhost:11434/v1", {"data": [{"id": "custom-model"}]}, ["custom-model"]),
        ("http://localhost:11434/api", {"models": [{"name": "qwen:8b"}]}, ["qwen:8b"]),
        ("https://example.invalid/prefix/v1", {"data": []}, []),
    ],
)
def test_discovery_uses_protocol_schema_and_supplied_key(address, payload, names):
    transport = Transport(payload)
    result = client().discover_models(address, key="test-secret", opener=transport)
    assert result["ok"] and result["models"] == names
    assert transport.requests[0].get_header("Authorization") == "Bearer test-secret"
    assert "test-secret" not in str(result)


def _refused_error():
    return urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, "secret"))


def _timeout_error():
    return urllib.error.URLError(TimeoutError("secret"))


def _authentication_error():
    return urllib.error.HTTPError("http://localhost", 401, "secret", {}, None)


def _http_error():
    return urllib.error.HTTPError("http://localhost", 404, "secret", {}, None)


# 参数表里只放**工厂**，不放异常实例：urllib 的 HTTPError（经由 addinfourl）
# 在 Python 3.9 上继承 tempfile._TemporaryFileWrapper，pytest 生成用例 ID 时读
# __name__ 会掉进它的 __getattr__（KeyError: 'file'），整个模块在**收集期**就中断。
# 换成长度/内容确定的字符串用例名后，任何受支持的 Python 都能正常收集。
FAILURE_CASES = {
    "connection_refused": (_refused_error, "connection_refused"),
    "timeout": (_timeout_error, "timeout"),
    "authentication": (_authentication_error, "authentication"),
    "http_error": (_http_error, "http_error"),
    "html_body": (lambda: b"<html>secret</html>", "response_format"),
    "empty_choices": (lambda: {"choices": []}, "response_format"),
}


@pytest.mark.parametrize("case", sorted(FAILURE_CASES))
def test_diagnostics_distinguish_refusal_timeout_http_auth_and_format(case):
    factory, kind = FAILURE_CASES[case]
    result = client().discover_models(
        "http://localhost:11434/v1", key="test-secret", opener=Transport(factory())
    )
    assert not result["ok"] and result["kind"] == kind
    assert "secret" not in str(result)


def test_loopback_connections_bypass_environment_proxy(monkeypatch):
    import urllib.request

    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": "http://proxy.invalid:9999"})
    local = client().request_opener(client().parse_endpoint("http://127.0.0.1:11434/v1"))
    remote = client().request_opener(client().parse_endpoint("http://example.invalid/v1"))
    assert not any(isinstance(h, urllib.request.ProxyHandler) and h.proxies for h in local.handlers)
    assert any(isinstance(h, urllib.request.ProxyHandler) and h.proxies for h in remote.handlers)


@pytest.mark.parametrize(
    "address",
    [
        "http://example.invalid:11434/v1",
        "http://localhost:1234/v1",
        "http://localhost:11434/custom/v1",
        "https://localhost:11434/v1",
    ],
)
def test_other_services_never_start_ollama(address, monkeypatch):
    module = client()
    monkeypatch.setattr(
        module, "_spawn_ollama", lambda *a: pytest.fail("must not start another provider")
    )
    result = module.start_and_detect(address)
    assert not result["ok"] and result["kind"] == "start_not_allowed"


def test_cancelled_discovery_does_not_contact_service():
    transport = Transport({"data": []})
    result = client().discover_models(
        "http://localhost:11434/v1", should_stop=lambda: True, opener=transport
    )
    assert result["cancelled"] and transport.requests == []


def test_start_is_idempotent_for_online_service(monkeypatch):
    module = client()
    monkeypatch.setattr(
        module,
        "discover_models",
        lambda *a, **k: dict(ok=True, models=["ready"], error="", kind="ok"),
    )
    monkeypatch.setattr(module, "_spawn_ollama", lambda *a: pytest.fail("already online"))
    assert module.start_and_detect("http://localhost:11434/v1")["ok"]


def test_startup_duplicate_requests_launch_only_one_process(tmp_path, monkeypatch):
    module = client()
    monkeypatch.setattr(module, "_STARTED", {})
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"test-only executable")
    monkeypatch.setattr(module, "find_ollama", lambda: executable)
    monkeypatch.setattr(module, "_listening", lambda *a: False)
    entered, release = threading.Event(), threading.Event()
    launches = []
    answers = []

    def detect(*args, **kwargs):
        if launches:
            return dict(ok=True, models=["ready"], error="", kind="ok")
        return dict(ok=False, models=[], error="unreachable", kind="connection_refused")

    def spawn(*args):
        launches.append(args)
        entered.set()
        release.wait(2)
        return type("Process", (), {"pid": 12345, "poll": lambda self: None})()

    monkeypatch.setattr(module, "discover_models", detect)
    monkeypatch.setattr(module, "_spawn_ollama", spawn)
    thread = threading.Thread(
        target=lambda: answers.append(module.start_and_detect("http://localhost:11434/v1"))
    )
    thread.start()
    try:
        assert entered.wait(2)
        duplicate = module.start_and_detect("http://localhost:11434/v1")
        assert duplicate["kind"] == "starting"
    finally:
        release.set()
        thread.join(3)
    assert len(launches) == 1 and answers[0]["ok"]


def test_settings_detection_uses_unsaved_fields_without_blocking(qapp, qtbot, monkeypatch):
    from config import ToolkitConfig
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QMessageBox
    from ui.pages.settings import SettingsPage
    from ui.services import busy

    module = client()
    entered, release = threading.Event(), threading.Event()
    captured = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok)

    def detect(address, key="", should_stop=None, **kwargs):
        captured.append((address, key))
        entered.set()
        while not release.wait(0.01):
            if should_stop():
                return dict(ok=False, cancelled=True, error="已取消", kind="cancelled", models=[])
        return dict(ok=True, models=["custom-model"], error="", kind="ok")

    monkeypatch.setattr(module, "discover_models", detect)
    page = SettingsPage(ToolkitConfig())
    qtbot.addWidget(page)
    page.input_ai_url.setText("http://localhost:1234/custom/v1")
    page.input_ai_key.setText("test-key")
    ticks = []
    QTimer.singleShot(0, lambda: ticks.append(True))
    before = time.monotonic()
    page._detect_models()
    page._detect_models()
    try:
        qtbot.waitUntil(entered.is_set)
        qtbot.waitUntil(lambda: bool(ticks))
        assert time.monotonic() - before < 1 and len(captured) == 1
        assert not page.btn_ai_start.isEnabled()
        page.shutdown()
    finally:
        release.set()
        qtbot.waitUntil(lambda: not busy(page), timeout=4000)
    assert captured == [("http://localhost:1234/custom/v1", "test-key")]
    assert page.cfg.ai_base_url != page.input_ai_url.text()


def test_cancel_during_response_drops_models():
    module = client()
    stopped = []

    class InterruptedReply(Reply):
        def read1(self, size):
            stopped.append(True)
            return super().read1(size)

    class InterruptedTransport:
        def open(self, *args, **kwargs):
            return InterruptedReply({"data": [{"id": "must-not-appear"}]})

    result = module.discover_models(
        "http://localhost:11434/v1",
        should_stop=lambda: bool(stopped),
        opener=InterruptedTransport(),
    )
    assert result["cancelled"] and result["models"] == []


def test_cancel_during_connection_timeout_reports_cancelled():
    stopped = []

    class InterruptedTransport:
        def open(self, *args, **kwargs):
            stopped.append(True)
            raise TimeoutError()

    result = client().discover_models(
        "http://localhost:11434/v1",
        should_stop=lambda: bool(stopped),
        opener=InterruptedTransport(),
    )
    assert result["cancelled"] and result["kind"] == "cancelled"


def test_settings_keeps_unavailable_edited_model_and_explains_selection(qapp, qtbot, monkeypatch):
    from config import ToolkitConfig
    from PySide6.QtWidgets import QMessageBox
    from ui.pages.settings import SettingsPage

    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    page = SettingsPage(ToolkitConfig())
    qtbot.addWidget(page)
    page.input_ai_model.setText("edited-unavailable-model")
    page._show_models(["available-model"])
    assert page.input_ai_model.text() == "edited-unavailable-model"
    assert "不在" in page.ai_status.text() and "available-model" in page.ai_status.text()


def test_live_but_unresponsive_port_never_spawns_another_service(tmp_path, monkeypatch):
    module = client()
    monkeypatch.setattr(module, "_STARTED", {})
    monkeypatch.setattr(module, "find_ollama", lambda: tmp_path / "ollama.exe")
    monkeypatch.setattr(module, "_listening", lambda endpoint: True)
    monkeypatch.setattr(
        module, "discover_models", lambda *a, **k: dict(ok=False, kind="timeout", error="timeout")
    )
    monkeypatch.setattr(module, "_spawn_ollama", lambda *a: pytest.fail("port already occupied"))
    assert module.start_and_detect("http://localhost:11434/v1")["kind"] == "service_unresponsive"


def test_cancelled_poll_keeps_owned_service_and_retry_does_not_duplicate(tmp_path, monkeypatch):
    module = client()
    monkeypatch.setattr(module, "_STARTED", {})
    monkeypatch.setattr(module, "find_ollama", lambda: tmp_path / "ollama.exe")
    monkeypatch.setattr(module, "_listening", lambda endpoint: False)
    monkeypatch.setattr(
        module,
        "discover_models",
        lambda *a, **k: dict(ok=False, kind="connection_refused", error="refused"),
    )
    launches, cancelled = [], []
    process = type("Process", (), {"pid": 12345, "poll": lambda self: None})()

    def spawn(*args):
        launches.append(args)
        cancelled.append(True)
        return process

    monkeypatch.setattr(module, "_spawn_ollama", spawn)
    result = module.start_and_detect(
        "http://localhost:11434/v1", should_stop=lambda: bool(cancelled)
    )
    assert result["cancelled"] and result["pid"] == process.pid
    retry = module.start_and_detect("http://localhost:11434/v1", timeout=0)
    assert retry["kind"] == "startup_timeout" and len(launches) == 1


@pytest.mark.parametrize(
    "body",
    [b"bad JSON with test-secret", {"choices": []}, {"choices": [{"message": {"content": None}}]}],
)
def test_bad_inference_response_preserves_safe_failure_contract(body, monkeypatch):
    from workers.ai import query_llm

    module = client()
    monkeypatch.setattr(module, "request_opener", lambda endpoint: Transport(body))
    with pytest.raises(ValueError) as caught:
        query_llm("http://localhost:11434/v1", "test-secret", "model", [])
    assert "test-secret" not in str(caught.value)


@pytest.mark.parametrize("kind", ["authentication", "http_error", "response_format"])
def test_existing_http_service_errors_never_launch_another_server(monkeypatch, kind):
    module = client()
    monkeypatch.setattr(
        module,
        "discover_models",
        lambda *a, **k: dict(ok=False, kind=kind, error="controlled", models=[]),
    )
    monkeypatch.setattr(
        module, "_spawn_ollama", lambda *a: pytest.fail("service already responded")
    )
    assert module.start_and_detect("http://localhost:11434/v1")["kind"] == kind


def test_cancelled_start_does_not_launch_service(monkeypatch):
    module = client()
    monkeypatch.setattr(module, "_spawn_ollama", lambda *a: pytest.fail("cancelled startup"))
    assert module.start_and_detect("http://localhost:11434/v1", should_stop=lambda: True)[
        "cancelled"
    ]


def test_service_spawn_is_hidden_pinned_to_loopback_and_never_pulls(tmp_path, monkeypatch):
    module = client()
    calls = []
    executable = tmp_path / "Ollama with spaces" / "ollama.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"fake")
    monkeypatch.setenv("OLLAMA_HOST", "http://0.0.0.0:11434")
    monkeypatch.setattr(
        module.subprocess, "Popen", lambda *a, **k: calls.append((a, k)) or object()
    )
    endpoint = module.parse_endpoint("http://127.0.0.1:11434/v1")
    module._spawn_ollama(executable, endpoint)
    args, options = calls[0]
    assert args[0] == [str(executable), "serve"] and not options.get("shell")
    assert options["env"]["OLLAMA_HOST"] == "http://127.0.0.1:11434"
    if module.sys.platform == "win32":
        assert options["creationflags"] & module.subprocess.CREATE_NO_WINDOW
    else:
        assert options["start_new_session"]


@pytest.mark.parametrize(
    "address", ["http://127.0.0.1:11434/api", "http://localhost:1234/custom/v1/"]
)
def test_inference_uses_same_endpoint_transport_and_key(address, monkeypatch):
    from workers.ai import query_llm

    module = client()
    monkeypatch.setattr(
        module.request, "urlopen", lambda *a, **k: pytest.fail("must use shared transport")
    )
    transport = Transport({"choices": [{"message": {"content": "answer"}}]})
    monkeypatch.setattr(module, "request_opener", lambda endpoint: transport)
    assert (
        query_llm(address, "test-secret", "model", [{"role": "user", "content": "question"}])
        == "answer"
    )
    req = transport.requests[0]
    assert req.full_url == module.parse_endpoint(address).chat_url
    assert req.get_header("Authorization") == "Bearer test-secret"
    assert json.loads(req.data)["model"] == "model"


def test_inference_auth_failure_is_safe_and_distinct(monkeypatch):
    from workers.ai import query_llm

    module = client()
    monkeypatch.setattr(
        module.request, "urlopen", lambda *a, **k: pytest.fail("must use shared transport")
    )
    transport = Transport(
        urllib.error.HTTPError("http://localhost/?secret", 401, "test-secret", {}, None)
    )
    monkeypatch.setattr(module, "request_opener", lambda endpoint: transport)
    with pytest.raises(ValueError, match="鉴权") as caught:
        query_llm("http://localhost:11434/v1", "test-secret", "model", [])
    assert "secret" not in str(caught.value)
