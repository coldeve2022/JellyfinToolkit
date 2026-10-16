"""Qt-free AI connection discovery and explicit local Ollama service startup.

Nothing here downloads models, starts inference, or changes saved configuration.
"""

from __future__ import annotations

import errno
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from urllib import error, parse, request


@dataclass(frozen=True)
class Endpoint:
    models_url: str
    chat_url: str
    origin: str
    provider: str
    loopback: bool
    local_ollama: bool


def parse_endpoint(base_url):
    """Keep custom OpenAI prefixes; only explicit /api paths use native Ollama."""
    try:
        raw = str(base_url).strip()
        parts = parse.urlsplit(raw)
        port = parts.port
        host = parts.hostname or ""
        if (
            parts.scheme not in {"http", "https"}
            or not host
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or any(c.isspace() or ord(c) < 32 for c in raw)
        ):
            raise ValueError()
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host.lower().rstrip(".") == "localhost"
        path = parts.path.rstrip("/")
        local_ollama = (
            loopback
            and parts.scheme == "http"
            and port == 11434
            and path in {"", "/v1", "/v1/models", "/api", "/api/tags"}
        )
        origin = parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        native = path.endswith("/api/tags") or (port == 11434 and path in {"", "/api"})
        if native:
            prefix = path.removesuffix("/tags").removesuffix("/api")
            models, chat = prefix + "/api/tags", prefix + "/v1/chat/completions"
        else:
            prefix = path.removesuffix("/chat/completions").removesuffix("/models") or "/v1"
            models, chat = prefix + "/models", prefix + "/chat/completions"
        return Endpoint(
            origin + models,
            origin + chat,
            origin,
            "ollama" if native else "openai",
            loopback,
            local_ollama,
        )
    except (ValueError, TypeError):
        raise ValueError(
            "接口地址无效：请填写 http(s) 地址，不要在地址中放账号、密钥或查询参数。"
        ) from None


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a supplied API key to an unreviewed redirect target.
        return None


def request_opener(target):
    handlers = [_NoRedirect()]
    if target.loopback:
        handlers.append(request.ProxyHandler({}))
    return request.build_opener(*handlers)


def _failure(kind, message, **fields):
    return dict(
        ok=False, models=[], error=message, kind=kind, cancelled=kind == "cancelled", **fields
    )


def _diagnostic(exc, endpoint):
    if isinstance(exc, error.HTTPError):
        if exc.code in (401, 403):
            return _failure(
                "authentication",
                "服务已响应，但鉴权失败；请检查 API Key 和访问权限。",
                http_status=exc.code,
            )
        return _failure(
            "http_error",
            f"服务返回 HTTP {exc.code}；请检查接口路径、服务类型和访问设置。",
            http_status=exc.code,
        )
    reason = exc.reason if isinstance(exc, error.URLError) else exc
    if (
        isinstance(reason, ConnectionRefusedError)
        or getattr(reason, "errno", None) in (errno.ECONNREFUSED, 10061)
        or getattr(reason, "winerror", None) == 10061
    ):
        return _failure(
            "connection_refused",
            "连接被拒绝：目标地址/端口当前不接受连接。请检查服务是否启动及端口设置；这不表示模型未下载。",
        )
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return _failure(
            "timeout",
            "连接或响应超时：尚未确认服务在线。请检查服务状态、地址和防火墙；超时不能直接判为 Ollama 未启动。",
        )
    return _failure(
        "connection_error",
        "无法连接 AI 接口，请检查服务地址、网络和代理设置。"
        + ("本机请求已绕过环境代理。" if endpoint.loopback else ""),
    )


