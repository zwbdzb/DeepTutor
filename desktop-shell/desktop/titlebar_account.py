"""标题栏账号区：状态模型 + 同步线程（纯逻辑，不碰 WinForms）。

2026-09-23 起账号入口从「页面内悬浮按钮 + 自绘菜单」（ADR-002）迁移到
**原生标题栏**（ADR-004）：点击与菜单改由 WinForms 直接回调（见
``desktop/native_menu_backend.py`` 的 ``AccountChip``），不再往页面注入
按钮与事件槽。本模块只保留两块与页面无关的职责：

    1. chip_model()      —— 把 AuthManager.status() 渲染成标题栏账号区模型
                            （文案 / 悬停提示 / 下拉菜单内容）；
    2. AccountStatusSync —— 后台轮询状态、增量推给 chip，并在「登录成功 /
                            账号身份变化」跃迁时触发页面刷新（让应用重新
                            读取刚写入的模型目录）。

文案与菜单结构和旧版 inject.py 逐字平移，保证交互不回退：
    已登录      → 用户名（回退脱敏手机号），点击弹账号菜单
    已配置令牌  → 「已配置令牌」，点击直接发起登录（configured 不算登录）
    登录进行中  → 「等待浏览器…」，点击弹「重新发起登录」菜单
    未登录      → 「登录」，点击直接发起 OAuth
"""
from __future__ import annotations

import json
import logging
import re
import threading

log = logging.getLogger("dt.titlebar")

# 菜单动作名（与 main.py 的分发表对齐，便于一眼核对）
MENU_ACTIONS = ("switch", "platform", "refresh", "copy", "logout", "about")


# --------------------------------------------------------------------------- #
# 文案与脱敏                                                                    #
# --------------------------------------------------------------------------- #
def _mask_phone(phone: str) -> str:
    p = str(phone or "")
    if len(p) >= 7:
        return p[:3] + "****" + p[-4:]
    return p or "已登录"


def _display_name(acct: dict) -> str:
    """账号显示名：优先平台用户名（username），回退脱敏手机号。

    平台 userinfo 返回 username 字段（实测如 ``admin``），落库在
    ``account.raw``，经 AuthManager.status() 透出到 ``account.username``。
    若用户名本身就是未脱敏的 11 位手机号（有的平台这么存），强制打码，
    避免标题栏裸奔完整号码。
    """
    acct = acct or {}
    name = str(acct.get("username") or "").strip()
    if re.fullmatch(r"1\d{10}", name):
        name = _mask_phone(name)
    if not name:
        name = _mask_phone(acct.get("phone"))
    return name


def identity_of(st: dict) -> str:
    """账号身份签名：用户名/手机号/中继域名任一变化即视为换了账号。

    用于检测「已登录状态下切换账号」——旧逻辑只在 未登录→已登录 跃迁时
    刷新页面，切换账号完成后应用仍显示旧账号/旧模型，看起来像没生效。
    刻意不含模型数量：菜单里的「刷新可用模型」自己会刷新页面，别重复。
    """
    acct = st.get("account") or {}
    return "|".join([
        str(acct.get("username") or ""),
        str(acct.get("phone") or ""),
        str(st.get("relay_base") or ""),
    ])


def _fmt_balance(bal) -> str:
    """把平台返回的余额规整成两位小数金额文本（不含货币符号）。

    平台 userinfo 的 balance 是**成品展示字符串**（实测为 ``'¥16.769472 额度'``），
    也可能给纯数值。若直接拼接会得到「余额 ¥¥16.769472 额度」这种双重格式。
    这里统一只抽数字部分、保留两位小数，货币符号由客户端自己控制。
    抽不出数字时返回空串（调用方跳过余额展示）。
    """
    text = str(bal if bal is not None else "").strip()
    if not text:
        return ""
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    if not m:
        return ""
    try:
        return f"{float(m.group()):,.2f}"
    except ValueError:  # pragma: no cover  正则已保证是数字
        return ""


def chip_label(st: dict) -> tuple[str, str]:
    """标题栏账号区文案（文字, 悬停提示），按登录态渲染。"""
    acct = st.get("account") or {}
    models = acct.get("models") or []
    if st.get("logged_in"):
        return _display_name(acct), \
            f"Tokengine 账号已连接 · 可用模型 {len(models)} 个"
    if st.get("configured"):
        # configured 不再弹菜单（菜单是登录态专属）：点击直接发起登录
        return "已配置令牌", "本机已配置令牌，点击登录账号"
    if st.get("in_progress"):
        return "等待浏览器…", "请在浏览器中完成登录与授权"
    return "登录", "登录 Tokengine 账号，自动装载令牌与可用模型"


