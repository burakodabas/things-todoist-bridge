"""Install/uninstall a LaunchAgent so the bridge runs in the background at login."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import data_dir

LABEL = "io.github.ttbridge"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"

MENU_LABEL = "io.github.ttbridge.menu"
MENU_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{MENU_LABEL}.plist"
MENU_APP = Path.home() / "Applications" / "Things Todoist Bridge.app"
MENU_SOURCE = Path(__file__).resolve().parent / "menubar" / "TTBridgeMenu.swift"


class MenuBarError(RuntimeError):
    pass


def _load(plist_path: Path, plist: dict) -> None:
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    if plist_path.exists():
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    with open(plist_path, "wb") as fh:
        plistlib.dump(plist, fh)
    subprocess.run(["launchctl", "load", "-w", str(plist_path)], check=True)


def _unload(plist_path: Path) -> bool:
    if not plist_path.exists():
        return False
    subprocess.run(["launchctl", "unload", "-w", str(plist_path)], capture_output=True)
    plist_path.unlink()
    return True


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
    _load(PLIST, plist)
    return PLIST


def uninstall() -> bool:
    return _unload(PLIST)


# ---- menu bar icon ------------------------------------------------------------
def menubar_install(port: int) -> Path:
    """Compile the menu bar app (needs the Xcode command line tools) and start it at login."""
    swiftc = shutil.which("swiftc")
    if not swiftc:
        raise MenuBarError("swiftc not found. Install the command line tools with: xcode-select --install")
    macos = MENU_APP / "Contents" / "MacOS"
    with tempfile.TemporaryDirectory() as tmp:
        binary = Path(tmp) / "TTBridgeMenu"
        out = subprocess.run([swiftc, "-O", str(MENU_SOURCE), "-o", str(binary)], capture_output=True, text=True)
        if out.returncode != 0:
            raise MenuBarError("Building the menu bar app failed:\n" + (out.stderr or out.stdout)[-2000:])
        menubar_stop()
        if MENU_APP.exists():
            shutil.rmtree(MENU_APP)
        macos.mkdir(parents=True)
        shutil.copy2(binary, macos / "TTBridgeMenu")
    with open(MENU_APP / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump({
            "CFBundleName": "Things Todoist Bridge",
            "CFBundleIdentifier": MENU_LABEL,
            "CFBundleExecutable": "TTBridgeMenu",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": _version(),
            "LSUIElement": True,
            "LSMinimumSystemVersion": "11.0",
        }, fh)
    _load(MENU_PLIST, {
        "Label": MENU_LABEL,
        "ProgramArguments": [str(macos / "TTBridgeMenu"), "--port", str(port), "--agent-label", LABEL],
        "RunAtLoad": True,
        "LimitLoadToSessionType": "Aqua",
        "ProcessType": "Interactive",
    })
    return MENU_APP


def menubar_stop() -> None:
    subprocess.run(["launchctl", "unload", str(MENU_PLIST)], capture_output=True)


def menubar_uninstall() -> bool:
    removed = _unload(MENU_PLIST)
    if MENU_APP.exists():
        shutil.rmtree(MENU_APP)
        removed = True
    return removed


def _version() -> str:
    from . import __version__
    return __version__


def status() -> str:
    def one(label: str) -> str:
        out = subprocess.run(["launchctl", "list", label], capture_output=True, text=True)
        return "running" if out.returncode == 0 else "not running"
    return f"background sync: {one(LABEL)}\nmenu bar icon:   {one(MENU_LABEL)}"
