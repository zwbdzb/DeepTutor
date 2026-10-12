"""页面注入器：toast 浮层（全平台）+ macOS 账号 chip（仅 darwin）。

历史说明：本模块曾负责往页面注入左下角「登录/账号」悬浮按钮与自绘下拉菜单
（隐藏 input 事件槽 + Python ``evaluate_js`` 轮询通道，见 ADR-002）。
2026-09-23 起账号入口迁到**原生标题栏**（ADR-004），点击与菜单改由
WinForms 直接回调（``desktop/native_menu_backend.py`` 的 ``AccountChip``），
「按钮 + 菜单 + 事件槽轮询」整套页面注入随之拆除。

2026-10 起 macOS 重新走页面内悬浮 chip（ADR-002 机制在 mac 上复活）：
Windows 账号入口仍用原生标题栏 ``AccountChip``，mac 上没有 WinForms 标题
栏条带，原生菜单栏的「账号」菜单标题又依赖 AppKit（pyobjc），打包未把
pyobjc 收进 .app 时菜单标题无法动态跟随登录态。为绕开 AppKit 依赖、并让
账号出现在用户期望的「窗口右上角」，mac 上由 :class:`AccountChipInjector`
往应用页面注入右上角悬浮 chip + 自绘下拉菜单，模型复用
``titlebar_account.chip_model()``（与 Windows 账号区同一套文案/菜单），
点击动作复用 ``main._chip_actions`` 同一套回调。仅 darwin 生效。

保留 toast 的原因：菜单动作（刷新模型 / 复制地址等）需要一条轻量操作反馈，
而原生壳里最贴近旧体验的就是页面底部的短暂浮层。SPA 整页刷新（登录成功、
刷新模型后）会重建 DOM 把 toast 元素清掉，所以仍用「周期 ensure」的老办法
把它补回来。

通道本身（evaluate_js）不依赖 js_api 桥，跨 pywebview 版本稳定，已在
tools/_tmp_verify/probe_js_bridge.py 里单独验证过。
"""
from __future__ import annotations

import json
import logging
import threading
import time

log = logging.getLogger("dt.inject")

# --------------------------------------------------------------------------- #
# toast 浮层（全平台）------------------------------------------------------ #
# --------------------------------------------------------------------------- #
TOAST_ID = "edubuddy-toast"

# 确保 toast 浮层元素存在（幂等；SPA 路由/整页刷新后由定时器补挂）。
TOAST_ENSURE_JS = """
(function () {
  if (!document.body) { return 'no-body'; }
  var t = document.getElementById('%(id)s');
  var result = 'exists';
  if (!t) {
    t = document.createElement('div');
    t.id = '%(id)s';
    t.style.cssText = 'position:fixed;left:50%%;bottom:36px;transform:translateX(-50%%);' +
      'z-index:2147483002;background:rgba(17,17,17,.88);color:#fff;' +
      'font:12px/1.5 "Segoe UI","Microsoft YaHei",system-ui,sans-serif;' +
      'padding:8px 16px;border-radius:16px;display:none;max-width:70%%;text-align:center;' +
      'box-shadow:0 4px 16px rgba(0,0,0,.20)';
    document.body.appendChild(t);
    result = 'created';
  }
  window.__edubuddyToast = function (message) {
    if (t.__hideTimer) { clearTimeout(t.__hideTimer); }
    t.textContent = String(message || '');
    t.style.display = 'block';
    t.__hideTimer = setTimeout(function () {
      t.style.display = 'none';
    }, 2600);
  };
  return result;
})();
""" % {"id": TOAST_ID}


class ToastInjector:
    """在应用页面里维持 toast 浮层元素（供 api.toast / menubar._toast 使用）。

    参数
      window     : pywebview Window
      expect_url : 可选；只有当前 URL 以它开头（即真正进入应用页）才注入，
                   启动页/登录门控页不需要 toast。
      interval   : 自愈周期（秒）。
    """

    def __init__(self, window, expect_url: str | None = None,
                 interval: float = 2.0) -> None:
        self._w = window
        self._expect_url = expect_url
        self._interval = interval
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def _on_app_page(self) -> bool:
        if not self._expect_url:
            return True
        try:
            cur = self._w.get_current_url() or ""
        except Exception:  # noqa: BLE001
            return False
        return cur.startswith(self._expect_url)

    def run(self) -> None:
        while not self._stop.is_set():
            if self._on_app_page():
                try:
                    self._w.evaluate_js(TOAST_ENSURE_JS)
                except Exception:  # noqa: BLE001  页面未就绪，下一轮再试
                    pass
            self._stop.wait(self._interval)