def discover_models(base_url, key="", should_stop=None, timeout=3, opener=None):
    stopped = should_stop or (lambda: False)
    try:
        endpoint = parse_endpoint(base_url)
    except ValueError as exc:
        return _failure("invalid_url", str(exc))
    if stopped():
        return _failure("cancelled", "已取消检测。")
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = request.Request(endpoint.models_url, headers=headers)
    result = _request_json(endpoint, req, timeout, stopped, opener)
    if not result["ok"]:
        return result
    payload = result["value"]
    field, name = ("models", "name") if endpoint.provider == "ollama" else ("data", "id")
    rows = payload.get(field) if isinstance(payload, dict) else None
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or not isinstance(row.get(name), str) or not row[name].strip()
        for row in rows
    ):
        return _failure(
            "response_format",
            "服务已响应，但模型列表格式不符。请确认使用 OpenAI /v1/models 或 Ollama /api/tags 接口。",
        )
    names = list(dict.fromkeys(row[name].strip() for row in rows))
    return dict(
        ok=True,
        models=names,
        error="",
        kind="ok",
        cancelled=False,
        endpoint=endpoint.models_url,
        provider=endpoint.provider,
    )


def _release_http_error(exc):
    """释放 HTTP 错误响应占用的连接，且不让它盖掉真正的诊断结论。

    这里不能直接 ``exc.close()``：Python 3.9 的 ``HTTPError`` 经由
    ``urllib.response.addinfourl`` 继承 ``tempfile._TemporaryFileWrapper``，
    后者的 ``close()`` 需要 ``self.file``，而 ``HTTPError.__init__`` 从不设置它 ——
    于是未命中的属性查找会掉进 ``__getattr__`` 并抛 ``KeyError: 'file'``，
    把可信的「HTTP 401 鉴权失败」诊断变成「连接错误」。3.11 起不再继承该基类，
    所以只在 3.9 上暴露。
    """
    try:
        exc.close()
    except Exception:
        # 关闭只影响连接复用；诊断结论已经拿到，不能被它覆盖。
        pass


def _request_json(endpoint, req, timeout, stopped, opener=None):
    deadline = time.monotonic() + max(0.1, timeout)
    try:
        connection = opener or request_opener(endpoint)
        chunks, size = [], 0
        with connection.open(req, timeout=max(0.1, timeout)) as response:
            reader = getattr(response, "read1", response.read)
            while True:
                if stopped():
                    return _failure("cancelled", "已取消检测。")
                if time.monotonic() >= deadline:
                    raise TimeoutError()
                chunk = reader(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > 4 * 1024 * 1024:
                    raise ValueError()
        if stopped():
            return _failure("cancelled", "已取消检测。")
        payload = json.loads(b"".join(chunks).decode("utf-8-sig"))
        return dict(ok=True, value=payload)
    except (error.URLError, OSError) as exc:
        if isinstance(exc, error.HTTPError):
            _release_http_error(exc)
        if stopped():
            return _failure("cancelled", "已取消检测。")
        return _diagnostic(exc, endpoint)
    except (ValueError, UnicodeError, TypeError):
        return _failure(
            "response_format", "服务已响应，但返回内容不是完整的 JSON。请检查接口类型及服务状态。"
        )
    except Exception:
        # Responses and exception messages can contain credentials. Do not echo either.
        return _failure("connection_error", "AI 接口检测未完成，请检查连接设置后重试。")


def chat_completion(base_url, key, model, messages, timeout=180):
    """Use the exact same endpoint, loopback routing and safe diagnostics as discovery."""
    endpoint = parse_endpoint(base_url)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    payload = {"model": model, "messages": messages, "temperature": 0.2}
    req = request.Request(
        endpoint.chat_url, data=json.dumps(payload).encode("utf-8"), headers=headers
    )
    result = _request_json(endpoint, req, timeout, lambda: False)
    if not result["ok"]:
        raise ValueError(result["error"]) from None
    try:
        text = result["value"]["choices"][0]["message"]["content"]
        if not isinstance(text, str):
            raise ValueError()
    except (KeyError, IndexError, TypeError, ValueError):
        raise ValueError("模型未返回可解析文本，请检查 OpenAI 兼容接口与模型支持。") from None
    return text


def find_ollama():
    candidates = []
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA")
        if root:
            candidates.append(Path(root) / "Programs" / "Ollama" / "ollama.exe")
    found = shutil.which("ollama")
    if found:
        candidates.append(Path(found))
    return next((path for path in candidates if path.is_file()), None)


def can_start_ollama(base_url):
    try:
        return parse_endpoint(base_url).local_ollama and find_ollama() is not None
    except ValueError:
        return False


def _listening(endpoint):
    parts = parse.urlsplit(endpoint.origin)
    try:
        with socket.create_connection((parts.hostname, parts.port), timeout=0.2):
            return True
    except OSError:
        return False


def _spawn_ollama(executable, endpoint):
    import config

    directory = Path(config.DATA_DIR) / "local_ai"
    directory.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, OLLAMA_HOST=endpoint.origin)
    options = {"env": env, "stdin": subprocess.DEVNULL, "cwd": str(executable.parent)}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    with (directory / "ollama-startup.log").open("ab") as log:
        return subprocess.Popen([str(executable), "serve"], stdout=log, stderr=log, **options)


