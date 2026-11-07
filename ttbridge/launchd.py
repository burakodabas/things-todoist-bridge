"""Install/uninstall a LaunchAgent so the bridge runs in the background at login."""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

from .config import data_dir

LABEL = "io.github.ttbridge"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def install(port: int) -> Path:
    log_dir = data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "ttbridge", "serve", "--no-browser", "--port", str(port)],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "StandardOutPath": str(log_dir / "agent.out.log"),
        "StandardErrorPath": str(log_dir / "agent.err.log"),
        "EnvironmentVariables": {"PYTHONPATH": str(Path(__file__).resolve().parent.parent)},
        "WorkingDirectory": str(data_dir()),
    }
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    if PLIST.exists():
        subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)
    with open(PLIST, "wb") as fh:
        plistlib.dump(plist, fh)
    subprocess.run(["launchctl", "load", "-w", str(PLIST)], check=True)
    return PLIST


def uninstall() -> bool:
    if not PLIST.exists():
        return False
    subprocess.run(["launchctl", "unload", "-w", str(PLIST)], capture_output=True)
    PLIST.unlink()
    return True


def status() -> str:
    out = subprocess.run(["launchctl", "list", LABEL], capture_output=True, text=True)
    if out.returncode != 0:
        return "not loaded"
    return "loaded"
