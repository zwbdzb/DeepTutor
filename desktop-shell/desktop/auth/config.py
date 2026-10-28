"""ThinkBuddy 主站与 Tokengine 认证服务接入配置。

两个站点的语义分工：

* **ThinkBuddy 主站**（键 ``api_base``，默认 ``https://thinkbuddy.hanyoai.com``）：
  产品官网 / 邀请页 / 更新源 / 遥测上报等业务入口。
* **Tokengine**（键 ``oauth_base``，默认 ``https://tokengine.hanyoai.com``）：
  登录认证（OAuth）+ token（模型中继）服务。

端点解析优先级（高 → 低，两个站点一致）：

1. 环境变量（联调最快，无需改文件）
2. ``<ROOT>/endpoints.json``（打包版可外部覆盖，免重建就能指向本地平台）
3. 内置默认（当前兜底为生产地址；本地联调用 endpoints.json / 环境变量覆盖）

例外：更新源与遥测端点**纯硬编码派生**自主站 ``api_base``
（``<api_base>/update``、``<api_base>/v1/events``），不吃环境变量 / 文件覆盖。

``endpoints.json`` 示例（放在 ``%LOCALAPPDATA%\\ThinkBuddy\\endpoints.json``）::

    {
      "oauth_base": "http://127.0.0.1:3000",
      "relay_base": "http://127.0.0.1:3000/v1",
      "api_base": "http://127.0.0.1:8000"
    }

可覆写的键：``oauth_base`` / ``authorize_url`` / ``token_url`` / ``userinfo_url`` /
``revoke_url`` / ``relay_base`` / ``api_base``。

环境变量：Tokengine 侧为 ``TOKENGINE_`` + 对应键的大写形式（根地址为
``TOKENGINE_OAUTH_BASE``）；主站根地址为 ``THINKBUDDY_API_BASE``。
废弃键 ``thinkbuddy_website_url``（→ 改写为 ``api_base``）与 ``update_url`` /
``telemetry_url``（→ 随 ``api_base`` 自动派生）出现在 endpoints.json 时只告警、
不生效。
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("dt.auth.config")

# 内置默认平台地址。
#
# 当前构建兜底为**生产地址**（分发给别人的机器上没有 endpoints.json / 环境变量，
# 落到的就是这个兜底值——必须是收件人能访问的地址）。本地联调不要改这里，
# 用优先级更高的覆盖方式指回联调环境：
#   * 用户级覆盖文件 %LOCALAPPDATA%\ThinkBuddy\endpoints.json 写 "oauth_base"
#     （主站联调写 "api_base"）
#   * 或启动前设环境变量 TOKENGINE_OAUTH_BASE=http://127.0.0.1:3000
#     （主站联调设 THINKBUDDY_API_BASE=http://127.0.0.1:8000）
#
# 注意：``TOKENGINE_DEFAULT_OAUTH_BASE`` 是**运行时**在启动机器上读取的，
# 打包时设置它不会烘焙进 exe；要改「分发默认值」只能改下面这个兜底参数。
PROD_OAUTH_BASE = "https://tokengine.hanyoai.com"

DEFAULT_OAUTH_BASE = os.environ.get("TOKENGINE_DEFAULT_OAUTH_BASE", PROD_OAUTH_BASE)
# ThinkBuddy 主站根地址（官网/邀请页/更新源/遥测的派生基准）。
DEFAULT_API_BASE = "https://thinkbuddy.hanyoai.com"

# --------------------------------------------------------------------------- #
# 端点解析
# --------------------------------------------------------------------------- #
_TOKENGINE_ENDPOINT_KEYS = (
    "oauth_base",
    "authorize_url",
    "token_url",
    "userinfo_url",
    "revoke_url",
    "relay_base",
)
_CONFIG_KEYS = (*_TOKENGINE_ENDPOINT_KEYS, "api_base")


def _read_endpoints_file(root: Optional[Path]) -> dict[str, str]:
    """读取 <root>/endpoints.json（打包版的外部覆盖点）。缺失/损坏都静默回退。"""
    if root is None:
        return {}
    path = Path(root) / "endpoints.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("endpoints.json 解析失败，忽略：%s", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    # 废弃键迁移提示（只告警不读值）：
    #   thinkbuddy_website_url -> 改写为 "api_base"（主站根地址）
    #   update_url / telemetry_url -> 已退出覆盖链，随 api_base 自动派生
    for legacy_key in ("thinkbuddy_website_url", "update_url", "telemetry_url"):
        if legacy_key in data:
            log.warning(
                "endpoints.json 含废弃键 %r（已忽略）。迁移路径："
                "thinkbuddy_website_url -> api_base；update_url/telemetry_url "
                "随 api_base 自动派生，不再支持单独覆盖。",
                legacy_key,
            )
    out: dict[str, str] = {}
    for key in _CONFIG_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    if out:
        log.info("endpoints.json 覆盖生效：%s", sorted(out))
    return out


def endpoints(root: Optional[Path] = None) -> dict[str, str]:
    """解析最终生效的端点。所有值尾部无 ``/``。"""
    file_over = _read_endpoints_file(root)

    def pick(env_suffix: str, default: str) -> str:
        # env 值先 strip 再进 or 链：纯空白 env 视为未设置，穿透到 file/default
        # （与 thinkbuddy_api_base 的 candidate.strip() 语义保持一致）。
        env_value = (os.environ.get(f"TOKENGINE_{env_suffix.upper()}") or "").strip()
        value = env_value or file_over.get(env_suffix) or default
        return str(value).strip().rstrip("/")

    base = pick("oauth_base", DEFAULT_OAUTH_BASE)
    resolved = {
        "oauth_base": base,
        "authorize_url": pick("authorize_url", f"{base}/oauth/authorize"),
        "token_url": pick("token_url", f"{base}/oauth/token"),
        "userinfo_url": pick("userinfo_url", f"{base}/oauth/userinfo"),
        "revoke_url": pick("revoke_url", f"{base}/oauth/revoke"),
        "relay_base": pick("relay_base", f"{base}/v1"),
    }
    log.info("端点解析结果：oauth_base=%s authorize=%s relay=%s",
             resolved["oauth_base"], resolved["authorize_url"], resolved["relay_base"])
    return resolved


def thinkbuddy_api_base(root: Optional[Path] = None) -> str:
    """Resolve the ThinkBuddy main-site base URL for desktop requests.

    优先级：环境变量 ``THINKBUDDY_API_BASE`` > ``endpoints.json`` 的
    ``api_base`` > 内置默认 ``DEFAULT_API_BASE``。返回空串表示未配置
    （调用方自行回退内置生产地址）。
    """
    file_over = _read_endpoints_file(root)
    candidates = (
        os.environ.get("THINKBUDDY_API_BASE"),
        file_over.get("api_base"),
        DEFAULT_API_BASE,
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip().rstrip("/")
    return ""


def telemetry_url(root: Optional[Path] = None) -> str:
    """Resolve the telemetry ingest endpoint for the shell's direct reporter.

    纯硬编码派生：``<thinkbuddy_api_base(root)>/v1/events``，不读环境变量 /
    endpoints.json（2026-11 键值重定位：独立覆盖链退出，随主站域名走）。
    返回空串表示遥测关闭（reporter 直接丢弃事件）。
    """
    site = thinkbuddy_api_base(root)
    return f"{site}/v1/events" if site else ""


def update_base(root: Optional[Path] = None) -> str:
    """Resolve the update source base URL (latest.json + installers live here).

    纯硬编码派生：``<thinkbuddy_api_base(root)>/update``，不读环境变量 /
    endpoints.json（2026-11 键值重定位：独立覆盖链退出，随主站域名走）。
    返回空串表示没有可用的更新源（此时更新检查应降级为本地 mtime 探测）。
    """
    site = thinkbuddy_api_base(root)
    return f"{site}/update" if site else ""


def explicit_overrides(root: Optional[Path] = None) -> dict[str, str]:
    """收集**显式**配置的端点键值（环境变量 > endpoints.json）。

    与 :func:`endpoints` 的区别：这里只返回用户真正写下来的键，
    不含从 ``oauth_base`` 派生的默认值。用于区分「本地显式覆盖」与
    「内置默认」——显式覆盖在与平台宣告域名冲突时应当获胜。
    """
    file_over = _read_endpoints_file(root)
    out: dict[str, str] = {}
    for key in _TOKENGINE_ENDPOINT_KEYS:
        # env 值先 strip 再进 or 链：纯空白 env 视为未设置，穿透到 file
        # （与 endpoints().pick 同语义，避免两侧对空白 env 的判定不一致）。
        env_value = (os.environ.get(f"TOKENGINE_{key.upper()}") or "").strip()
        value = env_value or file_over.get(key) or ""
        if value.strip():
            out[key] = value.strip().rstrip("/")
    return out


# 模块级常量：环境变量 + 内置默认（不含 endpoints.json，因为它依赖 root）。
# 保留这些名字是为了向后兼容既有代码与测试；需要感知 endpoints.json 的调用方
# 请使用 endpoints(root)。
_EP = endpoints(root=None)

# Tokengine 认证+token 服务根地址（authorize/token/relay 等的派生基准）。
OAUTH_BASE = _EP["oauth_base"]
AUTHORIZE_URL = _EP["authorize_url"]
TOKEN_URL = _EP["token_url"]
USERINFO_URL = _EP["userinfo_url"]
REVOKE_URL = _EP["revoke_url"]
RELAY_BASE = _EP["relay_base"]

# --------------------------------------------------------------------------- #
# 客户端身份
# --------------------------------------------------------------------------- #
CLIENT_ID = os.environ.get("TOKENGINE_CLIENT_ID", "edubuddy-desktop")
SCOPE = os.environ.get("TOKENGINE_SCOPE", "openid relay")

# --------------------------------------------------------------------------- #
# userinfo 字段映射（平台侧命名差异都在这里兜底）
# --------------------------------------------------------------------------- #
# 平台 userinfo 的 "models" 字段名
USERINFO_MODELS_FIELD = os.environ.get("TOKENGINE_USERINFO_MODELS_FIELD", "models")
# 平台 userinfo 的账号显示字段
USERINFO_PHONE_FIELD = os.environ.get("TOKENGINE_USERINFO_PHONE_FIELD", "phone")
# 平台 userinfo 的业务令牌（AI token，sk-...）字段——契约：业务令牌由
# userinfo 下发，/oauth/token 只返回标准凭证字段。
USERINFO_AI_TOKEN_FIELD = os.environ.get(
    "TOKENGINE_USERINFO_AI_TOKEN_FIELD", "ai_token")

# 中继域名候选字段：按顺序取第一个非空值。
# 「登录后拉取域名」就是靠这里——平台在 userinfo 里返回哪个字段都能接住。
USERINFO_RELAY_FIELDS: list[str] = [
    f.strip()
    for f in os.environ.get(
        "TOKENGINE_USERINFO_RELAY_FIELDS",
        "relay_base,base_url,api_base,api_endpoint,endpoint,relay_url,domain",
    ).split(",")
    if f.strip()
]

# 平台返回的域名若不含路径，自动补 /v1（OpenAI 兼容中继的约定）。
RELAY_AUTO_V1 = os.environ.get("TOKENGINE_RELAY_AUTO_V1", "1").lower() not in (
    "0", "false", "no", "off",
)

# 登录单次等待上限（默认 15 分钟）
LOGIN_TIMEOUT = int(os.environ.get("TOKENGINE_LOGIN_TIMEOUT", "900"))

# 回调路径（桌面端本地服务），平台回调时必须带上 code + state
CALLBACK_PATH = "/authorize"

# 回环回调监听端口。
#   0  = 交给操作系统分配随机端口（RFC 8252 对原生应用的推荐做法）
#   >0 = 固定端口（排查期有用：平台侧日志/抓包里的回调地址稳定可复现）
#
# 实测（2026-09 对 127.0.0.1:3000）：平台对 redirect_uri 的策略是
# 「字面量 http://127.0.0.1:<任意端口>/<任意路径> 通配」，因此随机端口天然合规，
# 无需为了「地址注册」而固定端口。注意 localhost / [::1] / 无端口一律被拒。
CALLBACK_PORT = int(os.environ.get("TOKENGINE_CALLBACK_PORT", "0"))


# --------------------------------------------------------------------------- #
# 中继域名规整
# --------------------------------------------------------------------------- #
def normalize_relay(value: str) -> str:
    """把平台返回的域名/地址规整成可写入 model_catalog 的 OpenAI 兼容 base_url。

    * 缺 scheme 时补 ``https://``（``tokengine.hanyoai.com`` → ``https://...``）
    * 无路径时补 ``/v1``（可用 ``TOKENGINE_RELAY_AUTO_V1=0`` 关闭）
    * 已有路径（如 ``https://host/api/v1``）原样保留
    """
    text = (value or "").strip().rstrip("/")
    if not text:
        return ""
    if not text.startswith(("http://", "https://")):
        text = "https://" + text
    if RELAY_AUTO_V1:
        try:
            if urllib.parse.urlparse(text).path in ("", "/"):
                text = text + "/v1"
        except ValueError:
            pass
    return text


def pick_relay_from_userinfo(account: dict[str, Any]) -> str:
    """按 USERINFO_RELAY_FIELDS 顺序取平台返回的中继域名，取不到返回空串。"""
    if not isinstance(account, dict):
        return ""
    for field in USERINFO_RELAY_FIELDS:
        value = account.get(field)
        if isinstance(value, str) and value.strip():
            return normalize_relay(value)
    return ""


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def is_loopback(url: str) -> bool:
    """判断地址是否指向本机（联调/自托管场景）。"""
    try:
        host = urllib.parse.urlparse(
            url if "//" in url else "//" + url
        ).hostname or ""
    except ValueError:
        return False
    return host.lower() in _LOOPBACK_HOSTS


def resolve_relay(
    api_base: str,
    userinfo: Optional[dict[str, Any]] = None,
    fallback: str = "",
    local_override: str = "",
) -> tuple[str, str]:
    """决定最终生效的中继域名，返回 ``(relay_base, source)``。

    优先级（高 → 低）：

    1. **本地显式覆盖**：``endpoints.json`` / 环境变量里写明的 ``relay_base``。
       这是用户/运维最明确的意图声明。
    2. **``api_base`` 是回环地址**（127.0.0.1/localhost）时，以它为准并派生
       ``<api_base>/v1``。理由：联调时显式配了回环地址就应当被尊重，
       不能被任何远端宣告悄悄指回线上。
    3. **userinfo 显式字段**（平台直接下发中继地址，这里接得住）。
    4. ``fallback``（本地配置的 relay_base）。

    注意：客户端**不读取**平台 ``/api/status`` 的 ``server_address``——
    域名只来自内置默认 + 本地显式覆盖 + userinfo 下发，行为完全可预测
    （2026-09 决策：平台宣告域名曾把联调环境指向错误上游）。

    ``source`` 取值：``local-override`` / ``local-loopback`` / ``userinfo`` /
    ``local-config``。
    """
    if local_override:
        return local_override, "local-override"

    if api_base and is_loopback(api_base):
        derived = normalize_relay(api_base)
        if derived:
            return derived, "local-loopback"

    pulled = pick_relay_from_userinfo(userinfo or {})
    if pulled:
        return pulled, "userinfo"

    if fallback:
        return fallback, "local-config"
    return normalize_relay(api_base), "local-config" if api_base else "unset"