_START_LOCK = threading.Lock()
_STARTED = {}


def start_and_detect(base_url, key="", should_stop=None, timeout=15):
    """Explicit action: start the installed local service, then wait without loading models."""
    stopped = should_stop or (lambda: False)
    try:
        target = parse_endpoint(base_url)
    except ValueError as exc:
        return _failure("invalid_url", str(exc))
    if not target.local_ollama:
        return _failure(
            "start_not_allowed",
            "仅支持启动本机 http://localhost:11434 的 Ollama；其它服务请使用其自身启动方式。",
        )
    if stopped():
        return _failure("cancelled", "已取消，未启动服务。")
    if not _START_LOCK.acquire(blocking=False):
        return _failure("starting", "本机 Ollama 正在启动或检测，请等待当前任务完成。")
    process = None
    try:
        result = discover_models(base_url, key, stopped, timeout=0.7)
        if (
            result["ok"]
            or result.get("cancelled")
            or result["kind"] not in {"connection_refused", "timeout", "connection_error"}
        ):
            return result
        executable = find_ollama()
        if not executable:
            return _failure(
                "not_installed", "未找到本机 Ollama。请先安装或手动启动；本工具不会下载程序或模型。"
            )
        if stopped():
            return _failure("cancelled", "已取消，未启动服务。")
        process = _STARTED.get(11434)
        if process is None or process.poll() is not None:
            if _listening(target):
                return _failure(
                    "service_unresponsive",
                    "端口可连接，但模型接口没有正常响应。请检查当前服务；未启动其它服务。",
                )
            if stopped():
                return _failure("cancelled", "已取消，未启动服务。")
            process = _spawn_ollama(executable, target)
            _STARTED[11434] = process
        deadline = time.monotonic() + max(0, timeout)
        while time.monotonic() < deadline:
            if stopped():
                return _failure(
                    "cancelled", "已取消检测；已启动的 Ollama 保持后台运行。", pid=process.pid
                )
            result = discover_models(base_url, key, stopped, timeout=0.7)
            if result["ok"]:
                return dict(result, started=True, pid=process.pid)
            if process.poll() is not None:
                return _failure(
                    "startup_failed",
                    "Ollama 已退出，启动未完成。请查看应用数据目录 local_ai/ollama-startup.log。",
                    pid=process.pid,
                )
            if result["kind"] not in {"connection_refused", "timeout", "connection_error"}:
                return result
            for _ in range(5):
                if stopped():
                    break
                time.sleep(0.05)
        return _failure(
            "startup_timeout",
            "已请求启动 Ollama，但模型接口尚不可达。请检查端口、防火墙及 local_ai/ollama-startup.log，再点击检测重试。",
            pid=process.pid,
        )
    except Exception:
        return _failure(
            "startup_failed",
            "无法启动本机 Ollama，请检查程序权限及 local_ai/ollama-startup.log；没有下载模型。",
        )
    finally:
        _START_LOCK.release()
