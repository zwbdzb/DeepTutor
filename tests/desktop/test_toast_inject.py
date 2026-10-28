"""desktop.inject toast 注入脚本的浏览器侧行为回归（node 子进程执行）。

契约（与 inject.py 对齐）：

* ``TOAST_ENSURE_JS`` / ``TOAST_SHOW_JS`` 是含 ``%(id)s`` 等占位符的模板，
  调用侧必须先 ``% {"id": TOAST_ID, ...}`` 格式化再 ``evaluate_js``；
  本测试用与生产一致的格式化产物喂给 harness。
* ensure 幂等：首次 'created'、再次 'exists'，且只建一个 DOM 节点。
* 隐藏计时器存在 ``window.__thinkbuddyToastTimer`` 单一槽位：再次弹 toast
  必须先 clearTimeout 旧计时器（不叠层）。
* 文案经 ``textContent`` 写入，HTML 注入向量（<img onerror=...>）不产生子节点。
* ``ToastInjector.show()`` 路径（TOAST_SHOW_JS）必须能找到 ensure 建立的
  同一元素（id 一致），否则 show 静默失效。
* 语义圆点（2026-10 主题化改造）：ensure 幂等注入
  ``<style id="thinkbuddy-toast-style">`` 到 head，圆点按 ``data-kind`` 变色
  （info=蓝 / success=绿 / error=红，走页面 CSS 变量 + 内置 fallback）；
  ``TOAST_SHOW_JS`` 与全局 ``__thinkbuddyToast`` 都把 kind 写进元素
  ``dataset.kind``，两参旧调用缺省归 'info'。
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
        "kind": json.dumps("info", ensure_ascii=False),
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
window.__thinkbuddyToast('<img src=x onerror=alert(1)>');
const toast = nodes.get('thinkbuddy-toast');
const unsafeText = toast.textContent;
const firstTimer = window.__thinkbuddyToastTimer;
window.__thinkbuddyToast('second');
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
        # node 的 stdin/stdout 均为 UTF-8；显式指定以免 Windows GBK locale
        # 把中文断言（textContent 往返）解成乱码。
        encoding="utf-8",
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


# --------------------------------------------------------------------------- #
# data-kind 语义圆点（2026-10 主题化改造）----------------------------------- #
# --------------------------------------------------------------------------- #

# head-capable DOM harness：与真实 DOM 对齐（div.dataset 恒存在、head 可挂
# style 元素），专供 data-kind / 样式注入断言使用。上面首条测试的极简
# harness（元素无 dataset、无 head）继续覆盖「dataset 缺失不炸」的防御分支，
# 两者互补。
DOM_HARNESS_PREFIX = """
const nodes = new Map();
const timers = new Map();
const cleared = [];
let created = 0;
let nextTimer = 0;
const makeElement = () => ({ style: {}, dataset: {}, textContent: '' });
global.document = {
  body: { appendChild(node) { nodes.set(node.id, node); } },
  head: { appendChild(node) { nodes.set(node.id, node); } },
  getElementById(id) { return nodes.get(id) || null; },
  createElement() { created += 1; return makeElement(); }
};
global.window = {};
global.setTimeout = (fn, delay) => {
  const id = ++nextTimer;
  timers.set(id, { fn, delay });
  return id;
};
global.clearTimeout = (id) => { cleared.push(id); timers.delete(id); };
"""


def _run_node_js(statements: str) -> dict:
    """在 node 子进程执行 statements（自动前置 DOM harness），回传 JSON 状态。"""
    if shutil.which("node") is None:
        pytest.skip("Node.js is required to execute the desktop toast script")
    result = subprocess.run(
        ["node", "-"],
        input=DOM_HARNESS_PREFIX + statements,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


def test_toast_ensure_js_renders_kind_selectors_and_style_element() -> None:
    """ensure 渲染产物含语义选择器；style 元素幂等注入且按 kind 变色。"""
    rendered = TOAST_ENSURE_JS % {"id": TOAST_ID}

    # 渲染产物完整性：占位符已全部替换，主题化样式与语义选择器就位
    assert "%(id)s" not in rendered
    assert 'data-kind="success"' in rendered
    assert 'data-kind="error"' in rendered
    assert "thinkbuddy-toast-style" in rendered
    assert "display:flex" in rendered
    assert "max-width:min(520px, calc(100vw - 48px))" in rendered
    assert "var(--popover," in rendered

    state = _run_node_js(
        "const ensureScript = " + json.dumps(rendered) + ";\n"
        + """
