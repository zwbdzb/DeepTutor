"""应用页面底部轻提示（toast）注入 —— 本模块现仅此一项职责。

历史说明：这里曾负责往页面注入左下角「登录/账号」悬浮按钮与自绘下拉菜单
（隐藏 input 事件槽 + Python ``evaluate_js`` 轮询通道，见 ADR-002）。
2026-09-23 起账号入口迁到**原生标题栏**（ADR-004），点击与菜单改由
WinForms 直接回调（``desktop/native_menu_backend.py`` 的 ``AccountChip``），
「按钮 + 菜单 + 事件槽轮询」整套页面注入随之拆除。

保留 toast 的原因：菜单动作（刷新模型 / 复制地址等）需要一条轻量操作反馈，
而原生壳里最贴近旧体验的就是页面底部的短暂浮层。SPA 整页刷新（登录成功、
刷新模型后）会重建 DOM 把 toast 元素清掉，所以仍用「周期 ensure」的老办法
把它补回来——这是本模块残留下来的唯一一个定时器职责。

通道本身（evaluate_js）不依赖 js_api 桥，跨 pywebview 版本稳定，已在
tools/_tmp_verify/probe_js_bridge.py 里单独验证过。
"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("dt.inject")

TOAST_ID = "edubuddy-toast"

# 确保 toast 浮层元素存在（幂等；SPA 路由/整页刷新后由定时器补挂）。
TOAST_ENSURE_JS = """
(function () {
  if (!document.body) { return 'no-body'; }
  if (document.getElementById('%(id)s')) { return 'exists'; }
  var t = document.createElement('div');
  t.id = '%(id)s';
  t.style.cssText = 'position:fixed;left:50%%;bottom:36px;transform:translateX(-50%%);' +
    'z-index:2147483002;background:rgba(17,17,17,.88);color:#fff;' +
    'font:12px/1.5 "Segoe UI","Microsoft YaHei",system-ui,sans-serif;' +
    'padding:8px 16px;border-radius:16px;display:none;max-width:70%%;text-align:center;' +
    'box-shadow:0 4px 16px rgba(0,0,0,.20)';
  document.body.appendChild(t);
  return 'created';
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
