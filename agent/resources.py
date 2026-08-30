"""Resource resolution that works both in development and inside a PyInstaller
--onefile bundle, plus a writable location for the demo database.

Two traps handled here:
  1. Path(__file__) is wrong inside a bundle; PyInstaller unpacks to sys._MEIPASS.
  2. The bundle directory is read-only, but SQLite opens a journal even for a
     transaction that ends in ROLLBACK. So the demo DB is copied out on first run.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

APP_NAME = "SafeMigrationGate"


def resource_path(relative: str) -> Path:
    """Resolve a bundled resource. `relative` is always POSIX-style and relative
    to the repo root, e.g. "data/demo.db" or "agent/knowledge"."""
    meipass = getattr(sys, "_MEIPASS", None)
    base = Path(meipass) if meipass else Path(__file__).resolve().parent.parent
    return base.joinpath(*relative.split("/"))


def user_data_dir() -> Path:
    """A per-user writable directory: %LOCALAPPDATA%\\SafeMigrationGate on Windows."""
    import os

    root = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    path = Path(root) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_demo_db() -> Path:
    """Copy the bundled demo database to a writable location on first run.
    Never overwrites an existing copy — the user may have pointed at it."""
    destination = user_data_dir() / "demo.db"
    if not destination.exists():
        shutil.copy2(resource_path("data/demo.db"), destination)
    return destination


def demo_db_url() -> str:
    return f"sqlite:///{ensure_demo_db()}"


def write_text_file(path: Path, content: str) -> Path:
    """Write UTF-8, creating parent directories. Reports contain Hebrew, and the
    Windows default encoding (cp1255) would mangle it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path
