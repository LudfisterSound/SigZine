"""Where the application keeps profiles, caches and recent files."""
from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(os.path.expanduser("~/Library/Application Support/Signature Zine")) \
    if os.uname().sysname == "Darwin" else Path(os.path.expanduser("~/.signature-zine"))


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
