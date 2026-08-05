"""Import a session cookie by pasting a request copied from your browser.

This is the only cookie-acquisition route. An earlier version embedded a
Chromium window and harvested the cookie itself; it was removed because on a
machine whose Direct3D device gets removed mid-resize, Chromium takes the
whole process down with an access violation that no Python can catch. A text
box and a parser cannot fail that way.

The input is whatever is easiest to get out of DevTools:

* **Copy as cURL** — right-click the request → Copy → Copy as cURL. Paste the
  whole blob; the ``Cookie:`` header and the request URL are both extracted.
  This is the least error-prone route because there is nothing to select by
  hand and nothing to truncate.
* A bare ``Cookie: JSESSIONID=…; crowd.token_key=…`` header line.
* Just the header *value*, ``JSESSIONID=…; crowd.token_key=…``.

All three shapes go through :func:`parse_cookie_input`, which is pure and
unit-tested — the parsing is the part that goes wrong, so it is the part that
is covered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)


# `-H 'Cookie: …'` / `--header "Cookie: …"`, and curl's own cookie flags
# `-b '…'` / `--cookie '…'`. Quotes may be single (bash / Copy as cURL) or
# double (cmd.exe / "Copy as cURL (cmd)").
_HEADER_RE = re.compile(
    r"""(?:-H|--header)\s+(['"])(?P<header>.*?)\1""",
    re.IGNORECASE | re.DOTALL,
)
_COOKIE_FLAG_RE = re.compile(
    r"""(?:-b|--cookie)\s+(['"])(?P<value>.*?)\1""",
    re.IGNORECASE | re.DOTALL,
)
_URL_RE = re.compile(r"""['"]?(?P<url>https?://[^\s'"]+)""", re.IGNORECASE)

_COOKIE_PREFIX_RE = re.compile(r"^\s*cookie\s*:\s*", re.IGNORECASE)

# Line continuations that shells insert into a copied command: `\` (bash),
# `^` (cmd.exe) and backtick (PowerShell), each at end of line.
_CONTINUATION_RE = re.compile(r"[\\^`]\s*\n")


@dataclass(frozen=True)
class PastedCredentials:
    """What could be recovered from the pasted text."""

    cookie_header: str = ""
    base_url: str = ""
    user_agent: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.cookie_header)


def _looks_like_cookies(value: str) -> bool:
    """A cookie header is ``name=value`` pairs; anything else is a mispaste."""
    value = value.strip().strip(";")
    if "=" not in value:
        return False
    return all("=" in part for part in value.split(";") if part.strip())


