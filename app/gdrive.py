"""Mirror an exported Markdown folder into Google Drive as Google Docs.

Runs *after* an export, against the local output folder, the same way
"Convert MD to PDF" does — so it works with every layout option and never
talks to Confluence. The destination is a Drive folder (in My Drive or a
Shared Drive) identified by the URL the user pastes from the browser.

What it does on every run:

* Recreates the local folder tree under the destination folder.
* Turns every ``.md`` into a Google Doc. The Markdown is rendered to HTML
  locally and uploaded with conversion, so headings, lists, tables and
  emphasis become native Docs formatting. Local images are embedded as
  ``data:`` URIs — Google cannot fetch a relative path, and a Confluence URL
  needs a login it doesn't have.
* Rewrites links between exported pages to the target Docs' URLs, and
  uploads locally linked files (``files/…``) so their links work too.

Re-runs are idempotent. Every item it creates is tagged with Drive
``appProperties`` — ``l33chRoot`` (the destination folder) and ``l33chKey``
(the Confluence page ID when the export carries one, else the relative path)
— so the next run finds and *updates* the same Doc instead of creating a
duplicate, even from another machine, and even when a page was renamed or
moved in Confluence. A content hash (``l33chHash``) skips Docs whose
rendered content hasn't changed, which also leaves edits made in Docs alone
until the Confluence page itself changes. Items it did not create are never
touched, and nothing is ever deleted.

The Google client libraries are optional dependencies, imported lazily so
the rest of the app runs without them.
"""

from __future__ import annotations

import base64
import hashlib
import html as html_lib
import mimetypes
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit, parse_qs

from PySide6.QtCore import QObject, Signal

from .worker import INDEX_FILENAME, LOG_FILENAME, STATE_FILENAME


# Full Drive scope: the destination is a folder someone else created (often
# in a Shared Drive), which the narrower `drive.file` scope cannot see.
SCOPES = ["https://www.googleapis.com/auth/drive"]

TOKEN_FILENAME = "google-token.json"

DOC_MIME = "application/vnd.google-apps.document"
FOLDER_MIME = "application/vnd.google-apps.folder"

DOC_URL = "https://docs.google.com/document/d/{id}/edit"
FILE_URL = "https://drive.google.com/file/d/{id}/view"

INSTALL_HINT = (
    "Install the Google client libraries with: "
    "pip install google-api-python-client google-auth-oauthlib markdown"
)

# Retries for rate limits (403 userRateLimitExceeded / 429) and 5xx, with
# the client library's own exponential backoff.
RETRIES = 5

# Never uploaded: the app's own bookkeeping.
_IGNORED_NAMES = frozenset({STATE_FILENAME, LOG_FILENAME})

_FOLDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")
_FRONT_MATTER_RE = re.compile(r"\A---\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)
_PAGE_ID_SUFFIX_RE = re.compile(r"_(\d+)$")
_TASK_RE = re.compile(r"^(\s*[-*+]\s+)\[([ xX])\][ \t]+", re.MULTILINE)
_ATTR_RE = re.compile(r'\b(href|src)="([^"]*)"')


class DriveError(RuntimeError):
    """A failure worth showing the user verbatim."""


# --- pure helpers ------------------------------------------------------


def parse_folder_id(text: str) -> str:
    """Extract a Drive folder ID from a pasted URL, or accept a bare ID.

    Handles ``/drive/folders/<id>``, ``/drive/u/1/folders/<id>``,
    ``/drive/folders/<id>?usp=sharing`` and ``open?id=<id>``. Returns ``""``
    for anything else, which the GUI turns into a message.
    """
    text = (text or "").strip()
    if not text:
        return ""
    if "://" not in text:
        return text if _FOLDER_ID_RE.match(text) else ""
    parts = urlsplit(text)
    segments = [s for s in parts.path.split("/") if s]
    if "folders" in segments:
        index = segments.index("folders")
        if index + 1 < len(segments) and _FOLDER_ID_RE.match(segments[index + 1]):
            return segments[index + 1]
    ids = parse_qs(parts.query).get("id")
    if ids and _FOLDER_ID_RE.match(ids[0]):
        return ids[0]
    return ""


def split_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Split the YAML front matter the exporter writes from the body.

    Only the flat ``key: value`` lines the exporter itself emits are parsed;
    that is all this needs, and it avoids a YAML dependency.
    """
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        return {}, text
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip().strip('"')
    return meta, text[match.end():]


def page_identity(rel_path: str, meta: dict[str, str]) -> tuple[str, str]:
    """``(key, title)`` for one exported ``.md`` file.

    The key prefers the Confluence page ID — from front matter, else the
    ``_<id>`` filename suffix — because it survives renames and moves; a
    file without one is keyed by its relative path instead. Keys are hashed
    when long: Drive caps an appProperty's key + value at 124 bytes.
    """
    stem = Path(rel_path).stem
    page_id = meta.get("page_id", "")
    suffix = _PAGE_ID_SUFFIX_RE.search(stem)
    if not page_id and suffix:
        page_id = suffix.group(1)
    title = meta.get("title") or (
        _PAGE_ID_SUFFIX_RE.sub("", stem) if suffix else stem
    )
    if page_id.isdigit():
        return f"page:{page_id}", title
    return path_key("path", rel_path), title


def path_key(kind: str, rel_path: str) -> str:
    """A bounded-length appProperty value for a relative path."""
    digest = hashlib.sha1(rel_path.encode("utf-8")).hexdigest()
    return f"{kind}:{digest}"


def checkboxes_to_symbols(markdown_text: str) -> str:
    """``- [x] Done`` → ``- ☑ Done``.

    Python-Markdown has no task-list syntax, so without this the brackets
    land in the Doc literally.
    """
    return _TASK_RE.sub(
        lambda m: m.group(1) + ("☐ " if m.group(2) == " " else "☑ "),
        markdown_text,
    )


def markdown_to_doc_html(markdown_text: str, title: str) -> str:
    """Render one page's Markdown (front matter already removed) to HTML.

    Kept minimal: Docs' importer ignores most CSS, and anything it doesn't
    ignore fights the Doc's own styles once people start editing.
    """
    import markdown as markdown_lib

    body = markdown_lib.markdown(
        checkboxes_to_symbols(markdown_text),
        extensions=["tables", "fenced_code", "sane_lists"],
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html_lib.escape(title)}</title></head>"
        f"<body>{body}</body></html>"
    )


def resolve_local(href: str, source_dir: Path, root: Path) -> Path | None:
    """The file under ``root`` a relative link points at, or None.

    Absolute URLs, in-page anchors and links that escape the export folder
    resolve to nothing and are left untouched.
    """
    href = html_lib.unescape(href)
    if not href or href.startswith("#") or ":" in href.split("/", 1)[0]:
        return None
    path_part = unquote(href.split("#", 1)[0].split("?", 1)[0])
    if not path_part:
        return None
    target = (source_dir / path_part).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return None
    return target


def data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def rewrite_html(
    html: str,
    source_dir: Path,
    root: Path,
    link_target: Callable[[Path], str],
) -> tuple[str, int]:
    """Point links at Drive and inline local images.

    ``link_target(path)`` returns the Drive URL for a local file, or ``""``
    to leave the link as-is. Returns the HTML and the number of images that
    still reference a remote URL — Google fetches those anonymously, so a
    Confluence-hosted image silently disappears from the Doc.
    """
    remote_images = 0

    def repl(match: re.Match) -> str:
        nonlocal remote_images
        attr, value = match.group(1), match.group(2)
        target = resolve_local(value, source_dir, root)
        if attr == "src":
            if target is not None and target.is_file():
                return f'src="{data_uri(target)}"'
            if value.startswith(("http://", "https://")):
                remote_images += 1
            return match.group(0)
        if target is None:
            return match.group(0)
        url = link_target(target)
        if not url:
            return match.group(0)
        return f'href="{html_lib.escape(url, quote=True)}"'

    return _ATTR_RE.sub(repl, html), remote_images


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- the mirror --------------------------------------------------------


@dataclass
class MirrorStats:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    files_uploaded: int = 0
    remote_images: int = 0
    errors: list[str] = field(default_factory=list)


class DriveMirror:
    """Sync a local export folder into one Drive folder.

    ``service`` is a Drive v3 resource from ``googleapiclient.discovery.build``
    — injected, so tests can drive this with a fake.
    """

    def __init__(
        self,
        service: Any,
        folder_id: str,
        local_root: Path,
        log: Callable[[str], None] = lambda _msg: None,
        progress: Callable[[int, int, str], None] = lambda *_a: None,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> None:
        self._files = service.files()
        self._root_id = folder_id
        self._root = local_root
        self._log = log
        self._progress = progress
        self._cancelled = cancelled
        self._drive_id = ""
        self._existing: dict[str, dict] = {}   # l33chKey -> Drive file
        self._folders: dict[str, str] = {}     # relative dir ("" = root) -> id
        self._linked_done: set[str] = set()    # linked-file keys synced this run
        self.stats = MirrorStats()

    # --- Drive plumbing ---------------------------------------------

    def _list_kwargs(self) -> dict:
        if self._drive_id:
            return {"corpora": "drive", "driveId": self._drive_id,
                    "includeItemsFromAllDrives": True, "supportsAllDrives": True}
        return {"corpora": "user", "supportsAllDrives": True,
                "includeItemsFromAllDrives": True}

    def _check_root(self) -> None:
        try:
            meta = self._files.get(
                fileId=self._root_id,
                fields="id,name,mimeType,driveId,capabilities(canAddChildren)",
                supportsAllDrives=True,
            ).execute(num_retries=RETRIES)
        except Exception as exc:  # noqa: BLE001 — HttpError and transport errors
            raise DriveError(
                f"Cannot open the Drive folder {self._root_id}: {exc}. Check the "
                "URL and that the signed-in account is a member of the Shared "
                "Drive (or has edit access to the folder)."
            ) from exc
        if meta.get("mimeType") != FOLDER_MIME:
            raise DriveError(f"'{meta.get('name')}' is not a folder.")
        if not (meta.get("capabilities") or {}).get("canAddChildren", True):
            raise DriveError(
                f"The signed-in account cannot add files to '{meta.get('name')}'. "
                "It needs Contributor (or higher) access."
            )
        self._drive_id = meta.get("driveId", "")
        self._log(f"Drive folder: {meta.get('name')}")

    def _load_existing(self) -> None:
        query = (
            "appProperties has { key='l33chRoot' and value='%s' } and trashed=false"
            % self._root_id
        )
        token = None
        while True:
            resp = self._files.list(
                q=query,
                fields="nextPageToken,files(id,name,mimeType,parents,appProperties)",
                pageSize=1000,
                pageToken=token,
                **self._list_kwargs(),
            ).execute(num_retries=RETRIES)
            for item in resp.get("files", []):
                key = (item.get("appProperties") or {}).get("l33chKey")
                if key:
                    self._existing[key] = item
            token = resp.get("nextPageToken")
            if not token:
                break

    def _tags(self, key: str, **extra: str) -> dict:
        return {"l33chRoot": self._root_id, "l33chKey": key, **extra}

    def _place(self, key: str, name: str, parent: str, mime: str) -> tuple[str, bool]:
        """Find or create the item for ``key``; move/rename it if needed.

        Returns ``(id, created)``. ``mime`` is only used when creating.
        """
        item = self._existing.get(key)
        if item is None:
            created = self._files.create(
                body={"name": name, "mimeType": mime, "parents": [parent],
                      "appProperties": self._tags(key)},
                fields="id",
                supportsAllDrives=True,
            ).execute(num_retries=RETRIES)
            self._existing[key] = {"id": created["id"], "name": name,
                                   "parents": [parent], "appProperties": {}}
            return created["id"], True
        parents = item.get("parents") or []
        kwargs: dict[str, Any] = {}
        if parents != [parent]:
            kwargs["addParents"] = parent
            if parents:
                kwargs["removeParents"] = ",".join(parents)
        if item.get("name") != name or kwargs:
            self._files.update(
                fileId=item["id"], body={"name": name}, fields="id",
                supportsAllDrives=True, **kwargs,
            ).execute(num_retries=RETRIES)
            item["name"], item["parents"] = name, [parent]
        return item["id"], False

    def _folder(self, rel_dir: str) -> str:
        """The Drive folder mirroring ``rel_dir``, created on demand."""
        if rel_dir in self._folders:
            return self._folders[rel_dir]
        if rel_dir in ("", "."):
            self._folders[rel_dir] = self._root_id
            return self._root_id
        parent_rel, _, name = rel_dir.rpartition("/")
        parent = self._folder(parent_rel)
        folder_id, _created = self._place(
            path_key("dir", rel_dir), name, parent, FOLDER_MIME
        )
        self._folders[rel_dir] = folder_id
        return folder_id

    def _stored_hash(self, key: str) -> str:
        item = self._existing.get(key) or {}
        return (item.get("appProperties") or {}).get("l33chHash", "")

    def _put_content(self, key: str, data: bytes, mime: str, digest: str) -> None:
        """Replace an item's content. Uploading HTML onto a Google Doc makes
        Drive convert it, and the Doc keeps its ID, URL and sharing."""
        from googleapiclient.http import MediaInMemoryUpload

        item = self._existing[key]
        self._files.update(
            fileId=item["id"],
            # appProperties is merged by key, so this keeps the l33ch tags.
            body={"appProperties": {"l33chHash": digest}},
            media_body=MediaInMemoryUpload(data, mimetype=mime, resumable=True),
            fields="id",
            supportsAllDrives=True,
        ).execute(num_retries=RETRIES)
        item.setdefault("appProperties", {})["l33chHash"] = digest

    # --- local side ---------------------------------------------------

    def _rel(self, path: Path) -> str:
        return path.resolve().relative_to(self._root.resolve()).as_posix()

    def _markdown_files(self) -> list[Path]:
        return sorted(
            p for p in self._root.rglob("*.md")
            if p.is_file() and p.name not in _IGNORED_NAMES
        )

    def _upload_linked_file(self, path: Path) -> str:
        """Upload a linked non-Markdown file; returns its Drive URL."""
        rel = self._rel(path)
        key = path_key("file", rel)
        if key in self._linked_done:
            return FILE_URL.format(id=self._existing[key]["id"])
        data = path.read_bytes()
        digest = content_hash(data)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        parent = self._folder(rel.rpartition("/")[0])
        if key not in self._existing:
            from googleapiclient.http import MediaInMemoryUpload

            created = self._files.create(
                body={"name": path.name, "parents": [parent],
                      "appProperties": self._tags(key, l33chHash=digest)},
                media_body=MediaInMemoryUpload(data, mimetype=mime, resumable=True),
                fields="id",
                supportsAllDrives=True,
            ).execute(num_retries=RETRIES)
            self._existing[key] = {"id": created["id"], "name": path.name,
                                   "parents": [parent],
                                   "appProperties": {"l33chHash": digest}}
            self.stats.files_uploaded += 1
        else:
            self._place(key, path.name, parent, "")
            if self._stored_hash(key) != digest:
                self._put_content(key, data, mime, digest)
                self.stats.files_uploaded += 1
        self._linked_done.add(key)
        return FILE_URL.format(id=self._existing[key]["id"])

    # --- run ------------------------------------------------------------

    def run(self) -> MirrorStats:
        self._check_root()
        self._load_existing()

        files = self._markdown_files()
        if not files:
            self._log(f"No .md files found in {self._root}.")
            return self.stats

        # Pass 1: make sure every page has a Doc, so pass 2 can link to any
        # of them regardless of order.
        pages: list[tuple[Path, str, str, bool]] = []  # path, key, title, new
        doc_for: dict[Path, str] = {}
        total = len(files)
        self._log(f"Preparing {total} Google Doc(s)…")
        for index, path in enumerate(files):
            if self._cancelled():
                return self.stats
            rel = self._rel(path)
            self._progress(index, total * 2, rel)
            meta, _body = split_front_matter(path.read_text(encoding="utf-8"))
            key, title = page_identity(rel, meta)
            if path.name == INDEX_FILENAME and rel == INDEX_FILENAME:
                key, title = path_key("path", rel), "README"
            try:
                parent = self._folder(rel.rpartition("/")[0])
                doc_id, created = self._place(key, title, parent, DOC_MIME)
            except Exception as exc:  # noqa: BLE001
                self.stats.failed += 1
                self._fail(rel, exc)
                continue
            if created:
                self.stats.created += 1
            pages.append((path, key, title, created))
            doc_for[path.resolve()] = doc_id

        def link_target(target: Path) -> str:
            if target in doc_for:
                return DOC_URL.format(id=doc_for[target])
            if target.is_file() and target.suffix.lower() != ".md":
                try:
                    return self._upload_linked_file(target)
                except Exception as exc:  # noqa: BLE001
                    self._fail(self._rel(target), exc)
            return ""

        # Pass 2: content.
        for index, (path, key, title, created) in enumerate(pages):
            if self._cancelled():
                break
            rel = self._rel(path)
            self._progress(total + index, total * 2, rel)
            try:
                _meta, body = split_front_matter(path.read_text(encoding="utf-8"))
                html, remote = rewrite_html(
                    markdown_to_doc_html(body, title),
                    path.parent.resolve(), self._root, link_target,
                )
                self.stats.remote_images += remote
                data = html.encode("utf-8")
                digest = content_hash(data)
                if self._stored_hash(key) == digest:
                    self.stats.unchanged += 1
                    continue
                self._put_content(key, data, "text/html", digest)
                if not created:
                    self.stats.updated += 1
                doc_url = DOC_URL.format(id=self._existing[key]["id"])
                self._log(f"  -> {rel}  ({doc_url})")
            except Exception as exc:  # noqa: BLE001
                self.stats.failed += 1
                self._fail(rel, exc)

        self._progress(total * 2, total * 2, "")
        return self.stats

    def _fail(self, rel: str, exc: Exception) -> None:
        message = f"{rel}: {type(exc).__name__}: {exc}"
        self.stats.errors.append(message)
        self._log(f"  ! Failed {message}")


# --- authentication ----------------------------------------------------


def token_path() -> Path:
    from .config import config_path

    return config_path().parent / TOKEN_FILENAME


def missing_dependency() -> str:
    """The name of a missing optional library, or ``""`` when all are there."""
    for module in ("googleapiclient", "google_auth_oauthlib", "markdown"):
        try:
            __import__(module)
        except ImportError:
            return module
    return ""


def load_credentials(path: Path | None = None):
    """Saved credentials, refreshed if needed; None when not signed in."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    path = path or token_path()
    if not path.is_file():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    except (ValueError, OSError):
        return None
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:  # noqa: BLE001 — RefreshError, transport
            raise DriveError(
                f"The saved Google sign-in no longer works ({exc}). "
                "Click 'Sign in' again."
            ) from exc
        _save_token(creds, path)
        return creds
    return None


def _save_token(creds, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    # Created owner-only: it holds a refresh token.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(creds.to_json())
    tmp.replace(path)


def sign_out(path: Path | None = None) -> None:
    path = path or token_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def build_service(creds):
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def account_email(service) -> str:
    about = service.about().get(fields="user(emailAddress)").execute(
        num_retries=RETRIES
    )
    return (about.get("user") or {}).get("emailAddress", "")


class SignInWorker(QObject):
    """Runs the browser OAuth flow off the GUI thread.

    ``run_local_server`` blocks until the browser redirects back to a
    loopback port, which can take as long as the user takes to click — so
    the GUI runs this on a daemon thread rather than a QThread.
    """

    finished = Signal(str, str)   # email ("" on failure), error message

    def __init__(self, client_secrets: Path) -> None:
        super().__init__()
        self._client_secrets = client_secrets

    def run(self) -> None:
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow

            flow = InstalledAppFlow.from_client_secrets_file(
                str(self._client_secrets), SCOPES
            )
            creds = flow.run_local_server(
                port=0, open_browser=True, timeout_seconds=300,
                authorization_prompt_message="",
                success_message=(
                    "Signed in. You can close this tab and return to "
                    "Confluence L33ch."
                ),
            )
            _save_token(creds, token_path())
            email = account_email(build_service(creds))
        except Exception as exc:  # noqa: BLE001
            self.finished.emit("", f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(email or "(signed in)", "")


class DriveUploadWorker(QObject):
    """Qt wrapper running :class:`DriveMirror` on a worker thread."""

    progress = Signal(int, int, str)
    log = Signal(str)
    # created, updated, unchanged, failed
    finished = Signal(int, int, int, int)

    def __init__(self, local_root: Path, folder_id: str) -> None:
        super().__init__()
        self._root = local_root
        self._folder_id = folder_id
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        missing = missing_dependency()
        if missing:
            self.log.emit(f"! Missing dependency: {missing}. {INSTALL_HINT}")
            self.finished.emit(0, 0, 0, 1)
            return
        try:
            creds = load_credentials()
            if creds is None:
                raise DriveError("Not signed in to Google. Click 'Sign in' first.")
            self.log.emit(f"Uploading {self._root} to Google Drive…")
            mirror = DriveMirror(
                build_service(creds), self._folder_id, self._root,
                log=self.log.emit, progress=self.progress.emit,
                cancelled=lambda: self._cancelled,
            )
            stats = mirror.run()
        except Exception as exc:  # noqa: BLE001
            self.log.emit(f"! {exc}")
            self.finished.emit(0, 0, 0, 1)
            return
        if self._cancelled:
            self.log.emit("Upload cancelled by user.")
        if stats.remote_images:
            self.log.emit(
                f"! {stats.remote_images} image(s) still point at Confluence and "
                "will be missing from the Docs — Google can't sign in to fetch "
                "them. Tick 'Download images to a central folder' and re-export."
            )
        self.finished.emit(stats.created, stats.updated, stats.unchanged, stats.failed)
