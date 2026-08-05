"""Convert exported Markdown to PDF via ``markdown`` + ``pdfkit``/wkhtmltopdf.

Used when the Confluence instance has its own PDF export disabled, which is
common. Three properties this needs to have as part of a GUI:

* The wkhtmltopdf lookup is a function, not import-time module state, so a
  path the user just typed into the field takes effect without a restart.
* Missing dependencies report a message rather than terminating the app, and
  nothing is ever ``pip install``-ed behind the user's back.
* Conversion runs on a worker thread and reports per-file progress.

The generated HTML carries a print stylesheet, because bare HTML renders as
unstyled Times New Roman with unruled tables — legible, but not something you
would hand to anyone.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import QObject, Signal


WKHTML_CANDIDATES = (
    r"C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe",
    r"C:\Program Files (x86)\wkhtmltopdf\bin\wkhtmltopdf.exe",
)

DOWNLOAD_HINT = (
    "Install wkhtmltopdf from https://wkhtmltopdf.org/downloads.html, then "
    "either add its 'bin' folder to PATH or point the wkhtmltopdf field at "
    "wkhtmltopdf.exe."
)

# Deliberately plain and print-friendly: a PDF is not a themed UI surface, so
# this is neutral typography rather than the app's Catppuccin palette.
PRINT_CSS = """
body { font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
       font-size: 11pt; line-height: 1.45; color: #1f2328; margin: 0; }
h1, h2, h3, h4 { line-height: 1.25; margin: 1.2em 0 0.4em; }
h1 { font-size: 20pt; border-bottom: 1px solid #d0d7de; padding-bottom: 4px; }
h2 { font-size: 16pt; }
h3 { font-size: 13pt; }
code, pre { font-family: Consolas, "Courier New", monospace; font-size: 9.5pt; }
pre { background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 4px;
      padding: 8px; white-space: pre-wrap; word-wrap: break-word; }
code { background: #f6f8fa; padding: 1px 4px; border-radius: 3px; }
pre code { background: none; padding: 0; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; }
th, td { border: 1px solid #d0d7de; padding: 5px 8px; text-align: left;
         vertical-align: top; }
th { background: #f6f8fa; }
blockquote { border-left: 3px solid #d0d7de; margin: 1em 0; padding: 0 1em;
             color: #57606a; }
img { max-width: 100%; }
a { color: #0969da; }
"""


def find_wkhtmltopdf(explicit: str = "") -> str:
    """Return a usable wkhtmltopdf path, or "" if none can be found.

    Order: the path the user gave, then ``WKHTMLTOPDF_PATH``, then the two
    default Windows install locations, then PATH.
    """
    import os

    candidates = [
        explicit.strip(),
        os.environ.get("WKHTMLTOPDF_PATH", ""),
        *WKHTML_CANDIDATES,
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    found = shutil.which("wkhtmltopdf")
    return found or ""


def wkhtmltopdf_version(path: str = "") -> tuple[bool, str]:
    """Run ``wkhtmltopdf --version``. Returns ``(ok, message)``."""
    import subprocess

    exe = find_wkhtmltopdf(path)
    if not exe:
        return False, "wkhtmltopdf was not found.\n\n" + DOWNLOAD_HINT
    try:
        completed = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except OSError as exc:
        return False, f"Could not run {exe}: {exc}"
    output = (completed.stdout or completed.stderr or "").strip()
    if completed.returncode != 0:
        return False, f"{exe} exited with {completed.returncode}.\n{output}"
    return True, f"{exe}\n{output or 'wkhtmltopdf OK'}"


def markdown_to_html(markdown_text: str, title: str) -> str:
    """Render Markdown to a standalone, styled HTML document."""
    import markdown as markdown_lib

    html_body = markdown_lib.markdown(
        markdown_text,
        extensions=["tables", "fenced_code", "toc", "sane_lists"],
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title><style>{PRINT_CSS}</style></head>"
        f"<body>{html_body}</body></html>"
    )


class MdToPdfWorker(QObject):
    """Converts every ``.md`` under a directory to a sibling ``.pdf``."""

    progress = Signal(int, int, str)   # done, total, current filename
    log = Signal(str)
    finished = Signal(int, int, int)   # success, failure, skipped

    def __init__(
        self,
        directory: Path,
        wkhtmltopdf_path: str = "",
        recursive: bool = True,
        overwrite: bool = True,
    ) -> None:
        super().__init__()
        self._directory = directory
        self._wkhtmltopdf_path = wkhtmltopdf_path
        self._recursive = recursive
        self._overwrite = overwrite
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            import pdfkit  # noqa: F401  — imported for the side-effect check
            import markdown  # noqa: F401
        except ImportError as exc:
            self.log.emit(
                f"! Missing dependency: {exc.name}. "
                "Install with: pip install markdown pdfkit"
            )
            self.finished.emit(0, 0, 0)
            return

        exe = find_wkhtmltopdf(self._wkhtmltopdf_path)
        if not exe:
            self.log.emit("! wkhtmltopdf not found. " + DOWNLOAD_HINT)
            self.finished.emit(0, 0, 0)
            return
        self.log.emit(f"Using wkhtmltopdf: {exe}")

        import pdfkit

        config = pdfkit.configuration(wkhtmltopdf=exe)

        pattern = "**/*.md" if self._recursive else "*.md"
        # index.md is generated navigation, not content worth printing.
        files = sorted(
            p for p in self._directory.glob(pattern)
            if p.is_file() and p.name != "index.md"
        )
        if not files:
            self.log.emit(f"No .md files found in {self._directory}.")
            self.finished.emit(0, 0, 0)
            return

        total = len(files)
        success = failure = skipped = 0
        self.log.emit(f"Converting {total} Markdown file(s) to PDF…")

        for index, md_file in enumerate(files):
            if self._cancelled:
                self.log.emit("Conversion cancelled by user.")
                break
            self.progress.emit(index, total, md_file.name)
            pdf_file = md_file.with_suffix(".pdf")
            if pdf_file.exists() and not self._overwrite:
                skipped += 1
                self.log.emit(f"= Exists, skipped: {pdf_file.name}")
                continue
            try:
                html = markdown_to_html(
                    md_file.read_text(encoding="utf-8"), md_file.stem
                )
                # enable-local-file-access is required for wkhtmltopdf 0.12.6+
                # to load the relative images an export with extracted
                # attachments would reference.
                pdfkit.from_string(
                    html,
                    str(pdf_file),
                    configuration=config,
                    options={
                        "quiet": "",
                        "encoding": "UTF-8",
                        "enable-local-file-access": "",
                        "margin-top": "18mm",
                        "margin-bottom": "18mm",
                        "margin-left": "16mm",
                        "margin-right": "16mm",
                    },
                )
                success += 1
                self.log.emit(f"  -> {pdf_file}")
            except Exception as exc:  # noqa: BLE001
                failure += 1
                self.log.emit(f"  ! Failed {md_file.name}: {type(exc).__name__}: {exc}")

        self.progress.emit(total, total, "")
        self.finished.emit(success, failure, skipped)