def _base_url_from(url: str) -> str:
    """Reduce a request URL to the scheme+host the app wants as Base URL.

    A context path is preserved (``/confluence``) because instances behind one
    need it, but the REST suffix is dropped — ``/rest/api/...`` is the API path
    field's business, not the base URL's.
    """
    match = re.match(r"^(https?://[^/]+)(/.*)?$", url.strip(), re.IGNORECASE)
    if not match:
        return ""
    origin, path = match.group(1), match.group(2) or ""
    # Strip everything from the first well-known Confluence path segment on.
    path = re.split(
        r"/(?:rest|wiki/rest|display|pages|spaces|plugins|login\.action|s)\b",
        path,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return (origin + path).rstrip("/")


def parse_cookie_input(text: str) -> PastedCredentials:
    """Extract a ``Cookie`` header value (and base URL) from pasted text.

    Never raises: unrecognised input returns an empty result, which the dialog
    turns into a message rather than a stack trace.
    """
    if not text or not text.strip():
        return PastedCredentials()

    joined = _CONTINUATION_RE.sub(" ", text)

    cookie = ""
    user_agent = ""
    # 1. Headers inside a cURL command. The User-Agent is worth keeping: the
    #    server sends `Vary: User-Agent`, and a gateway that ties a session to
    #    the UA that created it rejects the cookie under any other one.
    for match in _HEADER_RE.finditer(joined):
        header = match.group("header")
        lowered = header.strip().lower()
        if lowered.startswith("cookie:") and not cookie:
            cookie = _COOKIE_PREFIX_RE.sub("", header).strip()
        elif lowered.startswith("user-agent:") and not user_agent:
            user_agent = header.split(":", 1)[1].strip()

    # 2. curl's dedicated cookie flag.
    if not cookie:
        flag = _COOKIE_FLAG_RE.search(joined)
        if flag and _looks_like_cookies(flag.group("value")):
            cookie = flag.group("value").strip()

    # 3. A bare header line, or just the value. Checked per line so a pasted
    #    block of headers still yields the right one.
    if not cookie:
        for line in joined.splitlines():
            candidate = _COOKIE_PREFIX_RE.sub("", line).strip().strip("'\"")
            if candidate and _looks_like_cookies(candidate):
                cookie = candidate
                break

    # A raw header block (the "Raw" checkbox in DevTools) carries the UA as a
    # plain line rather than a -H argument.
    if not user_agent:
        for line in joined.splitlines():
            if line.strip().lower().startswith("user-agent:"):
                user_agent = line.split(":", 1)[1].strip().strip("'\"")
                break

    base_url = ""
    url_match = _URL_RE.search(joined)
    if url_match:
        base_url = _base_url_from(url_match.group("url"))

    if cookie and not _looks_like_cookies(cookie):
        cookie = ""
    return PastedCredentials(
        cookie_header=cookie.rstrip(";").strip(),
        base_url=base_url,
        user_agent=user_agent,
    )


def cookie_names(cookie_header: str) -> list[str]:
    """The cookie names in a header value, for reporting what was imported."""
    names = []
    for part in cookie_header.split(";"):
        name, _, _value = part.partition("=")
        name = name.strip()
        if name:
            names.append(name)
    return names


PLACEHOLDER = """Paste one of:

  • the whole "Copy as cURL" command from DevTools (easiest — nothing to
    select by hand)
  • the Request Headers block with the "Raw" checkbox ticked
  • a single Cookie: header line
  • just the cookie values, e.g.  JSESSIONID=...; crowd.token_key=..."""

# The probe URL. /user/current is the right request to copy from: it is the
# same endpoint the app's own "Test connection" calls, it needs no space
# permissions, and a 200 with JSON proves the cookie is genuinely
# authenticated before anything else is attempted.
PROBE_SUFFIX = "/user/current"


def probe_url(base_url: str, api_path: str) -> str:
    """The URL to open in the browser, built from the connection fields."""
    base = (base_url or "").strip().rstrip("/")
    path = (api_path or "/rest/api").strip().strip("/")
    if not base:
        return f"https://<your-confluence-host>/{path}{PROBE_SUFFIX}"
    return f"{base}/{path}{PROBE_SUFFIX}"


class CookiePasteDialog(QDialog):
    """Collect a cookie header from pasted text.

    On accept, :attr:`credentials` holds the parsed result. Deliberately
    dependency-free — no embedded browser, no native rendering, nothing that
    can fault the process.
    """

    def __init__(
        self,
        base_url: str = "",
        api_path: str = "/rest/api",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import cookie from your browser")
        self.resize(760, 520)
        self.credentials = PastedCredentials()
        self._probe = probe_url(base_url, api_path)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        steps = QLabel(
            "<b>1.</b> Open this URL in the browser where you are signed in to "
            "Confluence:"
        )
        steps.setWordWrap(True)
        layout.addWidget(steps)

        url_row = QHBoxLayout()
        url_row.setSpacing(6)
        self._url_edit = QLineEdit(self._probe)
        self._url_edit.setReadOnly(True)
        self._url_edit.setToolTip(
            "The same endpoint 'Test connection' uses. It needs no space\n"
            "permissions, so a 200 here means the cookie is good.\n"
            "If it shows a login page instead of JSON, sign in first and reload."
        )
        url_row.addWidget(self._url_edit, stretch=1)
        copy_url = QPushButton("Copy URL")
        copy_url.clicked.connect(self._copy_url)
        url_row.addWidget(copy_url)
        layout.addLayout(url_row)

        how = QLabel(
            "<b>2.</b> It should show JSON describing your account — if you get "
            "a login page, sign in and reload.<br>"
            "<b>3.</b> Press <b>F12</b> → <b>Network</b> tab → reload the page → "
            "click the <code>current</code> request in the list on the left.<br>"
            "<b>4.</b> Either right-click that request → <b>Copy</b> → "
            "<b>Copy as cURL</b>, or tick the <b>Raw</b> checkbox next to "
            "<i>Request Headers</i> and copy the text. Paste it below."
        )
        how.setWordWrap(True)
        layout.addWidget(how)

        self._edit = QPlainTextEdit()
        self._edit.setPlaceholderText(PLACEHOLDER)
        self._edit.textChanged.connect(self._on_text_changed)
        layout.addWidget(self._edit, stretch=1)

        self._status = QLabel("Waiting for a paste…")
        self._status.setObjectName("HintLabel")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText("Use this cookie")
        ok.setObjectName("PrimaryButton")
        ok.setEnabled(False)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

    def _copy_url(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._probe)

    def _on_text_changed(self) -> None:
        self.credentials = parse_cookie_input(self._edit.toPlainText())
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(self.credentials.ok)
        if not self.credentials.ok:
            self._status.setText(
                "No cookie found yet. The paste must contain name=value pairs "
                "— check you copied the request that was already authenticated."
            )
            return
        names = cookie_names(self.credentials.cookie_header)
        session = [n for n in names if n.lower() in SESSION_NAMES]
        parts = [f"Found {len(names)} cookie(s): {', '.join(names[:6])}"]
        if len(names) > 6:
            parts[0] += f", +{len(names) - 6} more"
        if session:
            parts.append(f"Session cookie present ({', '.join(session)}).")
        else:
            parts.append(
                "⚠ No JSESSIONID / crowd.token_key — this looks like an "
                "anonymous session and will authenticate as nobody."
            )
        if self.credentials.base_url:
            parts.append(f"Base URL detected: {self.credentials.base_url}")
        if self.credentials.user_agent:
            parts.append("User-Agent imported.")
        self._status.setText("  ".join(parts))


# Cookie names that prove an authenticated session. Everything else Confluence
# sets (XSRF tokens, analytics IDs, the confluence.* UI-state cookies, the
# NSC_* load-balancer cookies) is present on an anonymous session too, so a
# paste containing only those would authenticate as nobody.
SESSION_NAMES = frozenset({"jsessionid", "crowd.token_key", "seraph.confluence"})