# --------------------------------------------------------------------------- #
# macOS 账号 chip：右上角悬浮 chip + 自绘下拉菜单（仅 darwin）------------- #
# 机制：隐藏 input 事件槽 + Python evaluate_js 轮询（与 ADR-002 同源）。     #
# 模型由 AccountStatusSync 推送（titlebar_account.chip_model 的输出），      #
# 点击动作分发到 main._chip_actions（与 Windows 账号区同一套回调）。         #
# --------------------------------------------------------------------------- #
ACCT_BTN_ID = "edubuddy-account-chip"
ACCT_LOGIN_EVT_ID = "edubuddy-account-chip-evt"   # 登录事件槽（未登录点击直发 OAuth）
ACCT_MENU_EVT_ID = "edubuddy-account-menu-evt"    # 菜单动作事件槽（menu:<action>）

# 事件槽轮询模板：读后即清空（天然去重）。
_ACCT_POLL_TMPL = """
(function () {
  var e = document.getElementById('%(evt)s');
  if (!e) { return ''; }
  var v = e.value;
  if (v) { e.value = ''; }
  return v;
})();
"""
ACCT_POLL_LOGIN_JS = _ACCT_POLL_TMPL % {"evt": ACCT_LOGIN_EVT_ID}
ACCT_POLL_MENU_JS = _ACCT_POLL_TMPL % {"evt": ACCT_MENU_EVT_ID}

# 右上角悬浮 chip：按钮 + 隐藏登录事件槽，每 1s 自检（SPA 重建 DOM 时补回）。
# 点击路由：有菜单模型（已登录/进行中）→ 切换菜单；无菜单（未登录/已配置令牌）
# → 写登录事件槽发起 OAuth。
ACCT_ENSURE_JS = r"""
(function () {
  var BTN = '__BTN__', EV = '__EVT__';
  function slot() {
    var e = document.getElementById(EV);
    if (!e) {
      e = document.createElement('input');
      e.id = EV; e.type = 'hidden'; e.value = '';
      document.body.appendChild(e);
    }
    return e;
  }
  function ensure() {
    if (!document.body) { return 'no-body'; }
    slot();
    if (document.getElementById(BTN)) { return 'exists'; }
    var b = document.createElement('button');
    b.id = BTN; b.type = 'button';
    b.textContent = '登录';
    b.title = '登录 Tokengine 账号';
    b.style.cssText = [
      'position:fixed','right:12px','top:10px',
      'z-index:2147483000',
      'height:28px','padding:0 12px','border-radius:14px',
      'border:1px solid rgba(17,17,17,.14)','background:#fff',
      'color:#111','cursor:pointer',
      'font:500 12px/1 "Segoe UI","PingFang SC","Microsoft YaHei",system-ui,sans-serif',
      'box-shadow:0 1px 3px rgba(0,0,0,.10)',
      'transition:box-shadow .15s ease, opacity .15s ease'
    ].join(';');
    b.onmouseenter = function () { b.style.boxShadow = '0 2px 9px rgba(0,0,0,.18)'; };
    b.onmouseleave = function () { b.style.boxShadow = '0 1px 3px rgba(0,0,0,.10)'; };
    b.onclick = function () {
      var t = window.__edubuddyChipToggle;
      if (window.__edubuddyChipHasMenu && typeof t === 'function') { t(); return; }
      var e = document.getElementById(EV);
      if (e) { e.value = 'login:' + Date.now(); }
      b.disabled = true; b.style.opacity = '.55';
      setTimeout(function () { b.disabled = false; b.style.opacity = '1'; }, 2000);
    };
    document.body.appendChild(b);
    return 'created';
  }
  window.__edubuddyChipEnsure = ensure;
  var r = ensure();
  if (!window.__edubuddyChipTimer) { window.__edubuddyChipTimer = setInterval(ensure, 1000); }
  return r;
})();
""".replace("__BTN__", ACCT_BTN_ID).replace("__EVT__", ACCT_LOGIN_EVT_ID)

