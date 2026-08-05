"""Catppuccin-themed Confluence L33ch main window."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QByteArray, QRect, QSize, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QSpinBox,
    QStatusBar,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .config import config_path, load_config, save_config
from .cookie_import import CookiePasteDialog, cookie_names
from .confluence_client import (
    DEFAULT_API_PATH,
    DEFAULT_BASE_URL,
    ConfluenceClient,
    ConfluenceError,
    Credentials,
    PageRef,
)
from .discovery import DiscoveryRequest, DiscoveryWorker
from .md_to_pdf import MdToPdfWorker, wkhtmltopdf_version
from .theme import (
    ACCENTS,
    Flavor,
    PALETTES,
    apply_catppuccin,
    current_accent_hex,
    current_palette,
    leech_svg,
    refresh_widgets,
)
from .worker import (
    ExportOptions,
    ExportWorker,
    STATE_FILENAME,
    run_in_thread,
    wait_for_threads,
)


PAGE_ROLE = Qt.ItemDataRole.UserRole
ROOT_ROLE = Qt.ItemDataRole.UserRole + 1

API_PATH_CHOICES = ("/rest/api", "/wiki/rest/api", "/confluence/rest/api")

EXPORT_FORMATS = (
    ("Markdown (.md)", "md"),
    ("PDF (server export)", "pdf"),
    ("Both", "both"),
)


def _svg_pixmap(svg: str, size: QSize) -> QPixmap:
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pixmap = QPixmap(size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return pixmap


PREFERRED_WINDOW_SIZE = QSize(1040, 840)


def _initial_window_size() -> QSize:
    """The default window size, clamped to fit the screen it opens on.

    The layout's own minimum is what ultimately wins, so this is about not
    *asking* for a window taller than the display — a 1366x768 laptop would
    otherwise get a window with its action row off-screen.
    """
    screen = QApplication.primaryScreen()
    if screen is None:
        return PREFERRED_WINDOW_SIZE
    available = screen.availableGeometry()
    return QSize(
        min(PREFERRED_WINDOW_SIZE.width(), available.width() - 40),
        min(PREFERRED_WINDOW_SIZE.height(), available.height() - 60),
    )


def _open_in_explorer(path: Path) -> None:
    """Reveal a folder in the platform file manager, best-effort."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # noqa: S606 — user-initiated, no shell
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError:
        pass


