"""Tokengine OAuth 登录（本地回环回调版）。

桌面端由 Electron 主进程完成同一套流程（``desktop-shell/desktop/auth/``）；
手机端没有桌面壳，App 用 PRoot 起一个一次性 Python 进程执行本模块：App
负责把授权页交给系统浏览器，模块在 ``127.0.0.1:<随机端口>/authorize``
接收回调，换码 → 拉 userinfo（拿 ``ai_token`` 业务令牌）→ 拉中继
``/v1/models`` → 写入 model catalog。

命令行协议（App 逐行读取 stdout）：

    AUTHORIZE <url>        授权页地址，App 用系统浏览器打开
    OK <count> <relay>     登录成功，catalog 已更新
    ERROR <message>        失败原因

与桌面端保持一致的契约：客户端 ``edubuddy-desktop``、scope ``openid relay``、
PKCE S256、回调路径 ``/authorize``、业务令牌只来自 userinfo 的 ``ai_token``。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import ssl
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from deeptutor.services.path_service import get_path_service

from .tokengine_catalog import ensure_tokengine_catalog

OAUTH_BASE = (os.environ.get("TOKENGINE_OAUTH_BASE") or "https://tokengine.hanyoai.com").rstrip("/")
CLIENT_ID = os.environ.get("TOKENGINE_CLIENT_ID", "edubuddy-desktop")
SCOPE = os.environ.get("TOKENGINE_SCOPE", "openid relay")
CALLBACK_PATH = "/authorize"
LOGIN_TIMEOUT = int(os.environ.get("TOKENGINE_LOGIN_TIMEOUT", "900"))

_RELAY_FIELDS = ("relay_base", "base_url", "api_base", "api_endpoint", "endpoint", "relay_url", "domain")


def _ssl_context() -> ssl.SSLContext:
    """优先用 certifi 的 CA 包（容器里系统证书不一定齐）。"""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()


def _unwrap(payload: Any) -> dict[str, Any]:
    """剥掉网关常见的 ``{"success":true,"data":{...}}`` 外壳。"""
    if not isinstance(payload, dict):
        return {}
    if "access_token" in payload or "ai_token" in payload:
        return payload
    inner = payload.get("data")
    return inner if isinstance(inner, dict) else payload


def _post_form(url: str, data: dict[str, str], timeout: float = 20.0) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as resp:
        text = resp.read().decode("utf-8")
    return _unwrap(json.loads(text)) if text else {}


def _get_json(url: str, token: str, timeout: float = 20.0) -> Any:
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as resp:
        text = resp.read().decode("utf-8")
    return json.loads(text) if text else {}


def machine_id() -> str:
    """稳定的设备标识（桌面端同样在 authorize 里携带）。"""
    path = get_path_service().get_settings_dir() / "tokengine_machine_id.txt"
    try:
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
    except OSError:
        pass
    value = secrets.token_hex(16)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    except OSError:
        pass
    return value


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def normalize_relay(value: str) -> str:
    text = (value or "").strip().rstrip("/")
    if not text:
        return ""
    if not text.startswith(("http://", "https://")):
        text = "https://" + text
    if urllib.parse.urlparse(text).path in ("", "/"):
        text += "/v1"
    return text


def fetch_relay_models(relay_base: str, token: str) -> tuple[list[str], dict[str, int], dict[str, int]]:
    payload = _get_json(f"{relay_base.rstrip('/')}/models", token)
    items = payload if isinstance(payload, list) else None
    if isinstance(payload, dict):
        candidate = payload.get("data", payload.get("models"))
        items = candidate if isinstance(candidate, list) else None
    if items is None:
        raise RuntimeError("模型列表响应结构异常（缺少 data 数组）")

    names: list[str] = []
    types: dict[str, int] = {}
    windows: dict[str, int] = {}
    for item in items:
        if isinstance(item, str):
            name, model_type = item.strip(), None
        elif isinstance(item, dict):
            name = str(item.get("id") or item.get("model") or item.get("model_name") or "").strip()
            model_type = item.get("model_type")
        else:
            continue
        if not name:
            continue
        if name not in types and name not in names:
            names.append(name)
        if isinstance(model_type, (int, float)) and not isinstance(model_type, bool):
            types[name] = int(model_type)
        if isinstance(item, dict):
            window = item.get("max_model_len")
            if isinstance(window, int) and not isinstance(window, bool) and window > 0:
                windows[name] = window
    if not names:
        raise RuntimeError("模型列表为空")
    return names, types, windows


class _CallbackHandler(BaseHTTPRequestHandler):
    server_version = "ThinkBuddy Login/1.0"

    def log_message(self, *_args: Any) -> None:  # 静音 stderr
        return

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return
        query = urllib.parse.parse_qs(parsed.query)
        error = (query.get("error") or [""])[0]
        code = (query.get("code") or [""])[0]
        state = (query.get("state") or [""])[0]
        owner: "LoginFlow" = self.server.owner  # type: ignore[attr-defined]

        if error:
            owner.fail(f"平台返回错误：{error}")
            body = "登录失败，请回到 App 重试。"
        elif state != owner.state:
            owner.fail("回调 state 不匹配")
            body = "登录校验失败，请回到 App 重试。"
        elif not code:
            owner.fail("回调缺少 code")
            body = "登录失败，请回到 App 重试。"
        else:
            owner.code = code
            body = "登录成功，可以返回 ThinkBuddy。"
            owner.done.set()

        html = (
            "<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>ThinkBuddy</title><body style='font-family:system-ui;padding:48px;text-align:center'>"
            f"<h2 style='font-weight:600'>{body}</h2></body>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)


class LoginFlow:
    def __init__(self) -> None:
        self.state = secrets.token_urlsafe(24)
        self.verifier = secrets.token_urlsafe(64)
        self.code = ""
        self.error = ""
        self.done = threading.Event()
        self.server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.token_url = ""
        self.redirect_uri = ""

    def start(self) -> str:
        """启动回环监听，返回授权页 URL。"""
        self.server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
        self.server.owner = self  # type: ignore[attr-defined]
        port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

        redirect_uri = f"http://127.0.0.1:{port}{CALLBACK_PATH}"
        token_url = f"{OAUTH_BASE}/oauth/token"
        self.token_url = token_url
        self.redirect_uri = redirect_uri
        machine = machine_id()
        params = {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SCOPE,
            "state": self.state,
            "code_challenge": _b64url(hashlib.sha256(self.verifier.encode()).digest()),
            "code_challenge_method": "S256",
            "auth_type": "local",
            "login_channel": "native_desktop",
            "machine_id": machine,
            "x_machine_id": machine,
        }
        return f"{OAUTH_BASE}/oauth/authorize?{urllib.parse.urlencode(params)}"

    def fail(self, message: str) -> None:
        self.error = message
        self.done.set()

    def close(self) -> None:
        if self.server is not None:
            try:
                self.server.shutdown()
            except Exception:  # noqa: BLE001
                pass
            self.server = None

    def wait(self, timeout: float) -> None:
        self.done.wait(timeout)

    def complete(self) -> dict[str, Any]:
        """用回调拿到的 code 换凭证、拉模型、写 catalog。"""
        tokens = _post_form(
            self.token_url,
            {
                "grant_type": "authorization_code",
                "code": self.code,
                "redirect_uri": self.redirect_uri,
                "client_id": CLIENT_ID,
                "code_verifier": self.verifier,
                "machine_id": machine_id(),
            },
        )
        access_token = str(tokens.get("access_token") or "")
        if not access_token:
            raise RuntimeError("平台未返回 access_token")
        account = _unwrap(_get_json(f"{OAUTH_BASE}/oauth/userinfo", access_token))
        ai_token = str(account.get("ai_token") or "")
        if not ai_token:
            raise RuntimeError("平台未返回业务令牌（ai_token）")

        relay_raw = next(
            (
                str(account.get(field) or "").strip()
                for field in _RELAY_FIELDS
                if str(account.get(field) or "").strip()
            ),
            "",
        )
        relay_base = normalize_relay(relay_raw) or f"{OAUTH_BASE}/v1"

        models, model_types, windows = fetch_relay_models(relay_base, ai_token)
        written = ensure_tokengine_catalog(
            api_key=ai_token,
            base_url=relay_base,
            models=models,
            model_types=model_types,
            model_context_windows=windows,
        )
        return {
            "models": written.get("models") or models,
            "relay_base": relay_base,
            "phone": str(account.get("phone") or ""),
            "path": written.get("path", ""),
        }


def main(argv: list[str] | None = None) -> int:
    del argv
    flow = LoginFlow()
    try:
        url = flow.start()
        print(f"AUTHORIZE {url}", flush=True)
        flow.wait(LOGIN_TIMEOUT)
        if flow.error:
            print(f"ERROR {flow.error}", flush=True)
            return 1
        if not flow.code:
            print("ERROR 授权超时", flush=True)
            return 1
        result = flow.complete()
        print(
            "OK {} {}".format(len(result["models"]), result["relay_base"]),
            flush=True,
        )
        if result.get("phone"):
            print(f"PHONE {result['phone']}", flush=True)
        print(f"CATALOG {result.get('path', '')}", flush=True)
        return 0
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        print(f"ERROR 平台返回 HTTP {exc.code} {detail}", flush=True)
        return 1
    except urllib.error.URLError as exc:
        print(f"ERROR 无法连接平台（{exc.reason}）", flush=True)
        return 1
    except Exception as exc:  # noqa: BLE001 - 单次登录的任何失败都要落到一行 ERROR
        print(f"ERROR {exc}", flush=True)
        return 1
    finally:
        flow.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