# 自绘下拉菜单：复用同一套「事件槽 + Python 轮询」通道。
# 已登录点击 chip 在其下方弹出菜单（再点一次收起）；点菜单项把
# menu:<action>:<ts> 写进隐藏事件槽，Python 侧轮询消费。
# 危险项（退出登录）需要「再次点击」二次确认。
ACCT_MENU_ENSURE_JS = r"""
(function () {
  var EV = '__MVT__', BTN = '__BTN__', menu = null, confirmTimer = null, state = null;
  var MENU_CSS = [
    'position:fixed','z-index:2147483001','min-width:192px','max-width:268px',
    'padding:6px','border-radius:10px','border:1px solid rgba(17,17,17,.12)',
    'background:#fff','color:#111',
    'font:13px/1.55 "Segoe UI","PingFang SC","Microsoft YaHei",system-ui,sans-serif',
    'box-shadow:0 10px 30px rgba(0,0,0,.18)','display:none','user-select:none'
  ].join(';');

  function slot() {
    var e = document.getElementById(EV);
    if (!e) {
      e = document.createElement('input');
      e.id = EV; e.type = 'hidden'; e.value = '';
      document.body.appendChild(e);
    }
    return e;
  }
  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function headerHtml(st) {
    var h = (st && st.header) || {}, t = h.title || 'ThinkBuddy', s = h.sub || '';
    var html = '<div class="edubuddy-menu-head" style="border-bottom:1px solid rgba(17,17,17,.08);' +
               'padding:4px 10px 8px;margin-bottom:6px">';
    html += '<div style="font-weight:600;font-size:13px">' + escapeHtml(t) + '</div>';
    if (s) html += '<div class="edubuddy-menu-sub" style="font-size:11px;color:#8a8f98;margin-top:2px">' + escapeHtml(s) + '</div>';
    return html + '</div>';
  }
  function render() {
    if (!menu) return;
    var st = state || { items: [] }, html = headerHtml(st);
    (st.items || []).forEach(function (it) {
      if (it && it.type === 'sep') {
        html += '<div style="height:1px;background:rgba(17,17,17,.08);margin:4px 8px"></div>';
        return;
      }
      if (!it || !it.action) return;
      var danger = it.danger ? ';color:#d93025' : '';
      var confirm = it.danger ? '再次点击以确认' : '';
      html += '<div data-action="' + it.action + '" data-confirm="' + escapeHtml(confirm) + '"'
            + ' style="padding:7px 12px;border-radius:7px;cursor:pointer;white-space:nowrap;'
            + 'overflow:hidden;text-overflow:ellipsis' + danger + '">'
            + escapeHtml(it.label) + '</div>';
    });
    if (!(st.items || []).length) {
      html += '<div style="padding:8px 12px;color:#8a8f98;font-size:12px">暂无可用操作</div>';
    }
    menu.innerHTML = html;
    menu.querySelectorAll('[data-action]').forEach(function (d) {
      d.onmouseenter = function () { d.style.background = 'rgba(17,17,17,.06)'; };
      d.onmouseleave = function () { d.style.background = 'rgba(17,17,17,0)'; };
      d.onclick = function () {
        var a = d.getAttribute('data-action');
        var cf = d.getAttribute('data-confirm');
        if (cf) {
          if (d.__arm) { emit(a); close(); d.__arm = false; return; }
          d.__arm = true;
          var orig = d.textContent;
          d.textContent = cf;
          d.style.background = 'rgba(217,48,37,.10)'; d.style.color = '#d93025';
          if (confirmTimer) clearTimeout(confirmTimer);
          confirmTimer = setTimeout(function () {
            d.__arm = false; d.textContent = orig;
            d.style.background = 'rgba(17,17,17,0)'; d.style.color = '';
            confirmTimer = null;
          }, 3000);
          return;
        }
        emit(a); close();
      };
    });
  }
  function emit(action) { slot().value = 'menu:' + action + ':' + Date.now(); }
  function button() { return document.getElementById(BTN); }
  function close() {
    if (menu) menu.style.display = 'none';
    if (confirmTimer) { clearTimeout(confirmTimer); confirmTimer = null; }
    document.removeEventListener('mousedown', onDocDown, true);
  }
  function onDocDown(e) {
    if (!menu || menu.style.display !== 'block') return;
    if (menu.contains(e.target)) return;
    var b = button();
    if (b && (e.target === b || (b.contains && b.contains(e.target)))) return;
    close();
  }
  // 右上角 chip：菜单右缘对齐 chip 右缘，默认在 chip 下方打开；超出底部则改上方。
  function openAtButton() {
    var b = button();
    render();
    menu.style.display = 'block';
    var rect = b ? b.getBoundingClientRect() : { right: window.innerWidth - 12, bottom: 38 };
    var w = menu.offsetWidth, h = menu.offsetHeight;
    var left = Math.max(8, Math.min(rect.right - w, window.innerWidth - w - 8));
    var top = rect.bottom + 6;
    if (top + h > window.innerHeight - 8) top = Math.max(8, rect.top - h - 6);
    menu.style.left = left + 'px'; menu.style.top = top + 'px';
    setTimeout(function () { document.addEventListener('mousedown', onDocDown, true); }, 0);
  }
  function toggle() {
    if (!menu && !ensureEls()) return 'no-body';
    if (menu.style.display === 'block') { close(); return 'closed'; }
    render(); openAtButton(); return 'open';
  }
  function ensureEls() {
    if (!document.body) return false;
    slot();
    if (!menu) {
      menu = document.createElement('div');
      menu.id = 'edubuddy-account-menu';
      menu.style.cssText = MENU_CSS;
      document.body.appendChild(menu);
    }
    return true;
  }
  function ensureEvents() {
    if (window.__edubuddyChipMenuInit) return;
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); }, true);
    document.addEventListener('scroll', close, true);
    document.addEventListener('wheel', close, true);
    window.addEventListener('blur', close);
    window.addEventListener('resize', close);
    window.__edubuddyChipMenuInit = true;
  }
  window.__edubuddyChipToggle = toggle;
  // s 是 titlebar_account.chip_model() 的输出：{label, title, logged_in, menu}
  // menu 为 None 时点击 chip 走登录事件槽；非 None 时点击切换菜单。
  window.__edubuddyChipUpdate = function (s) {
    state = (s && s.menu) ? s.menu : null;
    window.__edubuddyChipHasMenu = !!(s && s.menu);
    var b = document.getElementById(BTN);
    if (b) {
      b.textContent = (s && s.label) || '登录';
      b.title = (s && s.title) || '登录 Tokengine 账号';
    }
    if (menu && menu.style.display === 'block') render();
  };
  if (!ensureEls()) return 'no-body';
  ensureEvents();
  return 'ok';
})();
""".replace("__MVT__", ACCT_MENU_EVT_ID).replace("__BTN__", ACCT_BTN_ID)


