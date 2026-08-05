"""Persistent settings stored in a JSON config file.

The file lives under the OS's standard per-user app-config location, which on
Windows is ``%LOCALAPPDATA%\\ConfluenceL33ch\\ConfluenceL33ch\\config.json``.
The path is exposed via :func:`config_path` so the user can inspect or edit it
directly, and the GUI prints it in the log panel at startup.

Secrets (PAT, session cookie) are persisted in **plain text**, and only when
the "Remember credentials" box is ticked — the default is off, so a fresh
install stores nothing sensitive. If you'd rather keep secrets out of the file
entirely, leave the box unticked and set ``CONFLUENCE_PAT`` /
``CONFLUENCE_COOKIE`` in your environment instead; the GUI falls back to those
whenever its own fields are blank.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import QStandardPaths


CONFIG_FILENAME = "config.json"
FALLBACK_APP_DIR = "ConfluenceL33ch"

# Keys never written to disk unless the user opts in. Kept as a module
# constant so the persistence code and this module's docstring can't drift.
SECRET_KEYS = ("pat", "cookie")


def config_path() -> Path:
    """Return the full path to the config file. The directory may not exist yet.

    Relies on QApplication.setOrganizationName / setApplicationName having
    been called before this is invoked (see ``app/main.py``). With those set,
    ``AppConfigLocation`` on Windows resolves to
    ``%LOCALAPPDATA%\\ConfluenceL33ch\\ConfluenceL33ch\\`` — we drop
    ``config.json`` into that directory.
    """
    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppConfigLocation
    )
    if base:
        return Path(base) / CONFIG_FILENAME
    return Path.home() / ".config" / FALLBACK_APP_DIR / CONFIG_FILENAME


def load_config() -> dict[str, Any]:
    """Return the stored config dict; empty dict if the file is missing/invalid.

    A missing or corrupt file is not an error: the GUI simply starts with its
    defaults, and the next save rewrites the file cleanly.
    """
    path = config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(data: dict[str, Any]) -> None:
    """Atomically write the config to disk, creating the directory if needed.

    Written to a temporary file and moved into place, so an interrupted write
    cannot leave a half-serialised config that fails to parse on next start.
    """
    path = config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        # Settings persistence is best-effort; never crash the UI over it.
        pass
