"""Keep a replaced onefile executable from reusing an older extraction directory."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def ensure_fresh_bundle(expected_id: str) -> None:
    """Restart before application imports when a legacy updater inherits old assets."""
    if not getattr(sys, "frozen", False):
        return

    try:
        actual_id = (Path(sys._MEIPASS) / "thinkbuddy-bundle-id.txt").read_text(encoding="ascii")
    except OSError:
        actual_id = ""
    if actual_id == expected_id:
        return

    # Old updaters pass _PYI_* through cmd, so the new EXE can reuse old DLLs/data.
    env = os.environ.copy()
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    subprocess.Popen([sys.executable, *sys.argv[1:]], env=env)
    os._exit(0)
