"""探针：`pywebview.api.confirm` → 系统 MessageBox 端到端是否可用（会弹一个窗口）。

ADR-007 把应用页的二次确认交给壳，全靠这条链路：页面（**导航之后**的页面，
不是 create_window 的 html= 首屏）里 `window.pywebview.api.confirm` 存在 →
Python 侧 `dialogs.confirm` 弹 MessageBoxW → 返回值经桥回到 JS 的 Promise。

这条链路有两个容易翻车的点，本脚本就是为它们准备的：

1. pywebview 是否给**导航后**的页面注入 js_api 桥（壳是 load_url 进应用页的，
   不是 html= 首屏；不同 pywebview 版本在这点上有过差异）；
2. 返回值是不是严格的 JS `boolean`（前端只认 true/false，其余按取消）。

做法：起一个只服务单页的本地 http server（与壳访问 127.0.0.1:<port> 同构），
先 html= 出启动页再 load_url 跳过去；页面在 `pywebviewready` 后调 confirm。
Python 线程用 FindWindowW("#32770", "ThinkBuddy") 找到系统弹窗，PostMessage
WM_COMMAND 按下指定按钮，再轮询 `document.title` 读回 JS 拿到的结果。

用法：python tools/_probe_confirm_bridge.py [--cancel]
  默认按「确定」→ 期望 RESULT:true:boolean
  --cancel 按「取消」→ 期望 RESULT:false:boolean
退出码 0 = 链路通且返回值类型正确。

实测（2026-11-11，pywebview 6.2.1 / Windows 11）：确定 → RESULT:true:boolean，
取消 → RESULT:false:boolean。
"""
from __future__ import annotations

import argparse
import ctypes
import http.server
import os
import socketserver
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from desktop import dialogs  # noqa: E402

import webview  # noqa: E402

PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>probe-boot</title></head>
<body><h1>probe</h1>
<script>
window.addEventListener('pywebviewready', async () => {
  const has = typeof window.pywebview !== 'undefined' && window.pywebview.api
    && typeof window.pywebview.api.confirm === 'function';
  if (!has) { document.title = 'ERROR:no-bridge'; return; }
  try {
    const r = await window.pywebview.api.confirm('永久删除此对话及其导师子对话？此操作无法撤销。');
    document.title = 'RESULT:' + r + ':' + (typeof r);
  } catch (e) {
    document.title = 'ERROR:' + (e && e.message ? e.message : String(e));
  }
});
</script></body></html>
"""


class _Api:
    """与 desktop.main.Api.confirm 同样的签名与语义。"""

    def confirm(self, message: str, title: str = "") -> bool:
        return dialogs.confirm(title or "ThinkBuddy", str(message))


def _serve(directory: str) -> tuple[socketserver.TCPServer, int]:
    def handler(*args, **kwargs):
        return http.server.SimpleHTTPRequestHandler(*args, directory=directory, **kwargs)

    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cancel", action="store_true", help="按「取消」而不是「确定」")
    args = parser.parse_args()
    button = 2 if args.cancel else 1  # IDCANCEL / IDOK

    tmp = tempfile.mkdtemp(prefix="tb-confirm-probe-")
    with open(os.path.join(tmp, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(PAGE)
    httpd, port = _serve(tmp)
    url = f"http://127.0.0.1:{port}/"

    window = webview.create_window(
        "probe", html="<html><body>boot</body></html>", width=520, height=320,
        js_api=_Api(),
    )
    seen: dict = {"dialog": False, "title": None}

    def driver() -> None:
        time.sleep(1.2)
        window.load_url(url)
        title = None
        deadline = time.time() + 15
        while time.time() < deadline:
            hwnd = ctypes.windll.user32.FindWindowW("#32770", "ThinkBuddy")
            if hwnd and not seen["dialog"]:
                seen["dialog"] = True
                time.sleep(0.4)
                ctypes.windll.user32.PostMessageW(hwnd, 0x0111, button, 0)  # WM_COMMAND
            try:
                title = window.evaluate_js("document.title")
            except Exception:  # noqa: BLE001  页面切换瞬间取不到
                title = None
            if isinstance(title, str) and title.split(":")[0] in ("RESULT", "ERROR"):
                seen["title"] = title
                break
            time.sleep(0.25)
        seen["title"] = seen["title"] or title
        try:
            window.destroy()
        except Exception:  # noqa: BLE001
            pass

    watchdog = threading.Timer(30, lambda: os._exit(3))
    watchdog.daemon = True
    watchdog.start()
    threading.Thread(target=driver, daemon=True).start()

    webview.start(debug=False)
    httpd.shutdown()
    watchdog.cancel()

    expected = "RESULT:false:boolean" if args.cancel else "RESULT:true:boolean"
    print("DIALOG_SEEN", seen["dialog"])
    print("PAGE_TITLE", seen["title"])
    ok = bool(seen["dialog"]) and seen["title"] == expected
    print("OK" if ok else f"FAIL (want {expected})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