const firstEnsure = eval(ensureScript);
const secondEnsure = eval(ensureScript);
const styleEl = document.getElementById('thinkbuddy-toast-style');
process.stdout.write(JSON.stringify({
  firstEnsure,
  secondEnsure,
  created,
  nodeCount: nodes.size,
  styleId: styleEl ? styleEl.id : null,
  styleText: styleEl ? styleEl.textContent : null
}));
"""
    )
    assert state["firstEnsure"] == "created"
    assert state["secondEnsure"] == "exists"
    # toast div + style 元素各建一次，二次 ensure 幂等不重复建
    assert state["created"] == 2
    assert state["nodeCount"] == 2
    assert state["styleId"] == "thinkbuddy-toast-style"
    # 语义圆点：info 默认蓝、success 绿、error 红（CSS 变量 + 内置 fallback）
    assert "#thinkbuddy-toast::before" in state["styleText"]
    assert 'data-kind="success"' in state["styleText"]
    assert 'data-kind="error"' in state["styleText"]
    assert "--info" in state["styleText"]
    assert "--success" in state["styleText"]
    assert "--destructive" in state["styleText"]


@pytest.mark.parametrize("kind", ["info", "success", "error"])
def test_toast_show_js_sets_data_kind_on_element(kind: str) -> None:
    """TOAST_SHOW_JS 按 kind 贯通：dataset.kind 正确写入并驱动同一元素。"""
    show_js = TOAST_SHOW_JS % {
        "id": TOAST_ID,
        "msg": json.dumps(f"kind={kind}", ensure_ascii=False),
        "duration": 3500,
        "kind": json.dumps(kind, ensure_ascii=False),
    }
    state = _run_node_js(
        "const ensureScript = "
        + json.dumps(TOAST_ENSURE_JS % {"id": TOAST_ID}) + ";\n"
        + "const showScript = " + json.dumps(show_js) + ";\n"
        + """
eval(ensureScript);
eval(showScript);
const toast = nodes.get('thinkbuddy-toast');
process.stdout.write(JSON.stringify({
  text: toast.textContent,
  kind: toast.dataset.kind,
  opacity: toast.style.opacity,
  created
}));
"""
    )
    assert state["text"] == f"kind={kind}"
    assert state["kind"] == kind
    assert state["opacity"] == "1"
    assert state["created"] == 2


def test_global_toast_two_arg_call_defaults_to_info() -> None:
    """旧两参 window.__thinkbuddyToast(msg, duration) 兼容：kind 缺省归 'info'。"""
    state = _run_node_js(
        "const ensureScript = "
        + json.dumps(TOAST_ENSURE_JS % {"id": TOAST_ID}) + ";\n"
        + """
eval(ensureScript);
window.__thinkbuddyToast('两参兼容调用', 2000);
const toast = nodes.get('thinkbuddy-toast');
const twoArgKind = toast.dataset.kind;
window.__thinkbuddyToast('单参兜底调用');
process.stdout.write(JSON.stringify({
  mounted: typeof window.__thinkbuddyToast,
  twoArgKind,
  oneArgKind: toast.dataset.kind,
  oneArgText: toast.textContent,
  opacity: toast.style.opacity
}));
"""
    )
    assert state["mounted"] == "function"
    assert state["twoArgKind"] == "info"
    assert state["oneArgKind"] == "info"
    assert state["oneArgText"] == "单参兜底调用"
    assert state["opacity"] == "1"
