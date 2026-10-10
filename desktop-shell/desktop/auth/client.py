"""Tokengine OAuth 端点客户端（纯 urllib，无第三方依赖）。"""
from __future__ import annotations

import functools
import json
import logging
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from . import config as cfg

log = logging.getLogger("dt.auth.client")


def _runtime_certifi_bundles() -> list[str]:
    """内嵌托管运行时自带 certifi 根库的路径（Windows/mac 布局不同）。"""
    try:
        # 延迟导入：runtime 只依赖标准库，且 manager 侧已是这个方向，无循环风险。
        from .. import runtime as _rt
    except Exception:  # noqa: BLE001
        return []
    try:
        base = _rt.select_runtime_base()
    except Exception:  # noqa: BLE001
        log.debug("select_runtime_base 失败，跳过运行时 certifi", exc_info=True)
        return []
    if not base:
        return []
    py_root = base / "python"
    candidates = [
        py_root / "Lib" / "site-packages" / "certifi" / "cacert.pem",  # Windows embeddable
    ]
    candidates.extend(
        py_root.glob("lib/python3*/site-packages/certifi/cacert.pem")  # macOS standalone
    )
    return [str(p) for p in candidates if p.is_file()]


@functools.lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext:
    """OAuth 请求专用 SSLContext。

    冻结打包后的桌面进程只信任 Windows/macOS 系统根库。在精简镜像或企业
    管控机器上，系统根库可能缺少站点链路上的根（实测 tokengine 的链为
    WoTrus DV → USERTrust RSA → AAA Certificate Services），而授权页在
    WebView2 里走 Chromium Root Store 仍可打开——于是表现为"浏览器登录
    正常、换码报 SELF_SIGNED_CERT_IN_CHAIN"。这里在系统库之上叠加
    certifi（Mozilla）根库，并允许通过环境变量追加企业 CA。
    """
    ctx = ssl.create_default_context()
    bundles: list[str] = []

    # 1) 进程内可直接导入的 certifi（开发态/已装环境）
    try:
        import certifi  # type: ignore

        path = certifi.where()
        ctx.load_verify_locations(path)
        bundles.append(path)
    except Exception:  # noqa: BLE001
        pass

    # 2) 内嵌托管运行时自带的 certifi（登录窗口出现时运行时必然已就绪）
    for path in _runtime_certifi_bundles():
        try:
            ctx.load_verify_locations(path)
            bundles.append(path)
        except OSError:
            log.debug("CA bundle 加载失败：%s", path, exc_info=True)

    # 3) 企业/运维显式下发的额外 CA（SSL 拦截型代理的根证书等）
    for env_name in ("DT_SSL_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        path = os.environ.get(env_name)
        if not path:
            continue
        try:
            ctx.load_verify_locations(path)
            bundles.append(f"{env_name}={path}")
        except OSError:
            log.warning("环境变量 %s 指定的 CA 加载失败：%s", env_name, path, exc_info=True)

    log.info("OAuth SSL 信任源：%s", " | ".join(bundles) or "仅系统根库")
    return ctx


class OAuthError(RuntimeError):
    def __init__(self, message: str, code: int = 0, payload: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


# 顶层只要出现这些键中任意一个，就认为响应已经是"扁平的"，不再下钻。
_FLAT_HINTS = (
    "token", "access_token", "refresh_token", "expires_in",
    "models", "ai_token", "phone", "balance",
    "relay_base", "base_url", "domain",
)


def unwrap(payload: Any) -> dict[str, Any]:
    """剥掉网关常见的 ``{"success":true,"data":{...}}`` 外壳。

    这个平台（new-api 系）惯于把业务数据包在 ``data`` 里。若不解包，
    ``models`` / ``phone`` / 域名等字段会全部取不到，表现为"登录成功但什么都没拉到"
    并且**没有任何报错**——静默失效最难查，所以在这一层统一兼容。

    规则：顶层已有我们认识的键 → 原样返回；否则 ``data`` 是 dict → 下钻一层。
    """
    if not isinstance(payload, dict):
        return {}
    if any(key in payload for key in _FLAT_HINTS):
        return payload
    inner = payload.get("data")
    if isinstance(inner, dict):
        return inner
    return payload


def _post_form(url: str, data: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as resp:
            text = resp.read().decode("utf-8")
            return unwrap(json.loads(text)) if text else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        payload = None
        try:
            payload = json.loads(detail)
        except (ValueError, TypeError):
            pass
        log.error("OAuth %s -> HTTP %s: %s", url, exc.code, detail[:300])
        raise OAuthError(f"平台返回错误（HTTP {exc.code}）", exc.code, payload) from exc
    except urllib.error.URLError as exc:
        log.error("OAuth network error: %s", exc)
        raise OAuthError(f"无法连接平台（{exc.reason}）") from exc


def _get_json(url: str, token: str, timeout: float = 10.0) -> dict[str, Any]:
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as resp:
            text = resp.read().decode("utf-8")
            return unwrap(json.loads(text)) if text else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        raise OAuthError(f"userinfo 失败（HTTP {exc.code}）：{detail[:200]}", exc.code) from exc
    except urllib.error.URLError as exc:
        raise OAuthError(f"userinfo 网络错误：{exc.reason}") from exc


class OAuthClient:
    def __init__(
        self,
        authorize_url: str = cfg.AUTHORIZE_URL,
        token_url: str = cfg.TOKEN_URL,
        userinfo_url: str = cfg.USERINFO_URL,
        revoke_url: str = cfg.REVOKE_URL,
        client_id: str = cfg.CLIENT_ID,
        scope: str = cfg.SCOPE,
    ) -> None:
        self.authorize_url = authorize_url
        self.token_url = token_url
        self.userinfo_url = userinfo_url
        self.revoke_url = revoke_url
        self.client_id = client_id
        self.scope = scope

    # -- URL 构造 -------------------------------------------------------- #
    def build_authorize_url(
        self,
        redirect_uri: str,
        code_challenge: str,
        state: str,
        machine_id: str,
        prompt: str = "",
    ) -> str:
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": self.scope,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "auth_type": "local",
            "login_channel": "native_desktop",
            "machine_id": machine_id,
            "x_machine_id": machine_id,
        }
        # prompt="login"（OIDC 语义：强制重新认证）：切换账号时携带，
        # 平台据此跳过「已有会话直接发码」，强制进授权页让用户选账号。
        # 旧版平台不识别该参数会忽略之，行为退化为原样（自动授权当前会话）。
        if prompt:
            params["prompt"] = prompt
        return f"{self.authorize_url}?{urllib.parse.urlencode(params)}"

    # -- token 交换 ------------------------------------------------------ #
    def exchange(self, code: str, code_verifier: str, redirect_uri: str,
                 machine_id: str) -> dict[str, Any]:
        return _post_form(self.token_url, {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self.client_id,
            "code_verifier": code_verifier,
            "machine_id": machine_id,
        })

    def refresh(self, refresh_token: str, machine_id: str) -> dict[str, Any]:
        return _post_form(self.token_url, {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client_id,
            "scope": self.scope,
            "machine_id": machine_id,
        })

    def revoke(self, token: str, machine_id: str) -> None:
        if not self.revoke_url:
            return
        try:
            _post_form(self.revoke_url, {"token": token, "machine_id": machine_id}, timeout=10)
        except OAuthError as exc:
            log.warning("revoke 未清理平台侧（可忽略）：%s", exc)

    # -- 用户信息 -------------------------------------------------------- #
    def userinfo(self, access_token: str) -> dict[str, Any]:
        return _get_json(self.userinfo_url, access_token)


def fetch_relay_models(
    relay_base: str, token: str, timeout: float = 10.0
) -> tuple[list[str], dict[str, int]]:
    """GET ``{relay_base}/models``——模型名单的**权威**来源。

    2026-09-24 起替代「userinfo.models 授权名单」成为唯一名单来源：
    中继（网关）的 /v1/models 每一项自带 ``model_type``
    （1=文生文 2=文生图 3=文生视频 4=重排序 5=向量），与平台模型管理
    后台的类型下拉一致，登录/刷新据此分流入库。

    返回 ``(names, model_types)``：与 ``ensure_tokengine_catalog`` 的
    ``(models, model_types)`` 入参同构，登录/刷新直接透传分流入库。

    任何失败（网络 / 非 200 / 空列表 / 结构异常）都抛
    :class:`OAuthError`。失败语义由调用方定：登录不阻断（模型列表保持
    现状），刷新报错返回且不写盘——这里不静默吞错，避免"名单悄悄变少"
    这类静默失效。
    """
    url = (relay_base or "").strip().rstrip("/") + "/models"
    if not (relay_base or "").strip():
        raise OAuthError("中继地址为空，无法拉取模型列表")
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as resp:
            text = resp.read().decode("utf-8")
            payload = json.loads(text) if text else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        raise OAuthError(
            f"模型列表拉取失败（HTTP {exc.code}）：{detail[:200]}", exc.code
        ) from exc
    except urllib.error.URLError as exc:
        raise OAuthError(f"模型列表网络错误：{exc.reason}") from exc
    except OSError as exc:  # 读超时/连接重置等不经 URLError 包装的套接字错误
        raise OAuthError(f"模型列表网络错误：{exc}") from exc
    except (ValueError, TypeError) as exc:
        raise OAuthError(f"模型列表响应不是合法 JSON：{exc}") from exc

    # OpenAI 兼容：{"object":"list","data":[{id,...},...]}；宽容兼容裸数组
    # 与 {"models":[...]} 两种变体。
    items = payload if isinstance(payload, list) else None
    if isinstance(payload, dict):
        candidate = payload.get("data", payload.get("models"))
        items = candidate if isinstance(candidate, list) else None
    if items is None:
        raise OAuthError("模型列表响应结构异常（缺少 data 数组）")

    names: list[str] = []
    types: dict[str, int] = {}
    for item in items:
        if isinstance(item, str):
            name = item.strip()
            model_type: Any = None
        elif isinstance(item, dict):
            name = str(
                item.get("id") or item.get("model") or item.get("model_name") or ""
            ).strip()
            model_type = item.get("model_type")
        else:
            continue
        if not name:
            continue
        if name not in types:
            names.append(name)
        if isinstance(model_type, (int, float)) and not isinstance(model_type, bool):
            types[name] = int(model_type)

    if not names:
        # 空名单视为异常：登录会保持模型列表现状、刷新直接报错不写盘，
        # 绝不让一次网关抖动把 catalog 里的模型清空（ensure 侧空 llm
        # 名单会清活动模型）。
        raise OAuthError("模型列表为空")
    return names, types
