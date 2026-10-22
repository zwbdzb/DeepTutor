"""desktop.inject toast 注入脚本的浏览器侧行为回归（node 子进程执行）。

契约（与 inject.py 对齐）：

* ``TOAST_ENSURE_JS`` / ``TOAST_SHOW_JS`` 是含 ``%(id)s`` 等占位符的模板，
  调用侧必须先 ``% {"id": TOAST_ID, ...}`` 格式化再 ``evaluate_js``；
  本测试用与生产一致的格式化产物喂给 harness。
* ensure 幂等：首次 'created'、再次 'exists'，且只建一个 DOM 节点。
* 隐藏计时器存在 ``window.__edubuddyToastTimer`` 单一槽位：再次弹 toast
  必须先 clearTimeout 旧计时器（不叠层）。
* 文案经 ``textContent`` 写入，HTML 注入向量（<img onerror=...>）不产生子节点。
* ``ToastInjector.show()`` 路径（TOAST_SHOW_JS）必须能找到 ensure 建立的
  同一元素（id 一致），否则 show 静默失效。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

DESKTOP_SHELL = Path(__file__).resolve().parents[2] / "desktop-shell"
sys.path.insert(0, str(DESKTOP_SHELL))

from desktop.inject import TOAST_ENSURE_JS, TOAST_ID, TOAST_SHOW_JS


def test_toast_script_is_idempotent_and_resets_hide_timer() -> None:
    if shutil.which("node") is None:
        pytest.skip("Node.js is required to execute the desktop toast script")

    # 与生产调用侧一致的格式化（ensure 挂全局函数；show 直接驱动元素）。
    ensure_js = TOAST_ENSURE_JS % {"id": TOAST_ID}
    show_js = TOAST_SHOW_JS % {
        "id": TOAST_ID,
        "msg": json.dumps("来自 show()", ensure_ascii=False),
        "duration": 2000,
    }

    harness = f"""
const ensureScript = {json.dumps(ensure_js)};
const showScript = {json.dumps(show_js)};
const nodes = new Map();
const timers = new Map();
const cleared = [];
let created = 0;
let nextTimer = 0;
global.document = {{
  body: {{ appendChild(node) {{ nodes.set(node.id, node); }} }},
  getElementById(id) {{ return nodes.get(id) || null; }},
  createElement() {{ created += 1; return {{ style: {{}} }}; }}
}};
global.window = {{}};
global.setTimeout = (fn, delay) => {{
  const id = ++nextTimer;
  timers.set(id, {{ fn, delay }});
  return id;
}};
global.clearTimeout = (id) => {{ cleared.push(id); timers.delete(id); }};

const firstEnsure = eval(ensureScript);
const secondEnsure = eval(ensureScript);
window.__edubuddyToast('<img src=x onerror=alert(1)>');
const toast = nodes.get('edubuddy-toast');
const unsafeText = toast.textContent;
const firstTimer = window.__edubuddyToastTimer;
window.__edubuddyToast('second');
const latestText = toast.textContent;
eval(showScript);
const showText = toast.textContent;

process.stdout.write(JSON.stringify({{
  firstEnsure,
  secondEnsure,
  created,
  nodeCount: nodes.size,
  unsafeText,
  latestText,
  showText,
  opacity: toast.style.opacity,
  firstTimerCleared: cleared.includes(firstTimer),
  activeTimers: timers.size
}}));
"""
    result = subprocess.run(
        ["node", "-"],
        input=harness,
        check=True,
        capture_output=True,
        text=True,
    )
    state = json.loads(result.stdout)

    assert state == {
        "firstEnsure": "created",
        "secondEnsure": "exists",
        "created": 1,
        "nodeCount": 1,
        # textContent 写入：注入向量不产生子节点，原文原样展示
        "unsafeText": "<img src=x onerror=alert(1)>",
        "latestText": "second",
        # show() 路径与 ensure 路径操作同一元素（id 一致）
        "showText": "来自 show()",
        # 现行实现以 opacity 淡入淡出（无 display 切换）
        "opacity": "1",
        "firstTimerCleared": True,
        "activeTimers": 1,
    }
