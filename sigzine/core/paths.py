"""Where the application keeps profiles, caches and recent files."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _app_dir() -> Path:
    """Where this platform expects an application to keep its data.

    Deliberately not inside the project folder: on a Mac that folder is
    often in iCloud Drive, which will happily sync a virtual environment to
    pieces behind your back.
    """
    home = Path(os.path.expanduser("~"))
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Signature Zine"
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA")
        return (Path(base) if base else home / "AppData" / "Roaming") \
            / "Signature Zine"
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) / "signature-zine") if base \
        else home / ".signature-zine"


APP_DIR = _app_dir()


def ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def app_dir() -> Path:
    return ensure(APP_DIR)


def printers_dir() -> Path:
    return ensure(APP_DIR / "printers")


def cache_dir() -> Path:
    return ensure(APP_DIR / "cache")


def settings_file() -> Path:
    return app_dir() / "settings.json"
