"""Entry point for the Catppuccin-themed Confluence L33ch GUI."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from . import __version__
from .config import load_config
from .main_window import MainWindow
from .theme import ACCENTS, DEFAULT_ACCENT, DEFAULT_FLAVOR, Flavor, apply_catppuccin


def main() -> int:
    app = QApplication(sys.argv)
    # Org + app name feed the QStandardPaths config-dir lookup, so on Windows
    # the settings file lands in %LOCALAPPDATA%\ConfluenceL33ch\ConfluenceL33ch\.
    app.setApplicationName("ConfluenceL33ch")
    app.setOrganizationName("ConfluenceL33ch")
    app.setApplicationVersion(__version__)

    # Apply Catppuccin before any widget is created so we don't have to
    # re-polish a tree of widgets that briefly rendered in the default theme.
    cfg = load_config()
    theme = cfg.get("theme") or {}
    flavor = Flavor.from_name(theme.get("flavor"))
    accent = theme.get("accent") if theme.get("accent") in ACCENTS else DEFAULT_ACCENT
    apply_catppuccin(app, flavor or DEFAULT_FLAVOR, accent)

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
