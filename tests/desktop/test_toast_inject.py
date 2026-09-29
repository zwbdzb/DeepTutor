from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

DESKTOP_SHELL = Path(__file__).resolve().parents[2] / "desktop-shell"
sys.path.insert(0, str(DESKTOP_SHELL))

from desktop.inject import TOAST_ENSURE_JS


def test_toast_script_is_idempotent_and_resets_hide_timer() -> None:
    if shutil.which("node") is None:
        pytest.skip("Node.js is required to execute the desktop toast script")

    harness = f"""
const script = {json.dumps(TOAST_ENSURE_JS)};
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

const firstEnsure = eval(script);
const secondEnsure = eval(script);
window.__edubuddyToast('<img src=x onerror=alert(1)>');
const toast = nodes.get('edubuddy-toast');
const firstTimer = toast.__hideTimer;
const unsafeText = toast.textContent;
window.__edubuddyToast('second');

process.stdout.write(JSON.stringify({{
  firstEnsure,
  secondEnsure,
  created,
  nodeCount: nodes.size,
  unsafeText,
  latestText: toast.textContent,
  display: toast.style.display,
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
        "unsafeText": "<img src=x onerror=alert(1)>",
        "latestText": "second",
        "display": "block",
        "firstTimerCleared": True,
        "activeTimers": 1,
    }
