"""Confluence Server / Data Center REST client.

Everything that talks HTTP lives here: header building for the three auth
modes, JSON-vs-HTML response validation (an SSO redirect returns 200 + HTML,
which is the single most common failure on on-prem instances), paginated
listing, subtree discovery via CQL, and the PDF export endpoint fallback
chain.

Design notes
------------

* **No module-level state.** Connection details, space key and export format
  are passed in via :class:`Credentials` and method arguments, never read from
  the environment at import time — that would make the client un-callable from
  a GUI where the user can change any of them at runtime.
* **Discovery and body fetching are separate.** Listing returns lightweight
  :class:`PageRef` objects (id + title + timestamp); the body is fetched
  per-page at export time. That keeps the page list responsive on large
  spaces and lets the worker report real per-page progress.
* **Errors raise.** :class:`ConfluenceError` carries a message already
  written for a human — the GUI puts it straight in the log panel.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterator

import requests


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ConfluenceL33ch/1.0"
)

# Confluence caps `limit` at 100 for most content endpoints; 50 is safe on
# every version and keeps a single slow response from stalling discovery.
PAGE_SIZE = 50

# No default host: the instance URL is site-specific and there is no sensible
# guess. The GUI shows `https://confluence.example.com` as a placeholder and
# refuses to make a request until a real one is entered.
DEFAULT_BASE_URL = ""
DEFAULT_API_PATH = "/rest/api"


class ConfluenceError(RuntimeError):
    """A request failed in a way worth showing the user verbatim."""


@dataclass(frozen=True)
class Credentials:
    """Everything needed to talk to one Confluence instance.

    ``pat`` and ``cookie`` are both optional individually but at least one
    must be present: PAT-only works on instances that accept bearer tokens on
    the REST API, cookie-only works when the instance is behind SSO and the
    user pasted a browser session cookie, and supplying both covers the case
    where you cannot tell which the instance will honour.
    """

    base_url: str = DEFAULT_BASE_URL
    api_path: str = DEFAULT_API_PATH
    pat: str = ""
    cookie: str = ""
    auth_mode: str = "bearer"  # "bearer" | "basic"
    username: str = ""         # only used by "basic"
    # Overrides the default UA. Set when a cookie was captured by the embedded
    # browser: some SSO gateways bind a session to the user agent that created
    # it, and replaying the cookie under a different UA is rejected in a way
    # that looks identical to an expired cookie.
    user_agent: str = ""

    @property
    def api_root(self) -> str:
        return f"{self.base_url.rstrip('/')}/{self.api_path.strip('/')}"

    @property
    def has_auth(self) -> bool:
        return bool(self.pat or self.cookie)


@dataclass(frozen=True)
class PageRef:
    """A page discovered by a listing call, before its body is fetched."""

    id: str
    title: str
    last_updated: str = ""   # ISO 8601 as returned by Confluence, or ""
    is_root: bool = False    # the subtree root the user asked for
    depth: int = 0           # ancestor distance from the root (0 = root)
    # Titles of the ancestors *below* the export root, outermost first. Drives
    # the "mirror page hierarchy" output layout.
    ancestor_titles: tuple[str, ...] = ()

    @property
    def last_updated_dt(self) -> datetime | None:
        if not self.last_updated:
            return None
        try:
            return datetime.fromisoformat(self.last_updated.replace("Z", "+00:00"))
        except ValueError:
            return None


@dataclass
class WhoAmI:
    """Result of the ``/user/current`` probe."""

    authenticated: bool
    display_name: str = ""
    detail: str = ""


CancelCheck = Callable[[], bool]


class ConfluenceClient:
    """Thin, synchronous REST wrapper. Safe to use from a worker thread."""

    def __init__(self, creds: Credentials, timeout: int = 30) -> None:
        self.creds = creds
        self.timeout = timeout
        # One session per client so keep-alive is reused across the hundreds
        # of requests a subtree export makes.
        self._session = requests.Session()

    # --- plumbing ------------------------------------------------------

    def _headers(self, accept: str = "application/json") -> dict[str, str]:
        headers = {
            "Accept": accept,
            "User-Agent": self.creds.user_agent or USER_AGENT,
        }
        if self.creds.cookie:
            headers["Cookie"] = self.creds.cookie
        if self.creds.pat:
            if self.creds.auth_mode == "basic" and self.creds.username:
                raw = f"{self.creds.username}:{self.creds.pat}".encode("utf-8")
                headers["Authorization"] = (
                    "Basic " + base64.b64encode(raw).decode("ascii")
                )
            else:
                headers["Authorization"] = f"Bearer {self.creds.pat}"
        return headers

    def _get_json(self, url: str, params: dict[str, Any] | None = None) -> dict:
        """GET and parse JSON, converting every failure mode into a message.

        The HTML check matters more than it looks: an SSO-protected instance
        answers unauthenticated REST calls with 200 + a login page, so
        ``raise_for_status()`` passes and ``.json()`` then explodes with an
        opaque ``JSONDecodeError``.
        """
        try:
            resp = self._session.get(
                url, headers=self._headers(), params=params, timeout=self.timeout
            )
            resp.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            raise ConfluenceError(
                f"HTTP {status} from {url}. "
                + _http_hint(status)
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise ConfluenceError(
                f"Could not reach {url} ({type(exc).__name__}: {exc}). "
                "Check the base URL, your network, and any VPN requirement."
            ) from exc

        ctype = resp.headers.get("Content-Type", "").lower()
        if "application/json" not in ctype:
            snippet = (resp.text or "")[:200].replace("\n", " ")
            raise ConfluenceError(
                f"Expected JSON from {url} but got '{ctype or 'no content-type'}'. "
                "This is what an SSO login redirect looks like — paste a browser "
                "session cookie into the Cookie field, or check the API path. "
                f"Body starts: {snippet}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise ConfluenceError(
                f"Malformed JSON from {url}: {exc}"
            ) from exc

    # --- identity ------------------------------------------------------

    def whoami(self) -> WhoAmI:
        """Probe ``/user/current`` to confirm the credentials actually work."""
        if not self.creds.has_auth:
            return WhoAmI(
                False,
                detail=(
                    "No credentials supplied. Provide a Personal Access Token, "
                    "a session cookie, or both."
                ),
            )
        data = self._get_json(f"{self.creds.api_root}/user/current")
        name = data.get("displayName") or data.get("username") or ""
        if data.get("type") == "anonymous":
            return WhoAmI(
                False,
                display_name=name,
                detail=(
                    "The server accepted the request but treats you as Anonymous. "
                    "The PAT is being ignored — supply CONFLUENCE_COOKIE from a "
                    "logged-in browser session, or switch the auth mode to Basic."
                ),
            )
        return WhoAmI(True, display_name=name, detail=f"Authenticated as {name}.")

    # --- discovery -----------------------------------------------------

    def resolve_page_id(
        self,
        space_key: str,
        page_id: str = "",
        title: str = "",
    ) -> str:
        """Return a page ID from either an explicit ID or a title lookup."""
        if page_id.strip():
            return page_id.strip()
        if not title.strip():
            return ""
        data = self._get_json(
            f"{self.creds.api_root}/content",
            {"spaceKey": space_key, "title": title.strip(), "type": "page"},
        )
        results = data.get("results", [])
        if not results:
            raise ConfluenceError(
                f"No page titled '{title}' in space '{space_key}'. "
                "Titles are exact and case-sensitive; check the space key too."
            )
        return str(results[0].get("id", ""))

    def get_page(self, page_id: str, with_body: bool = True) -> dict:
        """Fetch one page, optionally including its storage-format body."""
        expand = "history,ancestors,version"
        if with_body:
            expand = "body.storage," + expand
        return self._get_json(
            f"{self.creds.api_root}/content/{page_id}", {"expand": expand}
        )

    def descendants(
        self,
        root_id: str,
        should_cancel: CancelCheck | None = None,
        on_batch: Callable[[int], None] | None = None,
    ) -> list[PageRef]:
        """Every page below ``root_id`` at any depth, via a CQL ancestor query.

        ``ancestors`` is expanded so each result knows its distance from the
        root — the page list uses it to indent and badge the tree.
        """
        pages: list[PageRef] = []
        for item in self._paginate(
            f"{self.creds.api_root}/content/search",
            {"cql": f"ancestor={root_id} and type=page", "expand": "history,ancestors"},
            should_cancel=should_cancel,
            on_batch=on_batch,
        ):
            pages.append(_page_ref(item, root_id=root_id))
        # Sort by depth then title so parents precede their children in the
        # list even though CQL returns them in relevance order.
        pages.sort(key=lambda p: (p.depth, p.title.lower()))
        return pages

    def space_pages(
        self,
        space_key: str,
        modified_since: datetime | None = None,
        should_cancel: CancelCheck | None = None,
        on_batch: Callable[[int], None] | None = None,
    ) -> list[PageRef]:
        """Every page in a space, optionally filtered to recent changes.

        Two endpoints exist and on-prem instances disagree about which one
        honours ``spaceKey``: ``/space/{key}/content`` is authoritative when
        the API root is ``/rest/api``, while the generic ``/content`` endpoint
        with a ``spaceKey`` parameter is what proxied roots (``/wiki/rest/api``,
        ``/confluence/rest/api``) expect. Picking the wrong one returns an
        empty result set rather than an error, so the branch is not optional.
        """
        if self.creds.api_path.strip("/") == "rest/api":
            url = f"{self.creds.api_root}/space/{space_key}/content"
            params: dict[str, Any] = {"type": "page", "expand": "history"}
        else:
            url = f"{self.creds.api_root}/content"
            params = {"spaceKey": space_key, "type": "page", "expand": "history"}

        pages: list[PageRef] = []
        for item in self._paginate(
            url, params, should_cancel=should_cancel, on_batch=on_batch
        ):
            ref = _page_ref(item)
            if modified_since is not None:
                when = ref.last_updated_dt
                # Keep pages with an unparseable timestamp: skipping a page
                # because we couldn't read its date is the worse failure.
                if when is not None and when < modified_since:
                    continue
            pages.append(ref)
        pages.sort(key=lambda p: p.title.lower())
        return pages

    def _paginate(
        self,
        url: str,
        params: dict[str, Any],
        should_cancel: CancelCheck | None = None,
        on_batch: Callable[[int], None] | None = None,
    ) -> Iterator[dict]:
        """Walk a Confluence collection endpoint until a short page arrives."""
        start = 0
        while True:
            if should_cancel is not None and should_cancel():
                return
            data = self._get_json(
                url, {**params, "start": start, "limit": PAGE_SIZE}
            )
            # /space/{key}/content nests results under the type key.
            results = data.get("results")
            if results is None:
                results = data.get("page", {}).get("results", [])
            yield from results
            if on_batch is not None:
                on_batch(len(results))
            if len(results) < PAGE_SIZE:
                return
            start += PAGE_SIZE

    # --- content -------------------------------------------------------

    def storage_body(self, page_id: str) -> tuple[str, dict]:
        """Return ``(storage_xhtml, page_json)`` for one page."""
        page = self.get_page(page_id, with_body=True)
        body = (
            page.get("body", {})
            .get("storage", {})
            .get("value", "")
        )
        return body, page

    def pdf_url_candidates(self, page_id: str) -> list[str]:
        """PDF export URLs to try in order, REST first then the UI action.

        Which one works depends on the Confluence version and whether the
        ``flyingpdf`` plugin is enabled, and there's no reliable way to ask
        up front — so all six are attempted.
        """
        base = self.creds.base_url.rstrip("/")
        return [
            f"{base}/rest/api/content/{page_id}/export/pdf",
            f"{base}/wiki/rest/api/content/{page_id}/export/pdf",
            f"{base}/confluence/rest/api/content/{page_id}/export/pdf",
            f"{base}/spaces/flyingpdf/pdfpageexport.action?pageId={page_id}",
            f"{base}/wiki/spaces/flyingpdf/pdfpageexport.action?pageId={page_id}",
            f"{base}/confluence/spaces/flyingpdf/pdfpageexport.action?pageId={page_id}",
        ]

    def export_pdf(self, page_id: str) -> bytes:
        """Download a page as PDF, trying every known export endpoint."""
        headers = self._headers(accept="application/pdf")
        attempts: list[str] = []
        for url in self.pdf_url_candidates(page_id):
            try:
                resp = self._session.get(url, headers=headers, timeout=120)
            except requests.exceptions.RequestException as exc:
                attempts.append(f"{url} -> {type(exc).__name__}")
                continue
            ctype = resp.headers.get("Content-Type", "").lower()
            if resp.status_code == 200 and (
                "application/pdf" in ctype or "application/octet-stream" in ctype
            ):
                return resp.content
            attempts.append(f"{url} -> HTTP {resp.status_code} ({ctype or 'no type'})")
        raise ConfluenceError(
            "No PDF export endpoint responded with a PDF. Tried:\n  "
            + "\n  ".join(attempts)
            + "\nIf every attempt is HTTP 403/404, this instance has PDF export "
            "disabled — export Markdown instead and use 'Convert MD to PDF'."
        )


def _page_ref(item: dict, root_id: str = "") -> PageRef:
    """Build a :class:`PageRef` from a raw content JSON object.

    ``ancestors`` comes back outermost-first. When a root is known, only the
    ancestors *below* it are kept, so a subtree export mirrors as a tree
    rooted at the output directory rather than recreating the whole space.
    """
    ancestors = item.get("ancestors") or []
    ids = [str(a.get("id")) for a in ancestors]
    titles = [a.get("title") or "" for a in ancestors]

    if root_id and root_id in ids:
        cut = ids.index(root_id) + 1
        relative = titles[cut:]
        depth = len(ids) - ids.index(root_id)
    else:
        relative = titles
        depth = len(ids)

    return PageRef(
        id=str(item.get("id", "")),
        title=item.get("title") or f"page-{item.get('id')}",
        last_updated=(
            item.get("history", {}).get("lastUpdated", {}).get("when") or ""
        ),
        is_root=False,
        depth=depth,
        ancestor_titles=tuple(t for t in relative if t),
    )


def _http_hint(status: int | str) -> str:
    hints = {
        401: "The credentials were rejected — check the PAT, or switch auth mode.",
        403: "Authenticated but not allowed to read this content, or the PAT "
             "lacks the required scope.",
        404: "The URL is wrong — most often the API path (try /rest/api, "
             "/wiki/rest/api or /confluence/rest/api) or a bad page ID.",
        429: "Rate-limited by the server. Wait and retry.",
    }
    if isinstance(status, int):
        if status in hints:
            return hints[status]
        if 500 <= status < 600:
            return "The Confluence server itself errored; retry later."
    return "Check the base URL and API path."
