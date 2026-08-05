"""Catppuccin palette, Qt theme application, and brand asset.

Colors are assigned by **semantic role**
(Base, Surface, Text, Accent, …), not by hex code, so swapping flavors is a
one-line change and the contrast hierarchy is preserved automatically.

Public surface
--------------

* :data:`PALETTES` — ``{Flavor: Palette}`` containing all four flavors.
* :class:`Palette` / :class:`Flavor` / :data:`ACCENTS` — type-safe handles.
* :func:`apply_catppuccin` — one call that applies Fusion style + QPalette
  + QSS to a ``QApplication``.
* :func:`current_palette` / :func:`current_accent_hex` — read the active
  flavor/accent from anywhere (e.g. custom-paint code).
* :func:`refresh_widgets` — re-polish every widget after a runtime switch.
* :func:`leech_svg` — the siphon mark used in the hero band.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


# ---------------------------------------------------------------------------
# Palettes — hex codes from the Catppuccin spec, never typed from memory.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Palette:
    """One Catppuccin flavor: 14 accent colors + 12 neutrals (text → bg)."""

    name: str
    rosewater: str
    flamingo: str
    pink: str
    mauve: str
    red: str
    maroon: str
    peach: str
    yellow: str
    green: str
    teal: str
    sky: str
    sapphire: str
    blue: str
    lavender: str
    text: str
    subtext1: str
    subtext0: str
    overlay2: str
    overlay1: str
    overlay0: str
    surface2: str
    surface1: str
    surface0: str
    base: str
    mantle: str
    crust: str
    is_dark: bool


class Flavor(Enum):
    LATTE = "latte"
    FRAPPE = "frappe"
    MACCHIATO = "macchiato"
    MOCHA = "mocha"

    @classmethod
    def from_name(cls, name: str | None) -> "Flavor":
        if not name:
            return cls.MOCHA
        try:
            return cls(name.lower())
        except ValueError:
            return cls.MOCHA


MOCHA = Palette(
    name="Mocha",
    rosewater="#f5e0dc", flamingo="#f2cdcd", pink="#f5c2e7", mauve="#cba6f7",
    red="#f38ba8", maroon="#eba0ac", peach="#fab387", yellow="#f9e2af",
    green="#a6e3a1", teal="#94e2d5", sky="#89dceb", sapphire="#74c7ec",
    blue="#89b4fa", lavender="#b4befe",
    text="#cdd6f4", subtext1="#bac2de", subtext0="#a6adc8",
    overlay2="#9399b2", overlay1="#7f849c", overlay0="#6c7086",
    surface2="#585b70", surface1="#45475a", surface0="#313244",
    base="#1e1e2e", mantle="#181825", crust="#11111b",
    is_dark=True,
)

MACCHIATO = Palette(
    name="Macchiato",
    rosewater="#f4dbd6", flamingo="#f0c6c6", pink="#f5bde6", mauve="#c6a0f6",
    red="#ed8796", maroon="#ee99a0", peach="#f5a97f", yellow="#eed49f",
    green="#a6da95", teal="#8bd5ca", sky="#91d7e3", sapphire="#7dc4e4",
    blue="#8aadf4", lavender="#b7bdf8",
    text="#cad3f5", subtext1="#b8c0e0", subtext0="#a5adcb",
    overlay2="#939ab7", overlay1="#8087a2", overlay0="#6e738d",
    surface2="#5b6078", surface1="#494d64", surface0="#363a4f",
    base="#24273a", mantle="#1e2030", crust="#181926",
    is_dark=True,
)

FRAPPE = Palette(
    name="Frappé",
    rosewater="#f2d5cf", flamingo="#eebebe", pink="#f4b8e4", mauve="#ca9ee6",
    red="#e78284", maroon="#ea999c", peach="#ef9f76", yellow="#e5c890",
    green="#a6d189", teal="#81c8be", sky="#99d1db", sapphire="#85c1dc",
    blue="#8caaee", lavender="#babbf1",
    text="#c6d0f5", subtext1="#b5bfe2", subtext0="#a5adce",
    overlay2="#949cbb", overlay1="#838ba7", overlay0="#737994",
    surface2="#626880", surface1="#51576d", surface0="#414559",
    base="#303446", mantle="#292c3c", crust="#232634",
    is_dark=True,
)

LATTE = Palette(
    name="Latte",
    rosewater="#dc8a78", flamingo="#dd7878", pink="#ea76cb", mauve="#8839ef",
    red="#d20f39", maroon="#e64553", peach="#fe640b", yellow="#df8e1d",
    green="#40a02b", teal="#179299", sky="#04a5e5", sapphire="#209fb5",
    blue="#1e66f5", lavender="#7287fd",
    text="#4c4f69", subtext1="#5c5f77", subtext0="#6c6f85",
    overlay2="#7c7f93", overlay1="#8c8fa1", overlay0="#9ca0b0",
    surface2="#acb0be", surface1="#bcc0cc", surface0="#ccd0da",
    base="#eff1f5", mantle="#e6e9ef", crust="#dce0e8",
    is_dark=False,
)

PALETTES: dict[Flavor, Palette] = {
    Flavor.LATTE: LATTE,
    Flavor.FRAPPE: FRAPPE,
    Flavor.MACCHIATO: MACCHIATO,
    Flavor.MOCHA: MOCHA,
}

# Accents the runtime picker exposes. Order matches the Catppuccin convention
# of "warm → cool → neutral" so the dropdown reads naturally.
ACCENTS: tuple[str, ...] = (
    "mauve", "blue", "lavender", "peach", "pink", "teal", "sky", "green",
    "rosewater",
)
DEFAULT_FLAVOR = Flavor.MOCHA
DEFAULT_ACCENT = "teal"


# ---------------------------------------------------------------------------
# Active-theme state. Modules that paint by hand (e.g. the page-list badges)
# read these instead of importing palette constants directly, so they update
# automatically when the user picks a new flavor.
# ---------------------------------------------------------------------------

_active_flavor: Flavor = DEFAULT_FLAVOR
_active_accent: str = DEFAULT_ACCENT


def current_palette() -> Palette:
    """Return the palette for the currently-active flavor."""
    return PALETTES[_active_flavor]


def current_flavor() -> Flavor:
    return _active_flavor


def current_accent_name() -> str:
    return _active_accent


def current_accent_hex() -> str:
    return getattr(current_palette(), _active_accent)


# ---------------------------------------------------------------------------
# Theme application
# ---------------------------------------------------------------------------

def apply_catppuccin(
    app: QApplication,
    flavor: Flavor = DEFAULT_FLAVOR,
    accent: str = DEFAULT_ACCENT,
) -> None:
    """Set ``Fusion`` style + QPalette + QSS on ``app`` for the chosen flavor.

    Re-callable: switching flavor at runtime is `apply_catppuccin(...)` again
    followed by :func:`refresh_widgets` to re-polish existing widgets.
    """
    global _active_flavor, _active_accent
    if accent not in ACCENTS:
        accent = DEFAULT_ACCENT
    _active_flavor = flavor
    _active_accent = accent

    # Fusion gives consistent QPalette honouring across native widgets. The
    # default Windows style ignores most palette colors.
    app.setStyle("Fusion")
    app.setPalette(_build_qpalette(PALETTES[flavor], accent))
    app.setStyleSheet(_build_stylesheet(PALETTES[flavor], accent))


def refresh_widgets(widgets: Iterable) -> None:
    """Force every widget to re-evaluate its style. Call after a flavor switch."""
    for w in widgets:
        style = w.style()
        if style is None:
            continue
        style.unpolish(w)
        style.polish(w)
        w.update()


def _accent_foreground(p: Palette) -> str:
    """Foreground colour to use on top of an accent fill — Base for dark
    flavors, Crust for light. Never Text-on-accent, which fails contrast."""
    return p.base if p.is_dark else p.crust


def _build_qpalette(p: Palette, accent_name: str) -> QPalette:
    accent_hex = getattr(p, accent_name)
    accent_fg = _accent_foreground(p)

    qp = QPalette()
    qp.setColor(QPalette.ColorRole.Window,          QColor(p.base))
    qp.setColor(QPalette.ColorRole.WindowText,      QColor(p.text))
    qp.setColor(QPalette.ColorRole.Base,            QColor(p.surface0))
    qp.setColor(QPalette.ColorRole.AlternateBase,   QColor(p.surface1))
    qp.setColor(QPalette.ColorRole.ToolTipBase,     QColor(p.mantle))
    qp.setColor(QPalette.ColorRole.ToolTipText,     QColor(p.text))
    qp.setColor(QPalette.ColorRole.Text,            QColor(p.text))
    qp.setColor(QPalette.ColorRole.Button,          QColor(p.surface0))
    qp.setColor(QPalette.ColorRole.ButtonText,      QColor(p.text))
    qp.setColor(QPalette.ColorRole.BrightText,      QColor(p.rosewater))
    qp.setColor(QPalette.ColorRole.Link,            QColor(p.blue))
    qp.setColor(QPalette.ColorRole.LinkVisited,     QColor(p.lavender))
    qp.setColor(QPalette.ColorRole.Highlight,       QColor(accent_hex))
    qp.setColor(QPalette.ColorRole.HighlightedText, QColor(accent_fg))
    qp.setColor(QPalette.ColorRole.PlaceholderText, QColor(p.overlay1))

    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                 QPalette.ColorRole.ButtonText):
        qp.setColor(QPalette.ColorGroup.Disabled, role, QColor(p.overlay0))
    return qp


def _build_stylesheet(p: Palette, accent_name: str) -> str:
    """Build the QSS that paints the parts QPalette doesn't reach.

    Includes the hero band gradient (Crust → Surface0), the primary CTA in
    the active accent, the page list (with selection in the accent), and the
    dark log panel (Crust background, Green text — the only place we deviate
    from Catppuccin role semantics, because a "terminal" panel is its own
    subgenre and Green-on-Crust reads as expected by users).
    """
    accent = getattr(p, accent_name)
    accent_fg = _accent_foreground(p)
    # Hover/pressed tints for the accent. Lavender is a universal "accent
    # hover" companion in most Catppuccin ports.
    accent_hover = p.lavender
    accent_pressed = p.mauve

    return f"""
    * {{
        font-family: "Segoe UI", "Inter", "Helvetica Neue", sans-serif;
        color: {p.text};
    }}

    QMainWindow, QDialog {{ background-color: {p.base}; }}

    /* --- Hero band (Crust → Surface0 gradient with an accent edge) --- */
    QFrame#HeroFrame {{
        background: qlineargradient(
            x1:0, y1:0, x2:1, y2:1,
            stop:0 {p.crust},
            stop:0.6 {p.mantle},
            stop:1 {p.surface0}
        );
        border: none;
        border-bottom: 2px solid {accent};
    }}
    QLabel#HeroTitle {{
        color: {p.text};
        font-size: 26px;
        font-weight: 700;
        background: transparent;
        letter-spacing: -0.5px;
    }}
    QLabel#HeroSubtitle {{
        color: {p.subtext0};
        font-size: 13px;
        font-weight: 400;
        background: transparent;
    }}

    /* --- Section headers --- */
    QLabel#SectionLabel {{
        color: {p.subtext1};
        font-size: 12px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 1px;
        background: transparent;
    }}
    QLabel#HintLabel {{
        color: {p.subtext0};
        font-size: 11px;
        background: transparent;
    }}

    /* --- Inputs --- */
    QLineEdit, QPlainTextEdit, QComboBox, QSpinBox {{
        background-color: {p.surface0};
        color: {p.text};
        border: 1px solid {p.surface2};
        border-radius: 6px;
        padding: 8px 10px;
        selection-background-color: {accent};
        selection-color: {accent_fg};
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus {{
        border: 1px solid {p.lavender};
    }}
    QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
        background-color: {p.mantle};
        color: {p.overlay0};
        border-color: {p.surface1};
    }}
    QComboBox::drop-down {{ border: none; width: 18px; }}
    QComboBox QAbstractItemView {{
        background-color: {p.mantle};
        color: {p.text};
        selection-background-color: {p.surface1};
        border: 1px solid {p.overlay0};
        outline: 0;
    }}
    QSpinBox::up-button, QSpinBox::down-button {{ width: 14px; border: none; }}

    /* --- Buttons --- */
    QPushButton {{
        background-color: {p.surface0};
        color: {p.text};
        border: 1px solid {p.surface2};
        border-radius: 6px;
        padding: 8px 14px;
        font-weight: 500;
    }}
    QPushButton:hover   {{ background-color: {p.surface1}; }}
    QPushButton:pressed {{ background-color: {p.surface2}; }}
    QPushButton:focus   {{ border: 1px solid {p.lavender}; }}
    QPushButton:disabled {{
        background-color: {p.surface0};
        color: {p.overlay0};
        border-color: {p.surface1};
    }}

    QPushButton#PrimaryButton {{
        background-color: {accent};
        color: {accent_fg};
        border: none;
        padding: 12px 24px;
        font-size: 14px;
        font-weight: 700;
        border-radius: 6px;
    }}
    QPushButton#PrimaryButton:hover   {{ background-color: {accent_hover}; }}
    QPushButton#PrimaryButton:pressed {{ background-color: {accent_pressed}; }}
    QPushButton#PrimaryButton:disabled {{
        background-color: {p.surface1};
        color: {p.overlay0};
    }}

    QPushButton#DangerButton {{
        background-color: {p.surface0};
        color: {p.red};
        border: 1px solid {p.surface2};
    }}
    QPushButton#DangerButton:hover {{
        background-color: {p.surface1};
        border-color: {p.red};
    }}

    /* --- Page list --- */
    QListWidget {{
        background-color: {p.surface0};
        border: 1px solid {p.surface1};
        border-radius: 8px;
        padding: 6px;
        outline: 0;
        alternate-background-color: {p.surface1};
    }}
    QListWidget::item {{
        padding: 6px 8px;
        border-radius: 4px;
        color: {p.text};
    }}
    QListWidget::item:hover {{
        background-color: {p.surface1};
    }}
    QListWidget::item:selected {{
        background-color: {accent};
        color: {accent_fg};
    }}

    /* --- Group boxes (auth / scope / options panels) --- */
    QGroupBox {{
        border: 1px solid {p.surface1};
        border-radius: 8px;
        margin-top: 10px;
        padding: 12px 12px 10px 12px;
        background-color: {p.mantle};
        font-weight: 600;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        left: 12px;
        padding: 0 6px;
        color: {p.subtext1};
        font-size: 12px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 1px;
    }}

    /* --- Log view: keep the terminal "dark panel" feel even on Latte ---
       Catppuccin convention: terminal/code panels use Crust + Green. */
    QPlainTextEdit#LogView {{
        background-color: {MOCHA.crust};
        color: {MOCHA.green};
        border: 1px solid {p.surface2};
        border-radius: 8px;
        padding: 8px;
        font-family: "Consolas", "Cascadia Mono", "Courier New", monospace;
        font-size: 11px;
        selection-background-color: {MOCHA.surface2};
        selection-color: {MOCHA.text};
    }}

    /* --- Progress bar --- */
    QProgressBar {{
        background-color: {p.surface0};
        border: 1px solid {p.surface1};
        border-radius: 6px;
        height: 14px;
        text-align: center;
        color: {p.text};
        font-size: 10px;
        font-weight: 500;
    }}
    QProgressBar::chunk {{
        background: qlineargradient(
            x1:0, y1:0, x2:1, y2:0,
            stop:0 {accent},
            stop:1 {p.lavender}
        );
        border-radius: 5px;
    }}

    /* --- Checkbox / radio --- */
    QCheckBox, QRadioButton {{
        spacing: 8px; color: {p.text}; background: transparent;
    }}
    QCheckBox::indicator, QRadioButton::indicator {{
        width: 16px; height: 16px;
        border: 1px solid {p.overlay2};
        border-radius: 3px;
        background-color: {p.surface0};
    }}
    QRadioButton::indicator {{ border-radius: 8px; }}
    QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
        border-color: {p.lavender};
    }}
    QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
        background-color: {accent};
        border-color: {accent};
        image: none;
    }}
    QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
        border-color: {p.surface2};
        background-color: {p.mantle};
    }}

    /* --- Status bar --- */
    QStatusBar {{
        background-color: {p.mantle};
        color: {p.subtext1};
        border-top: 1px solid {p.surface1};
        font-size: 11px;
    }}
    QStatusBar QLabel {{ color: {p.subtext1}; background: transparent; }}

    /* --- Tooltips --- */
    QToolTip {{
        background-color: {p.mantle};
        color: {p.text};
        border: 1px solid {p.overlay0};
        padding: 4px 8px;
    }}

    /* --- Menu (right-click on the page list) --- */
    QMenu {{
        background-color: {p.mantle};
        color: {p.text};
        border: 1px solid {p.overlay0};
    }}
    QMenu::item:selected {{ background-color: {p.surface1}; }}

    /* --- Scroll bars --- */
    QScrollBar:vertical {{
        background: transparent; width: 10px; margin: 0; border: none;
    }}
    QScrollBar::handle:vertical {{
        background: {p.surface2}; min-height: 24px; border-radius: 5px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {accent}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical,  QScrollBar::sub-page:vertical  {{ background: transparent; }}
    """


# ---------------------------------------------------------------------------
# Hero icon — a minimalist "siphon" mark: a wiki page stack drained downward
# into a local tray.
# ---------------------------------------------------------------------------

def leech_svg(color: str | None = None) -> str:
    """Return the siphon mark as an SVG string, filled with ``color``.

    ``color`` defaults to the current accent, so the mark re-tints on every
    flavor/accent switch (see ``MainWindow._refresh_hero_logo``).
    """
    fill = color or current_accent_hex()
    return f"""<?xml version='1.0' encoding='UTF-8'?>
<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>
  <g fill='{fill}'>
    <!-- Source: a stack of remote pages, widest at the top -->
    <rect x='8'  y='6'  width='48' height='7' rx='3.5'/>
    <rect x='15' y='17' width='34' height='6' rx='3'/>
    <!-- Siphon: shaft + arrow head pulling content down -->
    <rect x='29' y='24' width='6'  height='15' rx='3'/>
    <path d='M19 36 L32 52 L45 36 Z'/>
    <!-- Local tray the pages land in -->
    <rect x='6'  y='55' width='52' height='6' rx='3'/>
  </g>
</svg>"""