def _chip_update_js(model: dict) -> str:
    """把 chip 模型推给页面（JSON 即合法 JS 字面量）。"""
    return "window.__edubuddyChipUpdate(%s)" % json.dumps(model, ensure_ascii=False)


class AccountChipInjector:
    """macOS 右上角账号 chip：页面注入悬浮 chip + 自绘下拉菜单。

    仅 darwin 用（Windows 账号入口在原生标题栏 ``AccountChip``）。
    模型由 :class:`desktop.titlebar_account.AccountStatusSync` 通过
    :meth:`set_model` 推送（``titlebar_account.chip_model()`` 的输出），
    文案/菜单结构与 Windows 账号区逐字一致。点击动作分发到
    ``main._chip_actions`` 的同一套回调。

    机制：隐藏 input 事件槽 + Python ``evaluate_js`` 轮询（见 ADR-002）。
    本类只负责 DOM 维持（周期 ensure）+ 事件槽消费；登录态跃迁检测、
    页面刷新由 AccountStatusSync 统一负责，不在此重复。

    参数
      window      : pywebview Window
      on_login    : 无参可调用；chip 被点击且无菜单（未登录/已配置令牌）时触发
      menu_actions: 可选；{动作名: 无参可调} 分发表，供下拉菜单项分发。
                    键见 titlebar_account.MENU_ACTIONS（switch/platform/refresh/
                    copy/logout/about/invite/checkin）
      expect_url  : 可选；只有当前 URL 以它开头（真正进入应用页）才注入。
      interval    : 事件槽轮询周期（秒）。
      ensure_every: DOM 自愈周期（秒）；SPA 路由/整页刷新会清掉 JS 状态。
    """

    def __init__(self, window, on_login, menu_actions: dict | None = None,
                 expect_url: str | None = None,
                 interval: float = 0.4, ensure_every: float = 2.0) -> None:
        self._w = window
        self._on_login = on_login
        self._menu_actions = dict(menu_actions or {})
        self._expect_url = expect_url
        self._interval = interval
        self._ensure_every = ensure_every
        self._stop = threading.Event()
        self._model: dict | None = None  # 最新模型（ensure 重建 DOM 后重推）
        self.click_count = 0
        self.menu_count = 0

    # ---- 对外 ---------------------------------------------------------- #
    def set_model(self, model: dict) -> None:
        """AccountStatusSync 调用：把 chip 模型推给页面。"""
        self._model = model or None
        self._push_model()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        """后台线程：等应用页 -> ensure chip/菜单 -> 轮询事件槽。"""
        last_ensure = 0.0
        installed = False
        while not self._stop.is_set():
            if not self._on_app_page():
                self._stop.wait(self._interval)
                continue
            now = time.monotonic()
            try:
                if not installed or (now - last_ensure) >= self._ensure_every:
                    btn_state = self._w.evaluate_js(ACCT_ENSURE_JS)
                    self._w.evaluate_js(ACCT_MENU_ENSURE_JS)
                    if btn_state == "created":
                        # 按钮被重建（页面刷新/路由重建 DOM）——初始文案写死
                        # 「登录」，已登录用户会一直看到「登录」直到模型重推。
                        # 清缓存强制下方 _push_model 重新执行。
                        self._w.evaluate_js(_chip_update_js(self._model or {}))
                    else:
                        self._push_model()
                    last_ensure = now
                    installed = True
            except Exception:  # noqa: BLE001  页面还没加载完，下一轮再试
                installed = False
                self._stop.wait(self._interval)
                continue

            # 登录事件槽（未登录点击 chip 直发 OAuth）
            try:
                evt = self._w.evaluate_js(ACCT_POLL_LOGIN_JS)
                if evt:
                    self.click_count += 1
                    log.info("mac 账号 chip 被点击（第 %d 次）：%s",
                             self.click_count, evt)
                    threading.Thread(target=self._safe_login, daemon=True).start()
            except Exception:  # noqa: BLE001
                pass

            # 菜单动作事件槽（menu:<action>:<ts>）
            try:
                mevt = self._w.evaluate_js(ACCT_POLL_MENU_JS)
                if mevt:
                    parts = mevt.split(":")
                    action = parts[1] if len(parts) > 1 else parts[0]
                    self.menu_count += 1
                    log.info("mac 账号菜单动作（第 %d 次）：%s",
                             self.menu_count, mevt)
                    threading.Thread(
                        target=self._dispatch_menu, args=(action,), daemon=True
                    ).start()
            except Exception:  # noqa: BLE001
                pass

            self._stop.wait(self._interval)

    # ---- 内部 ---------------------------------------------------------- #
    def _on_app_page(self) -> bool:
        if not self._expect_url:
            return True
        try:
            cur = self._w.get_current_url() or ""
        except Exception:  # noqa: BLE001
            return False
        return cur.startswith(self._expect_url)

    def _push_model(self) -> None:
        if not self._model:
            return
        try:
            self._w.evaluate_js(_chip_update_js(self._model))
        except Exception:  # noqa: BLE001
            pass

    def _dispatch_menu(self, action: str) -> None:
        handler = self._menu_actions.get(action)
        if handler is None:
            log.warning("未知/未注册的菜单动作：%s", action)
            return
        try:
            handler()
        except Exception:  # noqa: BLE001
            log.exception("菜单动作 %s 执行失败", action)

    def _safe_login(self) -> None:
        try:
            self._on_login()
        except Exception:  # noqa: BLE001
            log.exception("发起登录失败")
