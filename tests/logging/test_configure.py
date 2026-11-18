import importlib
import json
import logging
import os
from pathlib import Path
import re
import stat
import sys

import pytest

from deeptutor.logging import LOG_FILE_NAME, LoggingConfig, bind_log_context

REPO_ROOT = Path(__file__).resolve().parents[2]
DESKTOP_SHELL = REPO_ROOT / "desktop-shell"


@pytest.fixture(autouse=True)
def _clean_logging_handlers():
    configure_module = importlib.import_module("deeptutor.logging.configure")
    configure_module._remove_managed_handlers(logging.getLogger())
    yield
    configure_module._remove_managed_handlers(logging.getLogger())


def _flush_root_handlers() -> None:
    for handler in logging.getLogger().handlers:
        handler.flush()


def test_cli_console_logs_can_use_stderr_without_polluting_stdout(monkeypatch, capsys):
    configure_module = importlib.import_module("deeptutor.logging.configure")
    monkeypatch.setattr(
        configure_module, "load_logging_config", lambda: LoggingConfig(file_output=False)
    )
    configure_module.configure_logging(force=True, console_stream=sys.stderr)
    logging.getLogger("deeptutor.tests.cli").info("setup diagnostic")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "setup diagnostic" in captured.err


def test_configure_logging_writes_jsonl_and_respects_level(monkeypatch, tmp_path: Path):
    configure_module = importlib.import_module("deeptutor.logging.configure")
    monkeypatch.setattr(
        configure_module,
        "load_logging_config",
        lambda: LoggingConfig(
            level="WARNING",
            console_output=False,
            file_output=True,
            log_dir=str(tmp_path),
            max_bytes=1024 * 1024,
            backup_count=1,
        ),
    )

    configure_module.configure_logging(force=True)
    logger = logging.getLogger("deeptutor.tests.config")
    with bind_log_context(request_id="req-1", task_id="task-1"):
        logger.info("filtered")
        logger.warning("written")
    _flush_root_handlers()

    lines = (tmp_path / LOG_FILE_NAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["level"] == "WARNING"
    assert entry["logger"] == "deeptutor.tests.config"
    assert entry["message"] == "written"
    assert entry["context"] == {"request_id": "req-1", "task_id": "task-1"}


def test_configure_logging_uses_rotation_settings(monkeypatch, tmp_path: Path):
    configure_module = importlib.import_module("deeptutor.logging.configure")
    monkeypatch.setattr(
        configure_module,
        "load_logging_config",
        lambda: LoggingConfig(
            level="INFO",
            console_output=False,
            file_output=True,
            log_dir=str(tmp_path),
            max_bytes=220,
            backup_count=1,
        ),
    )

    configure_module.configure_logging(force=True)
    logger = logging.getLogger("deeptutor.tests.rotation")
    for index in range(20):
        logger.info("rotation line %02d %s", index, "x" * 40)
    _flush_root_handlers()

    assert (tmp_path / LOG_FILE_NAME).exists()
    assert (tmp_path / f"{LOG_FILE_NAME}.1").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not authoritative")
def test_configure_logging_uses_private_directory_and_file_modes(monkeypatch, tmp_path: Path):
    configure_module = importlib.import_module("deeptutor.logging.configure")
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(
        configure_module,
        "load_logging_config",
        lambda: LoggingConfig(
            level="INFO",
            console_output=False,
            file_output=True,
            log_dir=str(log_dir),
            max_bytes=1024,
            backup_count=1,
        ),
    )

    configure_module.configure_logging(force=True)

    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((log_dir / LOG_FILE_NAME).stat().st_mode) == 0o600


def test_desktop_shell_tails_the_same_log_file_name():
    """Guard the duplicated literal.

    ``desktop-shell`` must start even when the engine runtime is missing or
    broken, so it cannot import ``deeptutor.logging`` and instead repeats the
    file name. This pins the two sides together: renaming one without the other
    would silently stop shell-side error telemetry.
    """
    # Only the shell's own source package: ``runtime-build/`` holds a copy of
    # the whole interpreter and would both dwarf the scan and match third-party
    # file names that are none of our business.
    source_root = DESKTOP_SHELL / "desktop"
    if not source_root.is_dir():
        pytest.skip("desktop shell is not checked out")

    offenders: dict[str, list[str]] = {}
    for source in sorted(source_root.rglob("*.py")):
        # Bytes, not text: a stray non-UTF-8 source file must not blow up a
        # naming check, and the pattern is pure ASCII either way.
        for match in re.finditer(rb"[\w.-]+\.jsonl", source.read_bytes()):
            name = match.group(0).decode("ascii")
            if name != LOG_FILE_NAME:
                offenders.setdefault(source.relative_to(REPO_ROOT).as_posix(), []).append(name)

    assert offenders == {}, f"desktop shell still references a non-branded log file: {offenders}"
