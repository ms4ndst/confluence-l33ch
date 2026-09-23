"""Background worker that pulls Confluence pages down to disk."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable
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

# A page named "README" with page IDs turned off in the filename would
# collide with this — an accepted, pre-existing risk (the old "index.md"
# name had the same exposure against a page literally titled "Index").
INDEX_FILENAME = "README.md"

# Mirrors everything that reaches the GUI's log panel during a run. The GUI
# (not this module) opens/writes it — see MainWindow._open_run_log_file —
# but the name lives here so both sides agree on it, same as STATE_FILENAME.
LOG_FILENAME = "l33ch-log.txt"

# Where downloaded images/attachments land when `download_images` is on, one
# shared folder under the output root rather than a folder per page.
IMAGES_DIRNAME = "images"

# Where a linked (not embedded) file attachment lands when `resolve_links` is
# on — kept separate from IMAGES_DIRNAME so a PDF or .docx linked off a page
# doesn't end up sitting in a folder named "images".
FILES_DIRNAME = "files"


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
    front_matter: bool = False       # YAML header with id / url / timestamp
    resolve_links: bool = True       # rewrite intra-wiki links to local files
    link_out_of_scope: bool = True   # link pages outside the export to their live URL
    include_page_id: bool = True     # append "_<page id>" to each filename
    write_blank_pages: bool = False  # write a placeholder .md for empty pages
    write_index: bool = True         # emit README.md linking every page
    skip_unchanged: bool = False     # consult .l33ch-state.json and skip
    download_images: bool = False    # fetch attachments into a shared images/ folder
    download_linked_files: bool = False  # fetch linked (non-image) files into files/

    @property
    def wants_md(self) -> bool:
        return self.export_format in ("md", "both")

    @property
    def wants_pdf(self) -> bool:
        return self.export_format in ("pdf", "both")


class BlankPageError(ConfluenceError):
    """The page exists and was readable, but its body is empty.

    Not a failure: Confluence hands back a normal response with an empty
    storage body (a page it couldn't let us read would be a 403/404
    instead), so there is simply nothing to write. Carries the page JSON so
    a placeholder file can still get front matter.
    """

    def __init__(self, raw: dict):
        super().__init__("The page is blank — it has no content.")
        self.raw = raw


@dataclass
class ExportStats:
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    organizational: int = 0  # blank pages kept only as a folder level for subpages
    blank: int = 0           # blank pages with no subpages — nothing to write
    placeholders: int = 0    # placeholder files written for blank pages
    unknown_macros: Counter = field(default_factory=Counter)
    attachments: int = 0
    images_downloaded: int = 0
    images_failed: int = 0
    files_downloaded: int = 0
    files_failed: int = 0


class ExportWorker(QObject):
    progress = Signal(int, int, str)     # done_count, total, current_title
    page_done = Signal(str, str)         # page_id, destination_path
    page_failed = Signal(str, str)       # page_id, error_message
    log = Signal(str)                    # log line
    # success, failure, skipped, organizational, blank
    finished = Signal(int, int, int, int, int)

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
        # (page_id, filename) -> local path, so an image/file referenced
        # twice on the same page (or re-encountered via the in-memory cache)
        # is only downloaded once per run.
        self._downloaded_images: dict[tuple[str, str], Path] = {}
        self._downloaded_files: dict[tuple[str, str], Path] = {}
        # Every ancestor-title prefix some page in this run sits under, i.e.
        # every folder the mirrored layout creates. A page whose own
        # (ancestors + title) is in here has subpages, so in the mirrored
        # layout it's written *inside* its folder as ``<folder>.md``.
        self._folder_chains: set[tuple[str, ...]] = {
            page.ancestor_titles[:n]
            for page in pages
            for n in range(1, len(page.ancestor_titles) + 1)
        }
        # Only meaningful when `include_page_id` is off: page id -> filename
        # stem. Computed once up front so a same-titled sibling gets a
        # stable "(2)", "(3)", … suffix instead of two pages silently
        # overwriting each other.
        self._stems = self._compute_stems()

    def cancel(self) -> None:
        self._cancelled = True

    # --- paths ----------------------------------------------------------

    def _relative_dir(self, page: PageRef) -> Path:
        if not self._options.mirror_tree:
            return Path()
        return Path(*[sanitize_filename(t) for t in page.ancestor_titles])

    def _is_folder_page(self, page: PageRef) -> bool:
        """Whether the mirrored layout turns this page into a folder."""
        return (
            self._options.mirror_tree
            and (*page.ancestor_titles, page.title) in self._folder_chains
        )

    def _compute_stems(self) -> dict[str, str]:
        """Assign each page a filename stem with no page ID.

        Two pages can share a title — either genuinely (a common heading
        re-used across the space) or because they land in the same
        directory once ``include_page_id`` is off and the ID that used to
        disambiguate them is gone. Rather than let the second one silently
        overwrite the first, every page after the first with the same
        ``(folder, sanitized title)`` key gets a stable ``" (2)"``,
        ``" (3)"``, … suffix. "Stable" here means: for the same input page
        list, in the same order, the same page always gets the same
        suffix — good enough for a re-run over an unchanged export, though
        two same-titled pages could in principle swap suffixes if the
        Confluence API ever returns them in a different relative order.
        """
        counts: dict[tuple[Path, str], int] = {}
        stems: dict[str, str] = {}
        for page in self._pages:
            title = sanitize_filename(page.title)
            folder = self._relative_dir(page)
            if self._is_folder_page(page):
                # Claims ``<folder>/<folder>.md`` so a same-titled subpage
                # inside it gets the " (2)" suffix instead of overwriting it.
                folder = folder / title
            key = (folder, title)
            counts[key] = counts.get(key, 0) + 1
            n = counts[key]
            stems[page.id] = key[1] if n == 1 else f"{key[1]} ({n})"
        return stems

    def _destination(self, page: PageRef, suffix: str) -> Path:
        if self._is_folder_page(page):
            # A page with subpages lives inside the folder it became, so the
            # folder is self-contained: ``Docs/Docs.md`` beside its children
            # rather than ``Docs_123.md`` one level up.
            folder = sanitize_filename(page.title)
            return (
                self._options.output_dir
                / self._relative_dir(page)
                / folder
                / f"{folder}{suffix}"
            )
        if self._options.include_page_id:
            stem = f"{sanitize_filename(page.title)}_{page.id}"
        else:
            stem = self._stems[page.id]
        return self._options.output_dir / self._relative_dir(page) / f"{stem}{suffix}"

    def _image_destination(self, page: PageRef, filename: str) -> Path:
        """Where a downloaded embedded image lands in the shared images folder.

        Prefixed with the page ID so two pages that both have a file named
        e.g. ``diagram.png`` don't collide in the shared folder.
        """
        safe_name = sanitize_filename(filename)
        return (
            self._options.output_dir / IMAGES_DIRNAME / f"{page.id}_{safe_name}"
        )

    def _file_destination(self, page: PageRef, filename: str) -> Path:
        """Where a downloaded linked file lands in the shared files folder.

        Kept separate from :meth:`_image_destination` — a linked PDF or
        ``.docx`` isn't an image, so it doesn't belong in ``images/``.
        """
        safe_name = sanitize_filename(filename)
        return (
            self._options.output_dir / FILES_DIRNAME / f"{page.id}_{safe_name}"
        )

    def _page_url(self, page_id: str) -> str:
        base = self._credentials.base_url.rstrip("/")
        return f"{base}/pages/viewpage.action?pageId={page_id}"

    # --- organizational (blank parent) pages -----------------------------

    def _has_children_in_export(self, page: PageRef) -> bool:
        """Whether some other page in this run has ``page`` as an ancestor.

        Confluence titles are unique per space, so appearing anywhere in
        another page's ancestor chain is a reliable enough signal that this
        page exists only to hold that page (and possibly siblings) — not a
        perfect guarantee against a rare cross-space title collision, but
        good enough to distinguish "empty on purpose" from "actually broken".
        """
        return any(page.title in other.ancestor_titles for other in self._pages)

    def _is_organizational_page(self, page: PageRef) -> bool:
        """A blank page with subpages under it isn't a failure — it's a
        Confluence pattern for grouping pages in the tree, same as a folder
        with no files of its own. Its title already appears in its
        subpages' paths (mirrored layout) or ancestor chain regardless, so
        there's nothing lost by not writing a file for it."""
        return self._has_children_in_export(page)

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

        Falls back to the live Confluence URL for a link out of the exported
        scope, so it still goes somewhere useful — unless ``link_out_of_scope``
        is off, in which case it degrades to plain text instead. A link to a
        different space needs a logged-in browser session to open, which
        makes it a dead end in an export shared with someone who doesn't have
        one, or opened offline.
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
            if not self._options.link_out_of_scope:
                return ""
            base = self._credentials.base_url.rstrip("/")
            space_key = space or self._space_key
            return f"{base}/display/{quote(space_key)}/{quote(title)}"

        return resolve

    def _remote_attachment_url(self, page: PageRef, filename: str) -> str:
        base = self._credentials.base_url.rstrip("/")
        return f"{base}/download/attachments/{page.id}/{quote(filename)}"

    def _download_resolver(
        self,
        page: PageRef,
        client: ConfluenceClient | None,
        cache: dict[tuple[str, str], Path],
        destination_for: Callable[[PageRef, str], Path],
        on_success: Callable[[], None],
        on_failure: Callable[[], None],
    ):
        """Shared body for the image and linked-file download resolvers.

        Downloads through ``client``, caches by ``(page id, filename)`` so a
        repeat reference within the same run costs nothing, and falls back to
        the live Confluence URL if the fetch fails — a broken relative link
        to a file that was never saved would be worse.
        """
        source_dir = self._destination(page, ".md").parent

        def resolve(filename: str) -> str:
            if not filename:
                return ""
            cache_key = (page.id, filename)
            destination = cache.get(cache_key)
            if destination is None:
                destination = destination_for(page, filename)
                if not destination.exists() or self._options.overwrite:
                    try:
                        data = client.download_attachment(page.id, filename)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(data)
                        on_success()
                    except (ConfluenceError, OSError) as exc:
                        on_failure()
                        self.log.emit(
                            f"  ! Could not download '{filename}' for "
                            f"{page.title}: {exc}"
                        )
                        return self._remote_attachment_url(page, filename)
                cache[cache_key] = destination
            rel = os.path.relpath(destination, source_dir)
            return quote(rel.replace(os.sep, "/"))

        return resolve

    def _attachment_resolver_for(
        self, page: PageRef, client: ConfluenceClient | None = None
    ):
        """Resolve an embedded ``ac:image`` to a Markdown image target.

        Default behaviour links back at the server —
        ``/download/attachments/<pageId>/<file>`` is the canonical Server/DC
        path and resolves for anyone with a logged-in browser session, which
        is a far better outcome than a dead relative link to a file that was
        never fetched. When ``download_images`` is on, the file is instead
        pulled down into a shared ``images/`` folder and linked relatively,
        falling back to the server link if the download fails.
        """
        if not self._options.download_images:

            def resolve_remote(filename: str) -> str:
                if not filename:
                    return ""
                return self._remote_attachment_url(page, filename)

            return resolve_remote

        def record_success() -> None:
            self._stats.images_downloaded += 1

        def record_failure() -> None:
            self._stats.images_failed += 1

        return self._download_resolver(
            page,
            client,
            self._downloaded_images,
            self._image_destination,
            record_success,
            record_failure,
        )

    def _attachment_link_resolver_for(
        self, page: PageRef, client: ConfluenceClient | None = None
    ):
        """Resolve a link to a file attachment (``ac:link`` + ``ri:attachment``).

        Off by default, same as images — the link stays pointed at the
        server. When ``download_linked_files`` is on, the file is pulled down
        into a shared ``files/`` folder (kept separate from ``images/``,
        since a linked PDF or ``.docx`` isn't an image) and linked relatively,
        falling back to the server link if the download fails.
        """
        if not self._options.download_linked_files:

            def resolve_remote(filename: str) -> str:
                if not filename:
                    return ""
                return self._remote_attachment_url(page, filename)

            return resolve_remote

        def record_success() -> None:
            self._stats.files_downloaded += 1

        def record_failure() -> None:
            self._stats.files_failed += 1

        return self._download_resolver(
            page,
            client,
            self._downloaded_files,
            self._file_destination,
            record_success,
            record_failure,
        )

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
                if isinstance(exc, BlankPageError):
                    wrote_placeholder = False
                    if opts.write_blank_pages and opts.wants_md:
                        try:
                            path = self._write_placeholder(page, exc.raw)
                        except OSError as write_exc:
                            self.log.emit(
                                f"  ! Could not write placeholder: {write_exc}"
                            )
                        else:
                            wrote_placeholder = True
                            self._stats.placeholders += 1
                            self.log.emit(f"  -> {path} (placeholder)")
                            self.page_done.emit(page.id, str(path))
                    if not self._is_organizational_page(page):
                        # Typically a page whose content was cleared (e.g.
                        # after a migration) but which was never deleted.
                        self._stats.blank += 1
                        self.log.emit(
                            "  = Blank page (empty in Confluence)"
                            + ("" if wrote_placeholder else ", skipped")
                            + f": {page.title}"
                        )
                    elif opts.mirror_tree:
                        self._stats.organizational += 1
                        self.log.emit(
                            "  = No content of its own — its title became a "
                            f"folder for its subpages: {page.title}"
                        )
                    else:
                        self._stats.organizational += 1
                        self.log.emit(
                            "  = No content of its own (a Confluence "
                            f"organizational page): {page.title}"
                        )
                    continue
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
            self._stats.succeeded,
            self._stats.failed,
            self._stats.skipped,
            self._stats.organizational,
            self._stats.blank,
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
            raise BlankPageError(raw)

        result = convert_storage(
            storage,
            link_resolver=self._link_resolver_for(page, link_index),
            attachment_resolver=self._attachment_resolver_for(page, client),
            attachment_link_resolver=self._attachment_link_resolver_for(page, client),
        )
        self._stats.unknown_macros.update(result.unknown_macros)
        self._stats.attachments += len(result.attachments)

        body = result.markdown
        if self._options.front_matter:
            body = self._front_matter(page, raw) + "\n\n" + body

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(body.rstrip() + "\n", encoding="utf-8")
        return destination

    def _write_placeholder(self, page: PageRef, raw: dict) -> Path:
        """Write a stand-in ``.md`` for a page that is empty in Confluence.

        Keeps the page visible in the export (and its links and index entry
        working) even though there's no content to convert.
        """
        destination = self._destination(page, ".md")
        if destination.exists() and not self._options.overwrite:
            return destination
        body = (
            f"# {page.title}\n\n"
            "_This page is empty in Confluence._\n\n"
            f"Source: {self._page_url(page.id)}"
        )
        if self._options.front_matter:
            body = self._front_matter(page, raw) + "\n\n" + body
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(body + "\n", encoding="utf-8")
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
        """Write a ``README.md`` mirroring the page hierarchy.

        The exported tree has no other entry point, so this is its map —
        named README.md rather than index.md so it's the file a reader (or
        an LLM) lands on first when just browsing the output folder.
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
        if self._stats.organizational:
            self.log.emit(
                f"Note: {self._stats.organizational} page(s) had no content "
                "of their own — they exist only to group subpages and were "
                "skipped rather than counted as failures."
            )
        if self._stats.placeholders:
            self.log.emit(
                f"Wrote {self._stats.placeholders} placeholder file(s) for "
                "blank pages."
            )
        if self._stats.blank:
            self.log.emit(
                f"Note: {self._stats.blank} page(s) are blank in Confluence "
                "(no content and no subpages — often emptied after a "
                "migration) and were "
                + (
                    "written as placeholders"
                    if self._options.write_blank_pages and self._options.wants_md
                    else "skipped"
                )
                + " rather than counted as failures."
            )
        if self._options.download_images:
            if self._stats.images_downloaded:
                self.log.emit(
                    f"Downloaded {self._stats.images_downloaded} embedded "
                    f"image(s) into {IMAGES_DIRNAME}/ and linked them locally."
                )
            if self._stats.images_failed:
                self.log.emit(
                    f"Note: {self._stats.images_failed} embedded image(s) "
                    "could not be downloaded and were linked to Confluence "
                    "URLs instead."
                )
        if self._options.download_linked_files:
            if self._stats.files_downloaded:
                self.log.emit(
                    f"Downloaded {self._stats.files_downloaded} linked "
                    f"file(s) into {FILES_DIRNAME}/ and linked them locally."
                )
            if self._stats.files_failed:
                self.log.emit(
                    f"Note: {self._stats.files_failed} linked file(s) could "
                    "not be downloaded and were linked to Confluence URLs "
                    "instead."
                )
        if (
            not self._options.download_images
            and not self._options.download_linked_files
            and self._stats.attachments
        ):
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
