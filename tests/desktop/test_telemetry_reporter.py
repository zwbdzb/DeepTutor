from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import threading
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "desktop-shell"))

from desktop.telemetry_reporter import TelemetryReporter, os_description

BASE = {
    "machine_id": "device-1", "session_id": "session-1", "app_ver": "1.2.3",
    "os": "Windows 11 10.0.26100",
}


def test_report_includes_environment_and_oauth_header_only():
    reporter = TelemetryReporter("https://website.example/v1/events", BASE,
                                 access_token=lambda: "oauth-secret", flush_timeout=0.5)
    opener = Mock()
    opener.open.return_value = io.BytesIO(b'{"accepted":1}')
    props = {"nested": {"zero": 0, "flag": False}, "items": [1, None], "message": "测试"}
    with patch("desktop.telemetry_reporter.urllib.request.build_opener", return_value=opener):
        reporter.flush_exit("app_exit", props)
    request = opener.open.call_args.args[0]
    assert request.full_url == "https://website.example/v1/events"
    assert request.method == "POST"
    assert request.get_header("Authorization") == "Bearer oauth-secret"
    event = json.loads(request.data)["events"][0]
    assert event.items() >= BASE.items()
    assert event["props"] == props
    assert event["name"] == "app_exit"
    assert event["ts"] > 0
    assert "lost_before" not in event
    assert "oauth-secret" not in request.data.decode()
    assert "provider_user_id" not in event
    assert opener.open.call_args.kwargs == {"timeout": 0.5}


def test_identity_is_snapshotted_before_background_thread_runs():
    credential = ["account-a"]
    reporter = TelemetryReporter("https://website.example/v1/events", BASE,
                                 access_token=lambda: credential[0])
    opener = Mock()
    opener.open.return_value = io.BytesIO(b"{}")
    scheduled = []

    def defer_thread(*, target, args, **kwargs):
        scheduled.append((target, args))
        return Mock()

    with patch("desktop.telemetry_reporter.threading.Thread", side_effect=defer_thread), patch(
        "desktop.telemetry_reporter.urllib.request.build_opener", return_value=opener
    ):
        reporter.report("error_backend")
        credential[0] = "account-b"
        target, args = scheduled.pop()
        target(*args)
    assert opener.open.call_args.args[0].get_header("Authorization") == "Bearer account-a"


def test_reporting_does_not_wait_for_network():
    reporter = TelemetryReporter("https://website.example/v1/events", BASE)
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()

    def blocked_open(*args, **kwargs):
        entered.set()
        release.wait(timeout=3)
        completed.set()
        return io.BytesIO(b"{}")

    opener = Mock()
    opener.open.side_effect = blocked_open
    with patch("desktop.telemetry_reporter.urllib.request.build_opener", return_value=opener):
        try:
            reporter.report("app_start")
            assert entered.wait(timeout=2)
            assert not completed.is_set()
        finally:
            release.set()
            assert completed.wait(timeout=2)
    assert opener.open.call_args.args[0].get_header("Authorization") is None


def test_failed_reports_keep_circuit_breaker_and_recover():
    reporter = TelemetryReporter("https://website.example/v1/events", BASE, fail_threshold=2)
    opener = Mock()
    opener.open.side_effect = OSError("offline")
    with patch("desktop.telemetry_reporter.urllib.request.build_opener", return_value=opener):
        reporter.flush_exit("error_backend")
        reporter.flush_exit("error_backend")
        reporter.flush_exit("error_backend")  # muted; no third network request
        assert opener.open.call_count == 2
        reporter._muted_until = 0
        opener.open.side_effect = None
        opener.open.return_value = io.BytesIO(b"{}")
        reporter.flush_exit("app_exit")
    event = json.loads(opener.open.call_args.args[0].data)["events"][0]
    assert event["name"] == "app_exit"
    assert "lost_before" not in event
    assert opener.open.call_count == 3
    assert reporter._fails == 0


def test_error_rate_limit_does_not_block_normal_events():
    reporter = TelemetryReporter("https://website.example/v1/events", BASE, error_rate_limit=1)
    opener = Mock()
    opener.open.return_value = io.BytesIO(b"{}")
    with patch("desktop.telemetry_reporter.urllib.request.build_opener", return_value=opener):
        reporter.flush_exit("error_backend")
        reporter.flush_exit("error_backend")
        assert opener.open.call_count == 1
        reporter.flush_exit("app_exit")
    assert opener.open.call_count == 2


def test_redirect_does_not_forward_credentials_or_payload():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, self.headers.get("Authorization")))
            self.send_response(302)
            self.send_header("Location", "/redirect-target")
            self.end_headers()

        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            reporter = TelemetryReporter(f"http://127.0.0.1:{server.server_port}/v1/events", BASE,
                                         access_token=lambda: "oauth-secret")
            reporter.flush_exit("app_exit")
        finally:
            server.shutdown()
            worker.join(timeout=2)
    assert requests == [("/v1/events", "Bearer oauth-secret")]


def test_telemetry_failure_cannot_interrupt_business_flow():
    reporter = TelemetryReporter("https://website.example/v1/events", BASE)
    with patch("desktop.telemetry_reporter.threading.Thread", side_effect=RuntimeError("no threads")):
        reporter.report("login", access_token="oauth")
    with patch("desktop.telemetry_reporter.urllib.request.build_opener", side_effect=ValueError("bad URL")):
        reporter.flush_exit("app_exit")


@pytest.mark.parametrize("system,release,version,mac,want", [
    ("Windows", "11", "10.0.26100", "", "Windows 11 (10.0.26100)"),
    ("Darwin", "24.0.0", "unused", "15.0", "macOS 15.0"),
    ("Linux", "6.8.0", "unused", "", "Linux 6.8.0"),
])
def test_combined_os_description(system, release, version, mac, want):
    with patch("desktop.telemetry_reporter.platform.system", return_value=system), patch(
        "desktop.telemetry_reporter.platform.release", return_value=release
    ), patch("desktop.telemetry_reporter.platform.version", return_value=version), patch(
        "desktop.telemetry_reporter.platform.mac_ver", return_value=(mac, (), "")
    ):
        assert os_description() == want