# --------------------------------------------------------------------------- #
# 下拉菜单模型                                                                  #
# --------------------------------------------------------------------------- #
def account_menu_model(status: dict) -> dict:
    """按登录态渲染账号下拉菜单（标题栏 chip 据此构建原生 ContextMenuStrip）。

    顶层 ``logged_in`` / ``configured`` 标志保留（与旧 inject.py 版本同形，
    便于验证脚本复用）；chip 的点击路由见 :func:`chip_model`。
    """
    acct = status.get("account") or {}
    logged = bool(status.get("logged_in"))
    configured = bool(status.get("configured"))
    base = {"logged_in": logged, "configured": configured}

    if logged:
        models = acct.get("models") or []
        parts = []
        amount = _fmt_balance(acct.get("balance"))
        if amount:
            parts.append(f"余额 ¥{amount}")
        parts.append(f"{len(models)} 个模型")
        return {
            **base,
            "header": {"title": _display_name(acct),
                       "sub": " · ".join(parts)},
            "items": [
                {"action": "refresh", "label": "刷新可用模型"},
                {"type": "sep"},
                {"action": "platform", "label": "打开 Tokengine 平台"},
                {"action": "switch", "label": "切换账号"},
                {"type": "sep"},
                {"action": "logout", "label": "退出登录", "danger": True},
                {"type": "sep"},
                {"action": "about", "label": "关于 EduBuddy"},
            ],
        }
    if configured:
        return {
            **base,
            "header": {"title": "已配置令牌", "sub": "本机已配置令牌，可切换账号"},
            "items": [
                {"action": "switch", "label": "登录 / 切换账号"},
                {"action": "platform", "label": "打开 Tokengine 平台"},
                {"type": "sep"},
                {"action": "about", "label": "关于 EduBuddy"},
            ],
        }
    if status.get("in_progress"):
        return {
            **base,
            "header": {"title": "等待浏览器完成…", "sub": "请在浏览器中完成登录与授权"},
            "items": [
                {"action": "switch", "label": "重新发起登录"},
                {"action": "platform", "label": "打开 Tokengine 平台"},
                {"type": "sep"},
                {"action": "about", "label": "关于 EduBuddy"},
            ],
        }
    return {
        **base,
        "header": {"title": "EduBuddy", "sub": "未登录 Tokengine"},
        "items": [
            {"action": "switch", "label": "登录 Tokengine"},
            {"action": "platform", "label": "打开 Tokengine 平台"},
            {"type": "sep"},
            {"action": "about", "label": "关于 EduBuddy"},
        ],
    }


def chip_model(st: dict) -> dict:
    """标题栏账号区完整模型：{label, title, logged_in, menu}。

    ``menu`` 为 None 时点击账号区 = 直接发起登录；否则点击弹下拉菜单。
    分支与 :func:`chip_label` 一一对应：
      已登录      → 菜单（刷新/平台/切换/退出/关于）
      已配置令牌  → 无菜单，点击直发登录（与旧按钮行为一致）
      登录进行中  → 菜单（重新发起登录/平台/关于）
      未登录      → 无菜单，点击直发登录
    """
    st = st or {}
    label, title = chip_label(st)
    logged = bool(st.get("logged_in"))
    configured = bool(st.get("configured"))
    in_progress = bool(st.get("in_progress"))
    if logged or (not configured and in_progress):
        menu = account_menu_model(st)
    else:
        menu = None
    return {"label": label, "title": title,
            "logged_in": logged, "menu": menu}


# --------------------------------------------------------------------------- #
# 状态同步线程                                                                  #
# --------------------------------------------------------------------------- #
class AccountStatusSync:
    """轮询 AuthManager.status()，增量推送标题栏账号区模型 + 跃迁检测。

    参数
      chip_fn          : 无参可调用，返回 AccountChip（可为 None，如非
                         Windows / 条带构建失败）；每轮取最新引用。
      status_of        : 无参可调用，返回 AuthManager.status() 字典。
      on_authenticated : 可选；登录成功 / 账号身份变化时触发（刷新页面）。
      on_app_page      : 可选；无参可调用，返回当前是否在应用页面。仅在
                         应用页面上才触发 on_authenticated——门控页的登录
                         成功由会话主循环自己导航进应用，别抢跑刷新。
      interval         : 轮询间隔（秒）。
    """

    def __init__(self, chip_fn, status_of, on_authenticated=None,
                 on_app_page=None, interval: float = 0.4) -> None:
        self._chip_fn = chip_fn
        self._status_of = status_of
        self._on_auth = on_authenticated
        self._on_app_page = on_app_page or (lambda: True)
        self._interval = interval
        self._stop = threading.Event()
        self._last_payload: str | None = None
        self._was_logged_in: bool | None = None
        self._last_identity: str | None = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                st = self._status_of() or {}
            except Exception:  # noqa: BLE001  状态读取失败，下一轮再试
                st = {}
            try:
                self._push(st)
                self._detect_transitions(st)
            except Exception:  # noqa: BLE001  单轮失败不终止同步
                log.exception("account status sync failed")
            self._stop.wait(self._interval)

    # ---- 内部 ---------------------------------------------------------- #
    def _push(self, st: dict) -> None:
        """模型变化才推（UI 线程 invoke + 条带重绘都不便宜）。"""
        model = chip_model(st)
        payload = json.dumps(model, ensure_ascii=False, sort_keys=True)
        if payload == self._last_payload:
            return
        self._last_payload = payload
        chip = self._chip_fn() if callable(self._chip_fn) else self._chip_fn
        if chip is not None:
            chip.set_model(model)

    def _detect_transitions(self, st: dict) -> None:
        logged = bool(st.get("logged_in")) or bool(st.get("configured"))
        if self._was_logged_in is False and logged:
            log.info("检测到登录成功，刷新页面以载入新模型")
            self._fire_auth()
        else:
            identity = identity_of(st)
            if (logged and identity and self._last_identity
                    and identity != self._last_identity):
                # 已登录状态下身份变化（切换账号/换绑域名）：同样要刷新，
                # 否则应用一直显示旧账号的数据，切换看起来像没生效。
                log.info("检测到账号身份变化，刷新页面以载入新账号数据")
                self._fire_auth()
        self._was_logged_in = logged
        self._last_identity = identity_of(st)

    def _fire_auth(self) -> None:
        """触发「登录态/账号变化」回调（刷新页面载入新数据），绝不抛出。"""
        if not self._on_auth:
            return
        try:
            if not self._on_app_page():
                return  # 门控页/加载中：会话循环自己会导航，别抢跑
        except Exception:  # noqa: BLE001
            return
        try:
            self._on_auth()
        except Exception:  # noqa: BLE001
            log.exception("on_authenticated 回调执行失败")
