"""Background discovery of the page set an export will cover.

Split out from the export itself so the GUI can show *what it is about to do*
before it does it — the page list is a review step, so a mistyped space key
costs one request instead of a few hundred.

Two scopes:

* **Subtree** — a root page (by ID or title) plus every descendant, found with
  a CQL ``ancestor=`` query.
* **Whole space** — every page in the space, optionally filtered to those
  modified since the last sync.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import QObject, Signal

from .confluence_client import (
    ConfluenceClient,
    ConfluenceError,
    Credentials,
    PageRef,
)


@dataclass
class DiscoveryRequest:
    """Everything needed to work out which pages are in scope."""

    credentials: Credentials
    space_key: str
    top_page_id: str = ""
    top_page_title: str = ""
    modified_since: datetime | None = None

    @property
    def is_subtree(self) -> bool:
        return bool(self.top_page_id.strip() or self.top_page_title.strip())


class DiscoveryWorker(QObject):
    """Resolves a :class:`DiscoveryRequest` into a list of pages."""

    log = Signal(str)
    finished = Signal(list, str)   # pages, error_message ("" on success)

    def __init__(self, request: DiscoveryRequest) -> None:
        super().__init__()
        self._request = request
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        req = self._request
        client = ConfluenceClient(req.credentials)
        try:
            who = client.whoami()
            self.log.emit(
                f"Auth: {who.detail}" if who.detail else "Auth: unknown state."
            )
            if not who.authenticated:
                # Not fatal — some instances allow anonymous reads of public
                # spaces, and failing here would be worse than trying.
                self.log.emit(
                    "! Continuing unauthenticated; expect empty results if the "
                    "space is restricted."
                )

            if req.is_subtree:
                pages = self._discover_subtree(client)
            else:
                pages = self._discover_space(client)
        except ConfluenceError as exc:
            self.finished.emit([], str(exc))
            return
        except Exception as exc:  # noqa: BLE001 — surface anything unexpected
            self.finished.emit([], f"{type(exc).__name__}: {exc}")
            return

        if self._cancelled:
            self.finished.emit([], "Discovery cancelled.")
            return

        self.log.emit(f"Discovered {len(pages)} page(s).")
        self.finished.emit(pages, "")

    # --- scopes ---------------------------------------------------------

    def _discover_subtree(self, client: ConfluenceClient) -> list[PageRef]:
        req = self._request
        root_id = client.resolve_page_id(
            req.space_key, req.top_page_id, req.top_page_title
        )
        if not root_id:
            raise ConfluenceError(
                "Could not resolve the top page. Supply a page ID, or a title "
                "that exists in the given space."
            )
        root = client.get_page(root_id, with_body=False)
        root_ref = PageRef(
            id=root_id,
            title=root.get("title") or f"page-{root_id}",
            last_updated=(
                root.get("history", {}).get("lastUpdated", {}).get("when") or ""
            ),
            is_root=True,
            depth=0,
        )
        self.log.emit(f"Root page: {root_ref.title} (id={root_id})")

        descendants = client.descendants(
            root_id,
            should_cancel=lambda: self._cancelled,
            on_batch=lambda n: self.log.emit(f"  … {n} descendant(s) in batch"),
        )
        return [root_ref, *descendants]

    def _discover_space(self, client: ConfluenceClient) -> list[PageRef]:
        req = self._request
        if not req.space_key.strip():
            raise ConfluenceError(
                "A space key is required when no top page is given."
            )
        if req.modified_since is not None:
            self.log.emit(
                "Space scan limited to pages modified since "
                f"{req.modified_since.isoformat(timespec='seconds')}."
            )
        return client.space_pages(
            req.space_key,
            modified_since=req.modified_since,
            should_cancel=lambda: self._cancelled,
            on_batch=lambda n: self.log.emit(f"  … {n} page(s) in batch"),
        )