class PageListDelegate(QStyledItemDelegate):
    """Paints a small ``ROOT`` pill on the subtree root.

    Colours are pulled from the active Catppuccin palette at paint time so a
    flavor switch propagates without rebuilding the delegate.
    """

    BADGE_TEXT = "ROOT"
    BADGE_MARGIN_RIGHT = 8
    BADGE_PAD_X = 8
    BADGE_PAD_Y = 2

    @staticmethod
    def _badge_fill() -> QColor:
        # Peach reads as "this one is special" without the alarm of Red.
        return QColor(current_palette().peach)

    @staticmethod
    def _badge_text_color() -> QColor:
        p = current_palette()
        return QColor(p.base if p.is_dark else p.crust)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)

        if index.data(ROOT_ROLE):
            badge_w, badge_h = self._badge_size(opt)
            opt.rect = QRect(
                opt.rect.left(),
                opt.rect.top(),
                opt.rect.width() - badge_w - self.BADGE_MARGIN_RIGHT * 2,
                opt.rect.height(),
            )

        super().paint(painter, opt, index)

        if not index.data(ROOT_ROLE):
            return

        full_rect = option.rect
        badge_w, badge_h = self._badge_size(opt)
        x = full_rect.right() - badge_w - self.BADGE_MARGIN_RIGHT
        y = full_rect.center().y() - badge_h // 2 + 1
        rect = QRect(x, y, badge_w, badge_h)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.setBrush(self._badge_fill())
        painter.drawRoundedRect(rect, badge_h / 2, badge_h / 2)
        painter.setPen(self._badge_text_color())
        painter.setFont(self._badge_font(option.font))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.BADGE_TEXT)
        painter.restore()

    def _badge_font(self, base: QFont) -> QFont:
        f = QFont(base)
        f.setPointSize(max(7, base.pointSize() - 2))
        f.setBold(True)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
        return f

    def _badge_size(self, option: QStyleOptionViewItem) -> tuple[int, int]:
        fm = option.fontMetrics
        w = fm.horizontalAdvance(self.BADGE_TEXT) + self.BADGE_PAD_X * 2
        h = fm.height() + self.BADGE_PAD_Y * 2
        return w, h


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"Confluence L33ch  v{__version__}")
        self.resize(_initial_window_size())
        # The Catppuccin stylesheet is set on QApplication in app/main.py;
        # we don't override it per-window so flavor switches reach every
        # widget without an extra hop.

        self._thread = None
        self._worker = None
        self._discovery_thread = None
        self._discovery_worker: DiscoveryWorker | None = None
        self._pdf_thread = None
        self._pdf_worker: MdToPdfWorker | None = None
        # Set when a scheduled run kicks off discovery, so the export starts
        # automatically once the page list comes back.
        self._auto_export_after_discovery = False
        # UA of the browser that produced the current cookie, if any.
        self._captured_user_agent = ""

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_hero())

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(24, 12, 24, 10)
        body_layout.setSpacing(9)

        top_row = QHBoxLayout()
        top_row.setSpacing(12)
        top_row.addWidget(self._build_connection_group(), stretch=1)
        top_row.addWidget(self._build_scope_group(), stretch=1)
        body_layout.addLayout(top_row)

        body_layout.addLayout(self._build_output_row())

        body_layout.addWidget(self._build_section_label("Pages to export"))
        body_layout.addLayout(self._build_page_controls())
        self.page_list = QListWidget()
        self.page_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.page_list.setMinimumHeight(96)
        self.page_list.setItemDelegate(PageListDelegate(self.page_list))
        body_layout.addWidget(self.page_list, stretch=2)

        body_layout.addWidget(self._build_options_group())

        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFormat("%v / %m")
        progress_row = QHBoxLayout()
        progress_row.setSpacing(10)
        progress_row.addWidget(self._build_section_label("Progress"))
        progress_row.addWidget(self.progress, stretch=1)
        body_layout.addLayout(progress_row)

        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("LogView")
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(84)
        body_layout.addWidget(self.log_view, stretch=1)

        body_layout.addLayout(self._build_action_row())

        root.addWidget(body, stretch=1)

        status = QStatusBar()
        status.showMessage("Ready.")
        self.setStatusBar(status)
        self._add_theme_picker(status)

        # Repeat-run timer: re-runs discovery + export on an interval so the
        # export can track a space without being launched by hand.
        self._repeat_timer = QTimer(self)
        self._repeat_timer.setSingleShot(True)
        self._repeat_timer.timeout.connect(self._on_repeat_timeout)

        # Debounced auto-save: every change schedules a write 500 ms later.
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self._persist_settings)
        self._suppress_save = True
        self._load_settings()
        self._wire_persistence_signals()
        self._suppress_save = False

        self._on_auth_mode_changed()
        self._on_repeat_toggled(self.repeat_check.isChecked())
        self._update_count()
        self.log_view.appendPlainText(
            f"Settings file: {config_path()}\n"
            "Fill in the connection and scope, then click 'Discover pages'."
        )

    # --- Theme picker -------------------------------------------------

    def _add_theme_picker(self, status: QStatusBar) -> None:
        """Add a "Theme: [Flavor] / [Accent]" picker to the status bar's
        right-hand side. Swaps the entire Catppuccin theme at runtime."""
        wrapper = QWidget()
        row = QHBoxLayout(wrapper)
        row.setContentsMargins(0, 0, 6, 0)
        row.setSpacing(6)

        row.addWidget(QLabel("Theme:"))
        self.flavor_combo = QComboBox()
        for flavor in (Flavor.LATTE, Flavor.FRAPPE, Flavor.MACCHIATO, Flavor.MOCHA):
            self.flavor_combo.addItem(PALETTES[flavor].name, flavor)
        self.flavor_combo.setCurrentText(current_palette().name)
        self.flavor_combo.setToolTip(
            "Catppuccin flavor — Latte (light), Frappé / Macchiato / Mocha (dark)."
        )
        self.flavor_combo.currentIndexChanged.connect(self._on_theme_changed)
        row.addWidget(self.flavor_combo)

        self.accent_combo = QComboBox()
        for accent in ACCENTS:
            self.accent_combo.addItem(accent.capitalize(), accent)
        from .theme import current_accent_name
        idx = self.accent_combo.findData(current_accent_name())
        if idx >= 0:
            self.accent_combo.setCurrentIndex(idx)
        self.accent_combo.setToolTip("Accent colour — drives buttons, links, selection.")
        self.accent_combo.currentIndexChanged.connect(self._on_theme_changed)
        row.addWidget(self.accent_combo)

        status.addPermanentWidget(wrapper)

    def _on_theme_changed(self) -> None:
        flavor = self.flavor_combo.currentData()
        accent = self.accent_combo.currentData()
        if flavor is None or accent is None:
            return
        app = QApplication.instance()
        if app is None:
            return
        apply_catppuccin(app, flavor, accent)
        refresh_widgets(app.allWidgets())
        self._refresh_hero_logo()
        self.page_list.viewport().update()  # repaint ROOT badges
        self._schedule_save()

    def _refresh_hero_logo(self) -> None:
        """Re-render the siphon mark in the active accent."""
        if hasattr(self, "_hero_logo"):
            self._hero_logo.setPixmap(
                _svg_pixmap(leech_svg(current_accent_hex()), QSize(40, 40))
            )

    # --- UI builders ---------------------------------------------------

    def _build_hero(self) -> QFrame:
        hero = QFrame()
        hero.setObjectName("HeroFrame")
        hero.setFixedHeight(72)
        layout = QHBoxLayout(hero)
        layout.setContentsMargins(28, 10, 28, 10)
        layout.setSpacing(16)

        logo_label = QLabel()
        self._hero_logo = logo_label
        self._refresh_hero_logo()
        logo_label.setStyleSheet("background: transparent;")
        layout.addWidget(logo_label, alignment=Qt.AlignmentFlag.AlignTop)

        text_box = QVBoxLayout()
        text_box.setSpacing(2)
        title = QLabel("Confluence L33ch")
        title.setObjectName("HeroTitle")
        subtitle = QLabel(
            "Siphon Confluence spaces and page trees into clean local Markdown or PDF."
        )
        subtitle.setObjectName("HeroSubtitle")
        text_box.addWidget(title)
        text_box.addWidget(subtitle)
        text_box.addStretch(1)
        layout.addLayout(text_box, stretch=1)

        return hero

    def _build_section_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("SectionLabel")
        return label

    def _build_connection_group(self) -> QGroupBox:
        group = QGroupBox("Connection")
        form = QFormLayout(group)
        form.setSpacing(5)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.base_url_edit = QLineEdit(DEFAULT_BASE_URL)
        # DEFAULT_BASE_URL is empty on purpose — there is no sensible default
        # instance, so the field starts blank and is validated before use.
        self.base_url_edit.setPlaceholderText("https://confluence.example.com")
        self.base_url_edit.setToolTip(
            "Base URL of the Confluence Server / Data Center instance.\n"
            "Do NOT include the /wiki or /rest/api part — that's the API path below."
        )
        form.addRow("Base URL:", self.base_url_edit)

        self.api_path_combo = QComboBox()
        self.api_path_combo.setEditable(True)
        for choice in API_PATH_CHOICES:
            self.api_path_combo.addItem(choice)
        self.api_path_combo.setCurrentText(DEFAULT_API_PATH)
        self.api_path_combo.setToolTip(
            "REST API path. /rest/api suits most Server/DC installs; instances\n"
            "behind a context path or reverse proxy use /wiki/rest/api or\n"
            "/confluence/rest/api. A 404 on every request means this is wrong."
        )
        form.addRow("API path:", self.api_path_combo)

        self.auth_mode_combo = QComboBox()
        self.auth_mode_combo.addItem("Bearer (PAT)", "bearer")
        self.auth_mode_combo.addItem("Basic (user + token)", "basic")
        self.auth_mode_combo.setToolTip(
            "Bearer sends the Personal Access Token as 'Authorization: Bearer'.\n"
            "Basic sends username:token — needed by older instances and by\n"
            "Atlassian Cloud API tokens."
        )
        self.auth_mode_combo.currentIndexChanged.connect(self._on_auth_mode_changed)
        form.addRow("Auth mode:", self.auth_mode_combo)

        self.username_edit = QLineEdit()
        self.username_edit.setPlaceholderText("(Basic auth only)")
        self.username_label = QLabel("Username:")
        form.addRow(self.username_label, self.username_edit)

        self.pat_edit = QLineEdit()
        self.pat_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pat_edit.setPlaceholderText("(blank = use CONFLUENCE_PAT env var)")
        self.pat_edit.setToolTip(
            "Personal Access Token (Confluence 7.9+ Server / Data Center).\n\n"
            "To create one: avatar (top right) → Settings → Personal access\n"
            "tokens → Create token → name it, optionally set an expiry → Create.\n"
            "The token is shown ONCE — copy it before closing the dialog.\n"
            "Direct link: <your-host>/plugins/personalaccesstokens/manage-tokens.action\n\n"
            "It carries your own permissions, nothing more. Left blank, the\n"
            "CONFLUENCE_PAT environment variable is used instead.\n"
            "Confluence Cloud has no PATs — create an API token at\n"
            "id.atlassian.com/manage-profile/security/api-tokens and use Basic\n"
            "auth with your account email as the username."
        )
        form.addRow("PAT:", self.pat_edit)

        self.cookie_edit = QLineEdit()
        self.cookie_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.cookie_edit.setPlaceholderText("(blank = use CONFLUENCE_COOKIE env var)")
        self.cookie_edit.setToolTip(
            "Session cookie header value, for SSO-protected instances where the\n"
            "PAT alone gets redirected to a login page.\n\n"
            "Click 'Paste from browser…' — it shows the exact URL to open and\n"
            "walks through copying the request. Cookies expire; expect to\n"
            "refresh this periodically."
        )
        cookie_row = QHBoxLayout()
        cookie_row.setSpacing(6)
        cookie_row.addWidget(self.cookie_edit, stretch=1)
        self.paste_button = QPushButton("Paste from browser…")
        self.paste_button.setToolTip(
            "Import the session cookie from a request you copied in your\n"
            "browser's DevTools. The dialog shows the exact URL to open and\n"
            "walks through the four steps."
        )
        self.paste_button.clicked.connect(self._paste_cookie)
        cookie_row.addWidget(self.paste_button)
        form.addRow("Cookie:", cookie_row)
        # textEdited fires only on user input, so a hand-pasted cookie drops
        # the captured user agent — it belonged to a different session.
        self.cookie_edit.textEdited.connect(self._on_cookie_edited)

        bottom = QHBoxLayout()
        self.remember_check = QCheckBox("Remember credentials")
        self.remember_check.setToolTip(
            "Store the PAT and cookie in the settings file in PLAIN TEXT.\n"
            "Off by default — leave it off and use the CONFLUENCE_PAT /\n"
            "CONFLUENCE_COOKIE environment variables if that matters to you."
        )
        bottom.addWidget(self.remember_check)
        bottom.addStretch(1)
        self.test_button = QPushButton("Test connection")
        self.test_button.setToolTip(
            "Call /user/current and report who the server thinks you are.\n"
            "Use this before a big export — it catches wrong API paths, dead\n"
            "cookies and PATs the instance silently ignores."
        )
        self.test_button.clicked.connect(self._test_connection)
        bottom.addWidget(self.test_button)
        form.addRow(bottom)

        return group

    def _build_scope_group(self) -> QGroupBox:
        group = QGroupBox("Scope")
        form = QFormLayout(group)
        form.setSpacing(5)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.space_edit = QLineEdit()
        self.space_edit.setPlaceholderText("DOCS")
        self.space_edit.setToolTip(
            "Confluence space key — the short upper-case code in a page URL,\n"
            "e.g. the DOCS in /display/DOCS/Some+Page."
        )
        form.addRow("Space key:", self.space_edit)

        self.top_id_edit = QLineEdit()
        self.top_id_edit.setPlaceholderText("(blank = whole space)")
        self.top_id_edit.setToolTip(
            "Export this page and every descendant. The ID is the pageId in\n"
            "the page's URL. Takes precedence over the title below."
        )
        form.addRow("Top page ID:", self.top_id_edit)

        self.top_title_edit = QLineEdit()
        self.top_title_edit.setPlaceholderText("(exact title, case-sensitive)")
        self.top_title_edit.setToolTip(
            "Alternative to the ID: resolve the root page by its exact title\n"
            "within the space key above."
        )
        form.addRow("Top page title:", self.top_title_edit)

        hint = QLabel(
            "Leave both top-page fields blank to scan the entire space."
        )
        hint.setObjectName("HintLabel")
        hint.setWordWrap(True)
        form.addRow(hint)

        self.only_modified_check = QCheckBox("Only pages changed since last sync")
        self.only_modified_check.setToolTip(
            "Whole-space scans only: filter the listing by the timestamp\n"
            f"recorded in {STATE_FILENAME} in the output directory. Subtree\n"
            "exports always list the full tree — use 'Skip unchanged pages'\n"
            "below to avoid re-downloading their bodies."
        )
        form.addRow(self.only_modified_check)

        buttons = QHBoxLayout()
        self.discover_button = QPushButton("Discover pages")
        self.discover_button.setToolTip(
            "List the pages in scope without downloading anything, so you can\n"
            "prune the queue before exporting."
        )
        self.discover_button.clicked.connect(self._start_discovery)
        buttons.addWidget(self.discover_button)
        buttons.addStretch(1)
        form.addRow(buttons)

        return group

    def _build_output_row(self) -> QVBoxLayout:
        row = QVBoxLayout()
        row.setSpacing(6)
        row.addWidget(self._build_section_label("Output directory"))

        inner = QHBoxLayout()
        self.output_dir_edit = QLineEdit()
        self.output_dir_edit.setPlaceholderText("Select a folder…")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._pick_output)
        self.open_output_button = QPushButton("Open folder")
        self.open_output_button.clicked.connect(self._open_output)
        inner.addWidget(self.output_dir_edit, stretch=1)
        inner.addWidget(browse)
        inner.addWidget(self.open_output_button)
        row.addLayout(inner)
        return row

    def _build_page_controls(self) -> QHBoxLayout:
        row = QHBoxLayout()
        select_all = QPushButton("Select all")
        select_all.clicked.connect(self.page_list_select_all)
        remove_selected = QPushButton("Remove selected")
        remove_selected.setObjectName("DangerButton")
        remove_selected.clicked.connect(self._remove_selected)
        clear = QPushButton("Clear")
        clear.setObjectName("DangerButton")
        clear.clicked.connect(self.page_list_clear)
        row.addWidget(select_all)
        row.addSpacerItem(QSpacerItem(20, 10,
                                      QSizePolicy.Policy.Expanding,
                                      QSizePolicy.Policy.Minimum))
        row.addWidget(remove_selected)
        row.addWidget(clear)
        return row

    def _build_options_group(self) -> QGroupBox:
        group = QGroupBox("Export options")
        outer = QVBoxLayout(group)
        outer.setSpacing(6)

        first = QHBoxLayout()
        first.addWidget(QLabel("Format:"))
        self.format_combo = QComboBox()
        for label, value in EXPORT_FORMATS:
            self.format_combo.addItem(label, value)
        self.format_combo.setToolTip(
            "Markdown converts the page's storage format locally.\n"
            "PDF asks Confluence for its own PDF render — higher fidelity, but\n"
            "many instances have the export endpoint disabled.\n"
            "Both writes a .md and a .pdf per page."
        )
        first.addWidget(self.format_combo)
        first.addSpacing(20)

        self.overwrite_check = QCheckBox("Overwrite existing files")
        self.overwrite_check.setChecked(True)
        first.addWidget(self.overwrite_check)
        first.addSpacing(16)

        self.skip_unchanged_check = QCheckBox("Skip unchanged pages")
        self.skip_unchanged_check.setToolTip(
            f"Compare each page's last-modified timestamp against {STATE_FILENAME}\n"
            "in the output directory and skip the ones that haven't moved.\n"
            "This is what makes a repeat run cheap."
        )
        first.addWidget(self.skip_unchanged_check)
        first.addStretch(1)
        outer.addLayout(first)

        second = QGridLayout()
        second.setHorizontalSpacing(20)
        second.setVerticalSpacing(4)

        self.mirror_check = QCheckBox("Mirror page hierarchy as folders")
        self.mirror_check.setToolTip(
            "Recreate the parent/child structure as directories under the\n"
            "output folder, instead of writing every page side by side."
        )
        self.front_matter_check = QCheckBox("Write YAML front matter")
        self.front_matter_check.setChecked(True)
        self.front_matter_check.setToolTip(
            "Prepend title, page ID, space, source URL, version and\n"
            "last-modified timestamp to each .md file. Keeps the export\n"
            "traceable back to the page it came from."
        )
        self.resolve_links_check = QCheckBox("Rewrite wiki links to local files")
        self.resolve_links_check.setChecked(True)
        self.resolve_links_check.setToolTip(
            "Point links between exported pages at the sibling .md files, so\n"
            "the export is navigable offline. Links to pages outside the\n"
            "export keep their Confluence URL."
        )
        self.index_check = QCheckBox("Generate index.md")
        self.index_check.setChecked(True)
        self.index_check.setToolTip(
            "Write an index.md at the output root listing every exported page,\n"
            "indented by its depth in the tree."
        )

        second.addWidget(self.mirror_check, 0, 0)
        second.addWidget(self.front_matter_check, 0, 1)
        second.addWidget(self.resolve_links_check, 1, 0)
        second.addWidget(self.index_check, 1, 1)
        second.setColumnStretch(2, 1)
        outer.addLayout(second)

        third = QHBoxLayout()
        self.repeat_check = QCheckBox("Repeat every")
        self.repeat_check.setToolTip(
            "Re-run discovery and export on a timer, so the export tracks the\n"
            "space without being launched by hand. Combine with 'Skip unchanged\n"
            "pages' so each cycle only downloads what actually changed."
        )
        self.repeat_check.toggled.connect(self._on_repeat_toggled)
        self.repeat_spin = QSpinBox()
        self.repeat_spin.setRange(1, 1440)
        self.repeat_spin.setValue(60)
        self.repeat_spin.setSuffix(" min")
        self.repeat_spin.setMaximumWidth(110)
        third.addWidget(self.repeat_check)
        third.addWidget(self.repeat_spin)
        third.addStretch(1)
        outer.addLayout(third)

        outer.addWidget(self._build_wkhtml_row())
        return group

    def _build_wkhtml_row(self) -> QWidget:
        wrapper = QWidget()
        row = QHBoxLayout(wrapper)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        row.addWidget(QLabel("wkhtmltopdf:"))
        self.wkhtml_edit = QLineEdit()
        self.wkhtml_edit.setPlaceholderText(
            r"(blank = look on PATH; e.g. C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe)"
        )
        self.wkhtml_edit.setToolTip(
            "Only needed by 'Convert MD to PDF', which renders the exported\n"
            "Markdown locally. Unrelated to the server-side PDF export format."
        )
        row.addWidget(self.wkhtml_edit, stretch=1)

        browse = QPushButton("Browse…")
        browse.clicked.connect(self._pick_wkhtml)
        row.addWidget(browse)

        test = QPushButton("Test")
        test.clicked.connect(self._test_wkhtml)
        row.addWidget(test)

        self.md_to_pdf_button = QPushButton("Convert MD to PDF")
        self.md_to_pdf_button.setToolTip(
            "Render every .md in the output directory to a sibling .pdf using\n"
            "wkhtmltopdf. Use this when the server's own PDF export is disabled."
        )
        self.md_to_pdf_button.clicked.connect(self._start_md_to_pdf)
        row.addWidget(self.md_to_pdf_button)
        return wrapper

    def _build_action_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.count_label = QLabel("0 pages queued")
        self.count_label.setObjectName("HintLabel")
        self.export_button = QPushButton("Leech pages")
        self.export_button.setObjectName("PrimaryButton")
        self.export_button.clicked.connect(self._start_export)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel_running)
        row.addWidget(self.count_label)
        row.addStretch(1)
        row.addWidget(self.cancel_button)
        row.addWidget(self.export_button)
        return row

    # --- Settings persistence -----------------------------------------

    def _wire_persistence_signals(self) -> None:
        for line in (self.base_url_edit, self.username_edit, self.pat_edit,
                     self.cookie_edit, self.space_edit, self.top_id_edit,
                     self.top_title_edit, self.output_dir_edit,
                     self.wkhtml_edit):
            line.textChanged.connect(self._schedule_save)
        for cb in (self.remember_check, self.only_modified_check,
                   self.overwrite_check, self.skip_unchanged_check,
                   self.mirror_check, self.front_matter_check,
                   self.resolve_links_check, self.index_check,
                   self.repeat_check):
            cb.toggled.connect(self._schedule_save)
        self.api_path_combo.currentTextChanged.connect(self._schedule_save)
        self.auth_mode_combo.currentIndexChanged.connect(self._schedule_save)
        self.format_combo.currentIndexChanged.connect(self._schedule_save)
        self.repeat_spin.valueChanged.connect(self._schedule_save)

    def _schedule_save(self) -> None:
        if self._suppress_save:
            return
        self._save_timer.start()

    def _persist_settings(self) -> None:
        flavor_data = self.flavor_combo.currentData() if hasattr(self, "flavor_combo") else None
        accent_data = self.accent_combo.currentData() if hasattr(self, "accent_combo") else None
        data = {
            "base_url": self.base_url_edit.text(),
            "api_path": self.api_path_combo.currentText(),
            "auth_mode": self.auth_mode_combo.currentData(),
            "username": self.username_edit.text(),
            "space_key": self.space_edit.text(),
            "top_page_id": self.top_id_edit.text(),
            "top_page_title": self.top_title_edit.text(),
            "output_dir": self.output_dir_edit.text(),
            "wkhtmltopdf_path": self.wkhtml_edit.text(),
            "export_format": self.format_combo.currentData(),
            "overwrite": self.overwrite_check.isChecked(),
            "skip_unchanged": self.skip_unchanged_check.isChecked(),
            "only_modified": self.only_modified_check.isChecked(),
            "mirror_tree": self.mirror_check.isChecked(),
            "front_matter": self.front_matter_check.isChecked(),
            "resolve_links": self.resolve_links_check.isChecked(),
            "write_index": self.index_check.isChecked(),
            "repeat_enabled": self.repeat_check.isChecked(),
            "repeat_minutes": self.repeat_spin.value(),
            "remember_credentials": self.remember_check.isChecked(),
            "theme": {
                "flavor": flavor_data.value if isinstance(flavor_data, Flavor) else "mocha",
                "accent": accent_data if isinstance(accent_data, str) else "teal",
            },
        }
        # Secrets are opt-in. Rebuilding the dict from scratch every time also
        # means un-ticking the box erases what was already on disk.
        if self.remember_check.isChecked():
            data["pat"] = self.pat_edit.text()
            data["cookie"] = self.cookie_edit.text()
            # Stored with the cookie because it is only meaningful alongside
            # it — a UA without its session is noise.
            data["cookie_user_agent"] = self._captured_user_agent
        save_config(data)

    def _load_settings(self) -> None:
        cfg = load_config()
        if not cfg:
            return
        for key, widget in (
            ("base_url", self.base_url_edit),
            ("username", self.username_edit),
            ("space_key", self.space_edit),
            ("top_page_id", self.top_id_edit),
            ("top_page_title", self.top_title_edit),
            ("output_dir", self.output_dir_edit),
            ("wkhtmltopdf_path", self.wkhtml_edit),
            ("pat", self.pat_edit),
            ("cookie", self.cookie_edit),
        ):
            if cfg.get(key):
                widget.setText(str(cfg[key]))
        if cfg.get("cookie_user_agent"):
            self._captured_user_agent = str(cfg["cookie_user_agent"])
        if cfg.get("api_path"):
            self.api_path_combo.setCurrentText(str(cfg["api_path"]))
        if cfg.get("auth_mode"):
            idx = self.auth_mode_combo.findData(cfg["auth_mode"])
            if idx >= 0:
                self.auth_mode_combo.setCurrentIndex(idx)
        if cfg.get("export_format"):
            idx = self.format_combo.findData(cfg["export_format"])
            if idx >= 0:
                self.format_combo.setCurrentIndex(idx)
        for key, widget in (
            ("remember_credentials", self.remember_check),
            ("only_modified", self.only_modified_check),
            ("overwrite", self.overwrite_check),
            ("skip_unchanged", self.skip_unchanged_check),
            ("mirror_tree", self.mirror_check),
            ("front_matter", self.front_matter_check),
            ("resolve_links", self.resolve_links_check),
            ("write_index", self.index_check),
            ("repeat_enabled", self.repeat_check),
        ):
            if key in cfg:
                widget.setChecked(bool(cfg[key]))
        if cfg.get("repeat_minutes"):
            try:
                self.repeat_spin.setValue(int(cfg["repeat_minutes"]))
            except (TypeError, ValueError):
                pass

        # Theme: apply the saved flavor/accent and sync the combos. The theme
        # is also applied in app/main.py at startup, but redoing it here is
        # cheap and keeps the combos as the single source of truth.
        theme = cfg.get("theme") or {}
        if theme.get("flavor") or theme.get("accent"):
            flavor = Flavor.from_name(theme.get("flavor"))
            accent = theme.get("accent") or "teal"
            if accent not in ACCENTS:
                accent = "teal"
            f_idx = self.flavor_combo.findData(flavor)
            if f_idx >= 0:
                self.flavor_combo.setCurrentIndex(f_idx)
            a_idx = self.accent_combo.findData(accent)
            if a_idx >= 0:
                self.accent_combo.setCurrentIndex(a_idx)
            app = QApplication.instance()
            if app is not None:
                apply_catppuccin(app, flavor, accent)
                refresh_widgets(app.allWidgets())
                self._refresh_hero_logo()

    # --- Small slots ---------------------------------------------------

    def _on_auth_mode_changed(self) -> None:
        basic = self.auth_mode_combo.currentData() == "basic"
        self.username_edit.setEnabled(basic)
        self.username_label.setEnabled(basic)

    def _on_cookie_edited(self, _text: str) -> None:
        self._captured_user_agent = ""

    def _on_repeat_toggled(self, enabled: bool) -> None:
        self.repeat_spin.setEnabled(enabled)
        if not enabled:
            self._repeat_timer.stop()

    def _pick_output(self) -> None:
        start = self.output_dir_edit.text() or str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "Choose output directory", start)
        if path:
            self.output_dir_edit.setText(str(Path(path)))

    def _open_output(self) -> None:
        text = self.output_dir_edit.text().strip()
        if not text:
            QMessageBox.information(
                self, "No output directory", "Choose an output directory first."
            )
            return
        path = Path(text)
        if not path.is_dir():
            QMessageBox.information(
                self, "Not created yet",
                f"{path} doesn't exist yet — it's created on the first export.",
            )
            return
        _open_in_explorer(path)

    def _pick_wkhtml(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Locate wkhtmltopdf", self.wkhtml_edit.text() or "",
            "wkhtmltopdf (wkhtmltopdf.exe wkhtmltopdf);;All files (*.*)",
        )
        if path:
            self.wkhtml_edit.setText(str(Path(path)))

    def _test_wkhtml(self) -> None:
        ok, message = wkhtmltopdf_version(self.wkhtml_edit.text().strip())
        if ok:
            QMessageBox.information(self, "wkhtmltopdf found", message)
        else:
            QMessageBox.warning(self, "wkhtmltopdf not usable", message)

    def page_list_select_all(self) -> None:
        self.page_list.selectAll()

    def _remove_selected(self) -> None:
        for item in self.page_list.selectedItems():
            self.page_list.takeItem(self.page_list.row(item))
        self._update_count()

    def page_list_clear(self) -> None:
        self.page_list.clear()
        self._update_count()

    def _update_count(self) -> None:
        n = self.page_list.count()
        self.count_label.setText(f"{n} page{'s' if n != 1 else ''} queued")
        self.export_button.setEnabled(n > 0)

    # --- Credentials ---------------------------------------------------

    def _credentials(self) -> Credentials:
        """Build credentials from the form, falling back to the environment.

        The env-var fallback keeps credentials out of the settings file
        entirely: leave the fields blank, export ``CONFLUENCE_PAT`` (and/or
        ``CONFLUENCE_COOKIE``) in the shell, and nothing sensitive is stored.
        """
        return Credentials(
            base_url=self.base_url_edit.text().strip() or DEFAULT_BASE_URL,
            api_path=self.api_path_combo.currentText().strip() or DEFAULT_API_PATH,
            pat=self.pat_edit.text().strip() or os.environ.get("CONFLUENCE_PAT", ""),
            cookie=(
                self.cookie_edit.text().strip()
                or os.environ.get("CONFLUENCE_COOKIE", "")
            ),
            auth_mode=self.auth_mode_combo.currentData() or "bearer",
            username=self.username_edit.text().strip(),
            user_agent=self._captured_user_agent,
        )

    def _paste_cookie(self) -> None:
        """Import a cookie from a pasted cURL command / header, then verify."""
        dialog = CookiePasteDialog(
            self.base_url_edit.text().strip(),
            self.api_path_combo.currentText().strip(),
            self,
        )
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            creds = dialog.credentials
        finally:
            dialog.deleteLater()
        if not accepted or not creds.ok:
            self.log_view.appendPlainText("Cookie import cancelled.")
            return

        self.cookie_edit.setText(creds.cookie_header)
        # The browser's own User-Agent, pulled from the same paste. The server
        # answers `Vary: User-Agent`, and gateways that bind a session to its
        # originating UA reject a replay under a different one — a 401 that
        # looks exactly like an expired cookie.
        self._captured_user_agent = creds.user_agent
        names = cookie_names(creds.cookie_header)
        self.log_view.appendPlainText(
            f"Imported {len(names)} cookie(s) from the pasted request."
        )
        if creds.user_agent:
            self.log_view.appendPlainText(
                "Browser User-Agent imported too; it will be reused for every "
                "request made with this cookie."
            )
        if creds.base_url and not self.base_url_edit.text().strip():
            self.base_url_edit.setText(creds.base_url)
            self.log_view.appendPlainText(
                f"Base URL taken from the paste: {creds.base_url}"
            )
        self._schedule_save()
        self._test_connection()

    def _require_base_url(self) -> bool:
        """Warn and return False when the instance URL is missing.

        There is no default host, so every path that makes a request checks
        this first — otherwise the request goes to a schemeless URL and the
        user gets a confusing 'could not reach' message instead of being told
        what is actually missing.
        """
        if self.base_url_edit.text().strip():
            return True
        QMessageBox.warning(
            self, "Base URL required",
            "Enter the Confluence base URL first, e.g.\n"
            "  https://confluence.example.com\n\n"
            "No scheme-less or partial URL will work, and there is no default.",
        )
        return False

    def _test_connection(self) -> None:
        if not self._require_base_url():
            return
        creds = self._credentials()
        if not creds.has_auth:
            QMessageBox.warning(
                self, "No credentials",
                "Provide a Personal Access Token, a session cookie, or set "
                "CONFLUENCE_PAT / CONFLUENCE_COOKIE in your environment.",
            )
            return

        self.test_button.setEnabled(False)
        self.test_button.setText("Testing…")
        QApplication.processEvents()
        try:
            who = ConfluenceClient(creds, timeout=15).whoami()
        except ConfluenceError as exc:
            QMessageBox.critical(self, "Connection failed", str(exc))
            self.log_view.appendPlainText(f"! Connection test failed: {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(
                self, "Connection failed", f"{type(exc).__name__}: {exc}"
            )
            return
        finally:
            self.test_button.setEnabled(True)
            self.test_button.setText("Test connection")

        detail = f"{creds.api_root}\n\n{who.detail}"
        self.log_view.appendPlainText(f"Connection test: {who.detail}")
        if who.authenticated:
            QMessageBox.information(self, "Connected", detail)
        else:
            QMessageBox.warning(self, "Not authenticated", detail)

    # --- Discovery -----------------------------------------------------

    def _last_sync_time(self) -> datetime | None:
        """Read the last successful sync timestamp from the state file."""
        text = self.output_dir_edit.text().strip()
        if not text:
            return None
        state_file = Path(text) / STATE_FILENAME
        try:
            import json

            raw = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        stamp = raw.get("last_sync")
        if not stamp:
            return None
        try:
            return datetime.fromisoformat(str(stamp))
        except ValueError:
            return None

    def _start_discovery(self) -> None:
        if self._discovery_worker is not None or self._worker is not None:
            return
        if not self._require_base_url():
            self._auto_export_after_discovery = False
            return
        creds = self._credentials()
        if not creds.has_auth:
            QMessageBox.warning(
                self, "No credentials",
                "Provide a Personal Access Token or a session cookie first.",
            )
            return
        space = self.space_edit.text().strip()
        top_id = self.top_id_edit.text().strip()
        top_title = self.top_title_edit.text().strip()
        if not space and not top_id:
            QMessageBox.warning(
                self, "Scope required",
                "Enter a space key, or a top page ID to export a subtree.",
            )
            return

        modified_since = None
        if self.only_modified_check.isChecked() and not (top_id or top_title):
            modified_since = self._last_sync_time()
            if modified_since is None:
                self.log_view.appendPlainText(
                    "No previous sync recorded — scanning the whole space."
                )

        request = DiscoveryRequest(
            credentials=creds,
            space_key=space,
            top_page_id=top_id,
            top_page_title=top_title,
            modified_since=modified_since,
        )

        self.page_list.clear()
        self._update_count()
        self.log_view.appendPlainText("=" * 60)
        self.log_view.appendPlainText("Discovering pages…")
        self.discover_button.setEnabled(False)
        self.discover_button.setText("Discovering…")
        self.cancel_button.setEnabled(True)
        self.progress.setRange(0, 0)  # busy indicator
        self.statusBar().showMessage("Discovering pages…")

        worker = DiscoveryWorker(request)
        worker.log.connect(self.log_view.appendPlainText)
        worker.finished.connect(self._on_discovery_finished)
        self._discovery_worker = worker
        self._discovery_thread = run_in_thread(worker)

    def _on_discovery_finished(self, pages: list, error: str) -> None:
        self._discovery_worker = None
        self._discovery_thread = None
        self.discover_button.setEnabled(True)
        self.discover_button.setText("Discover pages")
        self.cancel_button.setEnabled(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)

        if error:
            self.statusBar().showMessage("Discovery failed.")
            self.log_view.appendPlainText(f"! {error}")
            self._auto_export_after_discovery = False
            QMessageBox.critical(self, "Discovery failed", error)
            return

        for page in pages:
            self._add_page(page)
        self._update_count()
        self.statusBar().showMessage(f"Discovered {len(pages)} page(s).")

        if not pages:
            self.log_view.appendPlainText(
                "Nothing to export. If you expected results, run 'Test "
                "connection' — an anonymous session sees an empty space."
            )
            self._auto_export_after_discovery = False
            return

        if self._auto_export_after_discovery:
            self._auto_export_after_discovery = False
            self.log_view.appendPlainText("Scheduled run: starting export…")
            self._start_export()

    def _add_page(self, page: PageRef) -> None:
        indent = "    " * page.depth
        label = f"{indent}{page.title}"
        item = QListWidgetItem(label)
        item.setData(PAGE_ROLE, page)
        item.setData(ROOT_ROLE, page.is_root)
        tooltip = [f"{page.title}", f"id={page.id}"]
        if page.last_updated:
            tooltip.append(f"updated={page.last_updated}")
        if page.ancestor_titles:
            tooltip.append("path=" + " / ".join(page.ancestor_titles))
        item.setToolTip("\n".join(tooltip))
        self.page_list.addItem(item)

    # --- Export --------------------------------------------------------

    def _queued_pages(self) -> list[PageRef]:
        return [
            self.page_list.item(i).data(PAGE_ROLE)
            for i in range(self.page_list.count())
        ]

    def _start_export(self) -> None:
        if self._worker is not None:
            return
        pages = self._queued_pages()
        if not pages:
            QMessageBox.information(
                self, "Nothing to export",
                "Click 'Discover pages' first, then export what's listed.",
            )
            return
        out_text = self.output_dir_edit.text().strip()
        if not out_text:
            QMessageBox.warning(
                self, "Output directory required",
                "Please choose an output directory.",
            )
            return

        options = ExportOptions(
            output_dir=Path(out_text),
            export_format=self.format_combo.currentData() or "md",
            overwrite=self.overwrite_check.isChecked(),
            mirror_tree=self.mirror_check.isChecked(),
            front_matter=self.front_matter_check.isChecked(),
            resolve_links=self.resolve_links_check.isChecked(),
            write_index=self.index_check.isChecked(),
            skip_unchanged=self.skip_unchanged_check.isChecked(),
        )

        self.progress.setRange(0, len(pages))
        self.progress.setValue(0)
        self.log_view.appendPlainText("=" * 60)
        self.export_button.setEnabled(False)
        self.discover_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.statusBar().showMessage("Exporting…")

        worker = ExportWorker(
            pages,
            credentials=self._credentials(),
            space_key=self.space_edit.text().strip(),
            options=options,
        )
        worker.progress.connect(self._on_progress)
        worker.log.connect(self.log_view.appendPlainText)
        worker.finished.connect(self._on_export_finished)
        self._worker = worker
        self._thread = run_in_thread(worker)

    def _on_progress(self, done: int, total: int, current: str) -> None:
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        if current:
            self.statusBar().showMessage(f"{current} ({done + 1}/{total})")

    def _on_export_finished(self, success: int, failure: int, skipped: int) -> None:
        self._worker = None
        self._thread = None
        self.export_button.setEnabled(True)
        self.discover_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        msg = (
            f"Done — {success} exported, {failure} failed, {skipped} unchanged."
        )
        self.statusBar().showMessage(msg)
        self.log_view.appendPlainText(msg)

        if self.repeat_check.isChecked():
            self._schedule_next_run()
            return

        if failure == 0 and success > 0:
            QMessageBox.information(self, "Export complete", msg)
        elif failure > 0:
            QMessageBox.warning(
                self, "Export finished with errors",
                msg + "\n\nSee the log for details.",
            )

    # --- Scheduled repeat ----------------------------------------------

    def _schedule_next_run(self) -> None:
        minutes = self.repeat_spin.value()
        self._repeat_timer.start(minutes * 60 * 1000)
        self.log_view.appendPlainText(
            f"Next scheduled run in {minutes} minute(s)."
        )
        self.statusBar().showMessage(
            f"Idle — next run in {minutes} minute(s)."
        )

    def _on_repeat_timeout(self) -> None:
        if self._worker is not None or self._discovery_worker is not None:
            # A manual run is in flight; try again after the same interval
            # rather than queueing two exports against one output folder.
            self._schedule_next_run()
            return
        self._auto_export_after_discovery = True
        self._start_discovery()

    # --- MD → PDF ------------------------------------------------------

    def _start_md_to_pdf(self) -> None:
        if self._pdf_worker is not None:
            return
        out_text = self.output_dir_edit.text().strip()
        if not out_text or not Path(out_text).is_dir():
            QMessageBox.warning(
                self, "Output directory required",
                "Choose an existing output directory containing .md files.",
            )
            return

        self.log_view.appendPlainText("=" * 60)
        self.md_to_pdf_button.setEnabled(False)
        self.progress.setRange(0, 0)
        self.cancel_button.setEnabled(True)
        self.statusBar().showMessage("Converting Markdown to PDF…")

        worker = MdToPdfWorker(
            Path(out_text),
            wkhtmltopdf_path=self.wkhtml_edit.text().strip(),
            overwrite=self.overwrite_check.isChecked(),
        )
        worker.progress.connect(self._on_progress)
        worker.log.connect(self.log_view.appendPlainText)
        worker.finished.connect(self._on_md_to_pdf_finished)
        self._pdf_worker = worker
        self._pdf_thread = run_in_thread(worker)

    def _on_md_to_pdf_finished(self, success: int, failure: int, skipped: int) -> None:
        self._pdf_worker = None
        self._pdf_thread = None
        self.md_to_pdf_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        msg = f"MD→PDF done — {success} converted, {failure} failed, {skipped} skipped."
        self.statusBar().showMessage(msg)
        self.log_view.appendPlainText(msg)
        if failure and not success:
            QMessageBox.warning(
                self, "Conversion failed",
                msg + "\n\nSee the log — wkhtmltopdf is the usual culprit.",
            )

    # --- Cancel / close ------------------------------------------------

    def _cancel_running(self) -> None:
        for worker in (self._worker, self._discovery_worker, self._pdf_worker):
            if worker is not None:
                worker.cancel()
        self._repeat_timer.stop()
        self.statusBar().showMessage("Cancelling…")

    def closeEvent(self, event) -> None:
        self._repeat_timer.stop()
        self._cancel_running()
        # Workers check their cancel flag between pages, so give them a moment
        # to notice and return. Exiting with a thread still running aborts the
        # process with "QThread: Destroyed while thread is still running".
        if not wait_for_threads(5000):
            self.log_view.appendPlainText(
                "! A background thread did not stop in time; exiting anyway."
            )
        # Flush any debounced save before the process exits.
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._persist_settings()
        super().closeEvent(event)
