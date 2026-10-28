# -*- coding: utf-8 -*-
"""Control-flow test for DeepTutorProcess.supervise with fake processes."""
import sys, time, types, threading
sys.path.insert(0, r"D:\studio\DeepTutor\desktop-shell")

from desktop.process import DeepTutorProcess, describe_exit_code

# --- exit-code hint table sanity -------------------------------------------
assert "DBG" in describe_exit_code(1073807364) or "关机" in describe_exit_code(1073807364)
assert "TerminateProcess" in describe_exit_code(1)
assert describe_exit_code(-1073741819) != "", "signed input must normalize"
assert describe_exit_code(42) == ""
print("[ok] describe_exit_code table")

class FakeProc:
    def __init__(self, rc):
        self.rc = rc
        self.pid = 12345
    def poll(self):
        return self.rc

def make_proc():
    p = DeepTutorProcess.__new__(DeepTutorProcess)   # bypass __init__ (no runtime needed)
    p._stopping = False
    p.frontend_ready = threading.Event()
    p.frontend_launching = threading.Event()
    return p

# --- scenario 1: two crashes then stable -> recovered -----------------------
proc = make_proc()
proc.proc = FakeProc(1)   # initial child already crashed (simulates 14:28)
calls = {"start": 0, "stop": 0, "wait": 0}
attempt = {"n": 0}

def fake_start(self):
    calls["start"] += 1
    attempt["n"] += 1
    self.proc = FakeProc(1 if attempt["n"] <= 2 else None)  # 3rd stays alive
    self.child_log_path = None
proc.start = types.MethodType(fake_start, proc)
proc.stop = types.MethodType(lambda self, timeout=10.0: calls.__setitem__("stop", calls["stop"] + 1), proc)
proc.wait_ready = types.MethodType(lambda self, timeout=180.0: (calls.__setitem__("wait", calls["wait"] + 1), ("http://127.0.0.1:3782", 200))[1], proc)

events = []
proc.supervise(on_event=lambda k, m: events.append((k, m)),
               max_restarts=3, backoff=0.2, stability_window=600.0)

deadline = time.monotonic() + 25
while time.monotonic() < deadline:
    if any(k == "recovered" for k, _ in events):
        break
    time.sleep(0.2)

kinds = [k for k, _ in events]
assert kinds[0] == "child_exit", kinds
assert kinds.count("restarting") == 3, kinds   # 3 starts: initial-crash + 2 fake crashes... adjust below
assert "recovered" in kinds, kinds
assert calls["start"] == 3 and calls["wait"] == 3, calls
print("[ok] scenario1 recovered:", kinds)
print("[ok] scenario1 calls:", calls)

# --- scenario 2: always-crash loop is bounded, _stopping aborts --------------
events2 = []
proc2 = make_proc()
proc2.proc = FakeProc(1)
def fake_start2(self):
    self.proc = FakeProc(1)   # every child dies
    self.child_log_path = None
proc2.start = types.MethodType(fake_start2, proc2)
proc2.stop = types.MethodType(lambda self, timeout=10.0: None, proc2)
def fake_wait2(self, timeout=180.0):
    raise RuntimeError("frontend never ready")
proc2.wait_ready = types.MethodType(fake_wait2, proc2)

proc2.supervise(on_event=lambda k, m: events2.append((k, m)),
                max_restarts=2, backoff=0.2, stability_window=600.0)
time.sleep(1.5)
proc2._stopping = True    # simulate window close (stop() sets it)
deadline = time.monotonic() + 10
while time.monotonic() < deadline and "gave_up" not in [k for k, _ in events2]:
    time.sleep(0.2)
assert "gave_up" in [k for k, _ in events2], events2
print("[ok] scenario2 bounded: gave_up emitted after max_restarts")
print("ALL SUPERVISE TESTS PASS")
