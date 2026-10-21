# -*- coding: utf-8 -*-
"""端口收敛单元测试：bind 探测、前端回退选口、banner 后端口回读。

对应 2026-10-20 端口收敛方案（不对称端口：后端 ephemeral、前端固定优先+回退）。
运行：desktop-shell/.venv/Scripts/python tools/test_port_selection.py
"""
import socket
import sys
import threading

sys.path.insert(0, r"D:\studio\DeepTutor\desktop-shell")

import desktop.process as dp
from desktop.process import DeepTutorProcess


def make_proc() -> DeepTutorProcess:
    proc = DeepTutorProcess.__new__(DeepTutorProcess)  # 绕过 __init__（无需运行时）
    proc.backend_port = dp.DEFAULT_BACKEND_PORT
    proc.frontend_port = dp.DEFAULT_FRONTEND_PORT
    proc.on_line = None
    return proc


def listen(family: int, host: str):
    """开一个真实监听 socket，返回 (sock, port)。"""
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.bind((host, 0))
    sock.listen(1)
    return sock, sock.getsockname()[1]


# --- A. bind 探测 ------------------------------------------------------------
free_port = dp._pick_ephemeral_port() if hasattr(dp, "_pick_ephemeral_port") else None
# 壳侧没有 ephemeral 助手，用系统随端口取一个空闲口
probe_sock, free_port = listen(socket.AF_INET, "127.0.0.1")
free_port_probe = free_port
probe_sock.close()
assert dp._port_free(free_port_probe) is True, "空闲端口应判定可 bind"
print(f"[ok] A1 空闲端口 {free_port_probe} → bind 探测通过")

sock_v4, port_v4 = listen(socket.AF_INET, "127.0.0.1")
assert dp._port_free(port_v4) is False, "IPv4 监听中的端口应判定占用"
print(f"[ok] A2 IPv4 监听 {port_v4} → 占用")
sock_v4.close()
assert dp._port_free(port_v4) is True, "关闭后应恢复空闲"
print(f"[ok] A3 关闭后恢复空闲")

try:
    sock_v6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock_v6.bind(("::1", 0))
    sock_v6.listen(1)
except OSError:
    print("[skip] A4 无 IPv6 栈")
else:
    port_v6 = sock_v6.getsockname()[1]
    assert dp._port_free(port_v6) is False, (
        "nginx [::]:P 事件回归：仅 IPv6 被占用也必须判占用（localhost 优先走 ::1）"
    )
    print(f"[ok] A4 仅 IPv6 监听 {port_v6} → 占用（nginx 事件回归）")
    sock_v6.close()

# --- B. 前端回退选口 ---------------------------------------------------------
proc = make_proc()
fallback_base = 39000 + (int(threading.current_thread().ident) % 500)
saved_default = dp.DEFAULT_FRONTEND_PORT
dp.DEFAULT_FRONTEND_PORT = fallback_base
try:
    chosen = proc._select_frontend_port()
    assert chosen == fallback_base, f"空闲时应用默认端口，得到 {chosen}"
    print(f"[ok] B1 空闲 → 固定用默认端口 {chosen}")

    sock_hold, held_port = listen(socket.AF_INET, "127.0.0.1")
    dp.DEFAULT_FRONTEND_PORT = held_port
    proc.frontend_port = held_port
    chosen = proc._select_frontend_port()
    assert chosen == held_port + 1, f"外部占用应回退到 +1，得到 {chosen}"
    print(f"[ok] B2 外部占用 {held_port} → 回退 {chosen}")
    sock_hold.close()

    dp.DEFAULT_FRONTEND_PORT = held_port
    proc.frontend_port = held_port
    chosen = proc._select_frontend_port()
    assert chosen == held_port, f"占用者退出后应回到默认端口，得到 {chosen}"
    print(f"[ok] B3 占用退出 → 回到默认端口 {chosen}")
finally:
    dp.DEFAULT_FRONTEND_PORT = saved_default

# --- C. banner 后端口回读（壳托管模式后端口 OS 分配，壳从日志学） -------------
proc = make_proc()
proc._handle_line("后端       http://127.0.0.1:52341".encode("utf-8"))
assert proc.backend_port == 52341, f"zh banner 行应回读端口，得到 {proc.backend_port}"
print("[ok] C1 zh banner 行 → backend_port=52341")

proc._handle_line("Backend    http://127.0.0.1:52400".encode("utf-8"))
assert proc.backend_port == 52400, f"en banner 行应回读端口，得到 {proc.backend_port}"
print("[ok] C2 en banner 行 → backend_port=52400")

proc._handle_line("前端 API    http://127.0.0.1:8001".encode("utf-8"))
assert proc.backend_port == 52400, "前端 API 行是浏览器外部地址，不得改写后端口"
print("[ok] C3 前端 API 行不误改")

proc._handle_line("Uvicorn running on http://0.0.0.0:52400".encode("utf-8"))
assert proc.backend_port == 52400, "非 127.0.0.1 的 URL 不参与回读"
print("[ok] C4 uvicorn 0.0.0.0 行不误改")

proc._handle_line("正在启动后端服务 ...".encode("utf-8"))
assert proc.backend_port == 52400, "无 URL 的行不得改写"
print("[ok] C5 无 URL 行不误改")

print("\n全部通过：bind 探测 / 前端回退 / banner 回读 3 组场景 OK")
