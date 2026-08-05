"""Background worker that pulls Confluence pages down to disk."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from PySide6.QtCore import QObject, QThread, Signal

from . import __version__
from .confluence_client import (
    ConfluenceClient,
    ConfluenceError,
    Credentials,
    PageRef,
)
from .storage_converter import convert_storage


# Written into the output directory so a re-run can skip pages whose version
# hasn't moved. Dot-prefixed to stay out of the way of the exported content.
STATE_FILENAME = ".l33ch-state.json"

INDEX_FILENAME = "index.md"


def sanitize_filename(name: str, max_length: int = 180) -> str:
    """Make ``name`` safe as a single Windows path component.

    Replaces characters Windows forbids and control chars with ``_``,
    collapses repeats, trims leading/trailing dots and spaces, and caps the
    length so directory + name stays under MAX_PATH. Deterministic, so a
    re-run over an existing output folder lands on the same filenames.
    """
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name)
    name = re.sub(r"_+", "_", name)
    name = name.strip(" .")
    if not name:
        name = "untitled"
    return name[:max_length]


@dataclass
class ExportOptions:
    output_dir: Path
    export_format: str = "md"        # "md" | "pdf" | "both"
    overwrite: bool = True
    mirror_tree: bool = False        # recreate the page hierarchy as folders
    front_matter: bool = True        # YAML header with id / url / timestamp
    resolve_links: bool = True       # rewrite intra-wiki links to local files
    write_index: bool = True         # emit index.md linking every page
    skip_unchanged: bool = False     # consult .l33ch-state.json and skip

    @property
    def wants_md(self) -> bool:
        return self.export_format in ("md", "both")

    @property
    def wants_pdf(self) -> bool:
        return self.export_format in ("pdf", "both")


@dataclass
class ExportStats:
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    unknown_macros: Counter = field(default_factory=Counter)
    attachments: int = 0


class ExportWorker(QObject):
    progress = Signal(int, int, str)     # done_count, total, current_title
    page_done = Signal(str, str)         # page_id, destination_path
    page_failed = Signal(str, str)       # page_id, error_message
    log = Signal(str)                    # log line
    finished = Signal(int, int, int)     # success, failure, skipped

    def __init__(
        self,
        pages: list[PageRef],
        credentials: Credentials,
        space_key: str,
        options: ExportOptions,
    ) -> None:
        super().__init__()
        self._pages = pages
        self._credentials = credentials
        self._space_key = space_key
        self._options = options
        self._cancelled = False
        self._stats = ExportStats()

    def cancel(self) -> None:
        self._cancelled = True

    # --- paths ----------------------------------------------------------

    def _relative_dir(self, page: PageRef) -> Path:
        if not self._options.mirror_tree:
            return Path()
        return Path(*[sanitize_filename(t) for t in page.ancestor_titles])

    def _destination(self, page: PageRef, suffix: str) -> Path:
        stem = f"{sanitize_filename(page.title)}_{page.id}"
        return self._options.output_dir / self._relative_dir(page) / f"{stem}{suffix}"

    def _page_url(self, page_id: str) -> str:
        base = self._credentials.base_url.rstrip("/")
        return f"{base}/pages/viewpage.action?pageId={page_id}"

    # --- link + attachment resolution -----------------------------------

    def _build_link_index(self) -> dict[str, Path]:
        """Map lower-cased page title → the ``.md`` file we're writing for it.

        Titles are unique per space in Confluence, so the title is a safe key
        and it's exactly what ``<ri:page ri:content-title="…">`` gives us.
        """
        index: dict[str, Path] = {}
        for page in self._pages:
            index[page.title.strip().lower()] = self._destination(page, ".md")
        return index

    def _link_resolver_for(self, page: PageRef, link_index: dict[str, Path]):
        """Return a resolver that points at a local file when we have one.

        Falls back to the live Confluence URL, so a link out of the exported
        subtree still goes somewhere useful instead of becoming plain text.
        """
        source_dir = (self._destination(page, ".md")).parent

        def resolve(title: str, space: str) -> str:
            if not title:
                return ""
            if self._options.resolve_links:
                target = link_index.get(title.strip().lower())
                if target is not None:
                    rel = os.path.relpath(target, source_dir)
                    return quote(rel.replace(os.sep, "/"))
            base = self._credentials.base_url.rstrip("/")
            space_key = space or self._space_key
            return f"{base}/display/{quote(space_key)}/{quote(title)}"

        return resolve

    def _attachment_resolver_for(self, page: PageRef):
        """Attachments aren't downloaded — link them on the server instead.

        ``/download/attachments/<pageId>/<file>`` is the canonical Server/DC
        path and resolves for anyone with a logged-in browser session, which
        is a far better outcome than a dead relative link to a file that was
        never fetched.
        """
        base = self._credentials.base_url.rstrip("/")

        def resolve(filename: str) -> str:
            if not filename:
                return ""
            return f"{base}/download/attachments/{page.id}/{quote(filename)}"

        return resolve

    # --- state ----------------------------------------------------------

    def _state_path(self) -> Path:
        return self._options.output_dir / STATE_FILENAME

    def _load_state(self) -> dict:
        try:
            return json.loads(self._state_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self, state: dict) -> None:
        try:
            self._options.output_dir.mkdir(parents=True, exist_ok=True)
            self._state_path().write_text(
                json.dumps(state, indent=2, sort_keys=True), encoding="utf-8"
            )
        except OSError as exc:
            self.log.emit(f"! Could not write {STATE_FILENAME}: {exc}")

    # --- run ------------------------------------------------------------

    def run(self) -> None:
        opts = self._options
        total = len(self._pages)
        client = ConfluenceClient(self._credentials)

        self.log.emit(
            f"Exporting {total} page(s) as {opts.export_format.upper()} to "
            f"{opts.output_dir}"
        )
        if opts.mirror_tree:
            self.log.emit("Layout: mirroring the Confluence page hierarchy.")

        state = self._load_state()
        page_state: dict[str, str] = dict(state.get("pages") or {})
        link_index = self._build_link_index()

        for index, page in enumerate(self._pages):
            if self._cancelled:
                self.log.emit("Export cancelled by user.")
                break

            self.progress.emit(index, total, page.title)

            if (
                opts.skip_unchanged
                and page.last_updated
                and page_state.get(page.id) == page.last_updated
            ):
                self._stats.skipped += 1
                self.log.emit(f"= Unchanged, skipped: {page.title}")
                continue

            self.log.emit(f"Fetching: {page.title} (id={page.id})")
            try:
                written: list[Path] = []
                if opts.wants_md:
                    written.append(self._export_markdown(client, page, link_index))
                if opts.wants_pdf:
                    written.append(self._export_pdf(client, page))

                self._stats.succeeded += 1
                for path in written:
                    self.log.emit(f"  -> {path}")
                self.page_done.emit(page.id, str(written[-1]) if written else "")
                if page.last_updated:
                    page_state[page.id] = page.last_updated
            except Exception as exc:  # noqa: BLE001 — every error reaches the user
                self._stats.failed += 1
                msg = (
                    str(exc)
                    if isinstance(exc, ConfluenceError)
                    else f"{type(exc).__name__}: {exc}"
                )
                self.page_failed.emit(page.id, msg)
                self.log.emit(f"  ! Failed: {msg}")

        if opts.write_index and opts.wants_md and not self._cancelled:
            try:
                path = self._write_index()
                self.log.emit(f"  -> {path}")
            except OSError as exc:
                self.log.emit(f"! Could not write {INDEX_FILENAME}: {exc}")

        state["pages"] = page_state
        state["last_sync"] = datetime.now().astimezone().isoformat(
            timespec="seconds"
        )
        state["space_key"] = self._space_key
        self._save_state(state)

        self._report_conversion_notes()
        self.progress.emit(total, total, "")
        self.finished.emit(
            self._stats.succeeded, self._stats.failed, self._stats.skipped
        )

    # --- per-page export -------------------------------------------------

    def _export_markdown(
        self,
        client: ConfluenceClient,
        page: PageRef,
        link_index: dict[str, Path],
    ) -> Path:
        destination = self._destination(page, ".md")
        if destination.exists() and not self._options.overwrite:
            raise FileExistsError(
                f"{destination.name} already exists (overwrite disabled)"
            )

        storage, raw = client.storage_body(page.id)
        if not storage.strip():
            raise ConfluenceError(
                "The page has no storage-format body. Either it is a blank page "
                "or the account cannot read its content."
            )

        result = convert_storage(
            storage,
            link_resolver=self._link_resolver_for(page, link_index),
            attachment_resolver=self._attachment_resolver_for(page),
        )
        self._stats.unknown_macros.update(result.unknown_macros)
        self._stats.attachments += len(result.attachments)

        body = result.markdown
        if self._options.front_matter:
            body = self._front_matter(page, raw) + "\n\n" + body

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(body.rstrip() + "\n", encoding="utf-8")
        return destination

    def _export_pdf(self, client: ConfluenceClient, page: PageRef) -> Path:
        destination = self._destination(page, ".pdf")
        if destination.exists() and not self._options.overwrite:
            raise FileExistsError(
                f"{destination.name} already exists (overwrite disabled)"
            )
        data = client.export_pdf(page.id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return destination

    def _front_matter(self, page: PageRef, raw: dict) -> str:
        version = raw.get("version", {}).get("number", "")
        lines = [
            "---",
            f'title: "{page.title.replace(chr(34), chr(39))}"',
            f"page_id: \"{page.id}\"",
            f"space: \"{self._space_key}\"",
            f"source: {self._page_url(page.id)}",
        ]
        if page.last_updated:
            lines.append(f"updated: {page.last_updated}")
        if version:
            lines.append(f"version: {version}")
        lines.append(f"exported_by: confluence-l33ch {__version__}")
        lines.append("---")
        return "\n".join(lines)

    # --- index ----------------------------------------------------------

    def _write_index(self) -> Path:
        """Write an ``index.md`` mirroring the page hierarchy.

        The exported tree has no other entry point, so this is its map — and
        it is what an LLM reads first to find the page it needs.
        """
        out = self._options.output_dir
        lines = [
            f"# {self._space_key or 'Confluence'} export",
            "",
            f"{len(self._pages)} page(s) exported by confluence-l33ch "
            f"{__version__} on "
            f"{datetime.now().astimezone().isoformat(timespec='minutes')}.",
            "",
        ]
        for page in self._pages:
            target = self._destination(page, ".md")
            rel = quote(os.path.relpath(target, out).replace(os.sep, "/"))
            indent = "  " * page.depth
            lines.append(f"{indent}- [{page.title}]({rel})")
        out.mkdir(parents=True, exist_ok=True)
        path = out / INDEX_FILENAME
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    # --- reporting ------------------------------------------------------

    def _report_conversion_notes(self) -> None:
        """Say what the conversion had to approximate. Silence would imply
        the Markdown is a lossless rendering of the source, which it isn't."""
        if self._stats.attachments:
            self.log.emit(
                f"Note: {self._stats.attachments} attachment reference(s) point "
                "at Confluence URLs — no files were downloaded."
            )
        if self._stats.unknown_macros:
            summary = ", ".join(
                f"{name} ({count})"
                for name, count in self._stats.unknown_macros.most_common()
            )
            self.log.emit(
                "Note: macros without a Markdown equivalent were passed through "
                f"as-is: {summary}"
            )


