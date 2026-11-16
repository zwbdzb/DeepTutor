"""Logging configuration loaded from runtime settings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: Product-facing name of the structured JSONL log.
#:
#: The engine *package* is ``deeptutor`` and its logger namespaces stay that
#: way (they are an import path, not a brand), but the file a user opens,
#: attaches to a bug report, or finds in ``data/user/logs`` is a ThinkBuddy
#: artifact. Keep this the single source of truth on the engine side; the
#: desktop shell carries its own literal because it must start even when the
#: engine runtime is missing or broken (see ``desktop-shell/desktop/main.py``).
LOG_FILE_NAME = "thinkbuddy.jsonl"


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    console_output: bool = True
    file_output: bool = True
    log_dir: str | None = None
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 5


def get_default_log_dir() -> Path:
    from deeptutor.services.path_service import get_path_service

    return get_path_service().get_logs_dir()


def load_logging_config() -> LoggingConfig:
    """Load logging settings from ``data/user/settings/main.yaml``."""
    try:
        from deeptutor.services.config import (
            PROJECT_ROOT,
            get_path_from_config,
            load_config_with_main,
        )

        config = load_config_with_main("main.yaml", PROJECT_ROOT)
        logging_config = config.get("logging", {}) or {}
        return LoggingConfig(
            level=str(logging_config.get("level", "INFO")).upper(),
            console_output=bool(logging_config.get("console_output", True)),
            file_output=bool(logging_config.get("save_to_file", True)),
            log_dir=get_path_from_config(config, "user_log_dir"),
            max_bytes=int(logging_config.get("max_bytes", 10 * 1024 * 1024)),
            backup_count=int(logging_config.get("backup_count", 5)),
        )
    except Exception:
        return LoggingConfig(log_dir=str(get_default_log_dir()))


def get_global_log_level() -> str:
    return load_logging_config().level