# Strong references to every running thread *and its worker*, released when
# the thread finishes. Both halves are load-bearing:
#
# * **The thread.** A caller that clears `self._thread = None` inside its
#   `finished` handler drops the last Python reference while the thread's
#   event loop is still unwinding. PySide6 then destroys the C++ QThread from
#   the garbage collector and the process aborts with
#   ``QThread: Destroyed while thread '' is still running``. The handler runs
#   first precisely because it is connected first, so this is the normal path,
#   not a rare race.
# * **The worker.** ``moveToThread`` does not confer ownership and the
#   ``started`` → ``worker.run`` connection does not keep it alive, so a
#   worker whose caller holds no reference is collected before it ever runs —
#   the task silently never happens.
#
# Holding both here means callers' own bookkeeping can be as loose as it likes.
_running: dict[QThread, QObject] = {}


def run_in_thread(worker: QObject) -> QThread:
    """Move a worker onto a fresh QThread and start it. Returns the thread.

    The worker must expose a ``run`` slot and a ``finished`` signal; the
    export, discovery and PDF workers all satisfy that.
    """
    thread = QThread()
    _running[thread] = worker
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    worker.finished.connect(worker.deleteLater)
    # Released only once the thread has actually stopped, at which point
    # dropping either object is safe.
    thread.finished.connect(lambda: _running.pop(thread, None))
    thread.finished.connect(thread.deleteLater)
    thread.start()
    return thread


def running_thread_count() -> int:
    """How many worker threads are currently tracked. For tests and status."""
    return len(_running)


def wait_for_threads(timeout_ms: int = 5000) -> bool:
    """Ask every running worker thread to finish, and wait for it.

    Called on window close: quitting the application while a thread is still
    running produces the same "Destroyed while thread is still running" abort,
    just at shutdown instead of mid-run. Returns True if all threads stopped
    within the timeout.
    """
    all_stopped = True
    for thread in list(_running):
        if not thread.isRunning():
            continue
        thread.quit()
        if not thread.wait(timeout_ms):
            all_stopped = False
    return all_stopped
