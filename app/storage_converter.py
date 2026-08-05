"""Confluence storage format (XHTML + ``ac:``/``ri:`` macros) → Markdown.

Storage format is XHTML with Confluence's own ``ac:``/``ri:`` namespaces laid
over it, so string substitution cannot handle it: tables, code macros, nested
lists and links all need structure to come out right. This module parses the
document with :mod:`html.parser` — stdlib only, no BeautifulSoup — and emits
Markdown that survives a linter.

What it handles
---------------

* Headings, paragraphs, ``<br>``, ``<hr>``, blockquotes
* Inline ``strong``/``em``/``code``/``del``/``sup``/``sub``
* Nested ordered and unordered lists, plus ``ac:task-list`` checkboxes
* Tables, including header rows and multi-line cells (joined with ``<br>``)
* ``ac:structured-macro``: code / noformat (fenced, with language), the
  admonition family (info, note, warning, tip, panel → blockquotes), expand,
  status, jira, and a pass-through for anything unrecognised so content is
  never silently dropped
* Navigation macros (toc, children, pagetree) are removed — the export's own
  file layout replaces them
* ``ac:link`` to other pages, resolved through an injectable callback so the
  worker can point links at the sibling ``.md`` files it just wrote
* ``ac:image`` with ``ri:attachment`` or ``ri:url``

Anything genuinely unknown is recorded in :attr:`StorageConverter.unknown_macros`
so the caller can log it rather than pretend the conversion was lossless.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from typing import Callable


# Macros whose entire subtree is navigation furniture. The exported tree is
# the navigation, so these are dropped rather than rendered.
DROPPED_MACROS = frozenset({
    "toc", "toc-zone", "children", "pagetree", "anchor", "recently-updated",
    "livesearch", "navmap", "include",
})

# name → label used for the blockquote heading
ADMONITION_MACROS = {
    "info": "Info",
    "note": "Note",
    "tip": "Tip",
    "warning": "Warning",
    "panel": "Panel",
    "error": "Error",
    "success": "Success",
}

CODE_MACROS = frozenset({"code", "noformat"})

INLINE_WRAPPERS = {
    "strong": "**", "b": "**",
    "em": "*", "i": "*",
    "code": "`",
    "del": "~~", "s": "~~", "strike": "~~",
}

HEADINGS = {f"h{n}": "#" * n for n in range(1, 7)}


@dataclass
class ConversionResult:
    """Markdown plus what the converter had to guess about."""

    markdown: str
    unknown_macros: set[str] = field(default_factory=set)
    attachments: set[str] = field(default_factory=set)
    dropped_macros: set[str] = field(default_factory=set)


LinkResolver = Callable[[str, str], str]
"""``(page_title, space_key) -> link target``. Return "" to emit plain text."""

AttachmentResolver = Callable[[str], str]
"""``(filename) -> image src``. Return "" to emit the filename as plain text."""


class StorageConverter(HTMLParser):
    """Streaming storage-format → Markdown converter.

    Output is accumulated into a stack of *sinks*. Constructs that need to
    post-process their own content (list items, table cells, blockquotes,
    macro bodies) push a fresh sink, let their children write into it, then
    pop it and re-indent or wrap the result. That's what makes arbitrary
    nesting — a table inside a list item inside an info macro — work without
    a separate tree-building pass.
    """

    def __init__(
        self,
        link_resolver: LinkResolver | None = None,
        attachment_resolver: AttachmentResolver | None = None,
    ) -> None:
        # convert_charrefs=False so `handle_entityref` fires and we can decide
        # what to unescape ourselves — inside a code block, `&lt;` must stay
        # literal text, not become a `<` that a downstream renderer eats.
        super().__init__(convert_charrefs=True)
        self._link_resolver = link_resolver
        self._attachment_resolver = attachment_resolver

        self._sinks: list[list[str]] = [[]]
        self._list_stack: list[list] = []      # [kind, counter] per nesting level
        self._pre_depth = 0                    # >0 → preserve whitespace verbatim
        self._skip_depth = 0                   # >0 → discard everything
        self._skip_tag: str | None = None

        self._tables: list[_Table] = []
        self._macros: list[_Macro] = []
        self._link_href: list[str] = []
        self._link_title: str = ""
        self._param_name: str | None = None
        self._task_complete = False

        self.unknown_macros: set[str] = set()
        self.dropped_macros: set[str] = set()
        self.attachments: set[str] = set()

    # --- sink plumbing --------------------------------------------------

    def _write(self, text: str) -> None:
        if self._skip_depth:
            return
        self._sinks[-1].append(text)

    def _push(self) -> None:
        self._sinks.append([])

    def _pop(self) -> str:
        return "".join(self._sinks.pop())

    def _break(self) -> None:
        """Separate two block-level constructs. Normalised again at the end."""
        self._write("\n\n")

    # --- public API -----------------------------------------------------

    def result(self) -> ConversionResult:
        self.close()
        while len(self._sinks) > 1:          # unbalanced markup: flush anyway
            tail = self._pop()
            self._write(tail)
        return ConversionResult(
            markdown=_normalise(self._pop()),
            unknown_macros=set(self.unknown_macros),
            attachments=set(self.attachments),
            dropped_macros=set(self.dropped_macros),
        )

    # --- HTMLParser hooks ----------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}

        if self._skip_depth:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return

        if tag in HEADINGS:
            self._break()
            self._write(HEADINGS[tag] + " ")
            return

        if tag == "p":
            self._break()
            return

        if tag == "br":
            # Two trailing spaces is the only line break Markdown honours
            # inside a paragraph without turning on HTML rendering.
            self._write("  \n")
            return

        if tag == "hr":
            self._break()
            self._write("---")
            self._break()
            return

        if tag in INLINE_WRAPPERS:
            self._write(INLINE_WRAPPERS[tag])
            return

        if tag in ("sup", "sub"):
            self._write(f"<{tag}>")
            return

        if tag in ("ul", "ol"):
            # A list nested inside a list item must not be preceded by a blank
            # line, or Markdown renders the whole thing as a loose list. Each
            # item already emits its own leading newline, so a nested list
            # needs no separator at all.
            if not self._list_stack:
                self._break()
            self._list_stack.append([tag, 0])
            return

        if tag == "li":
            self._push()
            return

        if tag == "pre":
            self._pre_depth += 1
            self._break()
            self._write("```\n")
            return

        if tag == "blockquote":
            self._push()
            return

        if tag == "table":
            self._tables.append(_Table())
            return

        if tag == "tr" and self._tables:
            self._tables[-1].start_row()
            return

        if tag in ("td", "th") and self._tables:
            self._tables[-1].header_seen |= (tag == "th")
            self._push()
            return

        if tag == "a":
            self._link_href.append(a.get("href", ""))
            self._push()
            return

        if tag == "img":
            src = a.get("src", "")
            alt = a.get("alt", "")
            if src:
                self._write(f"![{alt}]({src})")
            return

        if tag == "time":
            self._write(a.get("datetime", ""))
            return

        # --- Confluence-specific ---------------------------------------

        if tag == "ac:structured-macro":
            self._start_macro(a.get("ac:name", "").lower())
            return

        if tag == "ac:parameter":
            self._param_name = a.get("ac:name", "").lower()
            self._push()
            return

        if tag in ("ac:plain-text-body", "ac:plain-text-link-body"):
            self._pre_depth += 1
            self._push()
            return

        if tag in ("ac:rich-text-body", "ac:task-body", "ac:link-body"):
            self._push()
            return

        if tag == "ac:link":
            self._link_href.append("")     # filled in by ri:page
            self._push()
            return

        if tag == "ac:image":
            self._macros.append(_Macro("__image__"))
            return

        if tag == "ri:page":
            title = a.get("ri:content-title", "")
            space = a.get("ri:space-key", "")
            if self._link_href:
                self._link_href[-1] = self._resolve_page(title, space)
                # Remember the title so an empty link body falls back to it —
                # `<ac:link><ri:page .../></ac:link>` with no body is common.
                self._link_title = title
            return

        if tag == "ri:attachment":
            filename = a.get("ri:filename", "")
            if filename:
                self.attachments.add(filename)
            if self._macros and self._macros[-1].name == "__image__":
                self._macros[-1].params["filename"] = filename
            elif self._link_href:
                self._link_href[-1] = self._resolve_attachment(filename)
                self._link_title = filename
            return

        if tag == "ri:url":
            value = a.get("ri:value", "")
            if self._macros and self._macros[-1].name == "__image__":
                self._macros[-1].params["url"] = value
            elif self._link_href:
                self._link_href[-1] = value
            return

        if tag == "ac:task-list":
            self._list_stack.append(["task", 0])
            self._break()
            return

        if tag == "ac:task":
            self._push()
            return

        if tag == "ac:task-status":
            self._push()
            return

        if tag == "ac:emoticon":
            self._write(_EMOTICONS.get(a.get("ac:name", ""), ""))
            return

        if tag in ("ac:layout", "ac:layout-section", "ac:layout-cell",
                   "div", "span", "tbody", "thead", "tfoot", "colgroup", "col",
                   "ac:adf-extension", "ac:adf-node", "ac:adf-content"):
            return

        # Unrecognised tag: render nothing for it but keep its children.

    def handle_endtag(self, tag: str) -> None:
        if self._skip_depth:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth == 0:
                    self._skip_tag = None
            return

        if tag in HEADINGS:
            self._break()
            return

        if tag == "p":
            self._break()
            return

        if tag in INLINE_WRAPPERS:
            self._write(INLINE_WRAPPERS[tag])
            return

        if tag in ("sup", "sub"):
            self._write(f"</{tag}>")
            return

        if tag in ("ul", "ol", "ac:task-list"):
            if self._list_stack:
                self._list_stack.pop()
            # Only the outermost list gets a trailing blank line; a nested one
            # is still inside its parent's <li> sink.
            if not self._list_stack:
                self._break()
            return

        if tag == "li":
            self._close_list_item(self._pop())
            return

        if tag == "ac:task":
            self._close_list_item(self._pop())
            return

        if tag == "ac:task-status":
            status = self._pop().strip().lower()
            self._task_complete = status == "complete"
            return

        if tag == "pre":
            self._pre_depth = max(0, self._pre_depth - 1)
            self._write("\n```")
            self._break()
            return

        if tag == "blockquote":
            body = self._pop().strip()
            self._break()
            self._write(_prefix_lines(body, "> "))
            self._break()
            return

        if tag in ("td", "th") and self._tables:
            self._tables[-1].add_cell(self._pop())
            return

        if tag == "tr" and self._tables:
            self._tables[-1].end_row()
            return

        if tag == "table" and self._tables:
            table = self._tables.pop()
            self._break()
            self._write(table.render())
            self._break()
            return

        if tag == "a":
            self._close_link()
            return

        if tag == "ac:link":
            self._close_link()
            return

        if tag == "ac:image":
            self._close_image()
            return

        if tag == "ac:parameter":
            value = self._pop().strip()
            if self._macros and self._param_name:
                self._macros[-1].params[self._param_name] = value
            self._param_name = None
            return

        if tag in ("ac:plain-text-body", "ac:plain-text-link-body"):
            self._pre_depth = max(0, self._pre_depth - 1)
            body = self._pop()
            if tag == "ac:plain-text-link-body":
                self._write(body.strip())
            elif self._macros:
                self._macros[-1].body = body
            else:
                self._write(body)
            return

        if tag in ("ac:rich-text-body", "ac:task-body", "ac:link-body"):
            body = self._pop()
            if tag == "ac:rich-text-body" and self._macros:
                self._macros[-1].body = body
            else:
                self._write(body)
            return

        if tag == "ac:structured-macro":
            self._close_macro()
            return

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data:
            return
        if self._pre_depth:
            self._write(data)
            return
        # Collapse runs of whitespace — storage format is pretty-printed XHTML
        # and the newlines in it are not content.
        text = re.sub(r"\s+", " ", data)
        if text.strip() == "" and not self._sinks[-1]:
            return
        self._write(text)

    def unknown_decl(self, data: str) -> None:
        """Capture ``<![CDATA[...]]>`` — how code macros carry their body."""
        if data.startswith("CDATA["):
            self._write(data[len("CDATA["):])

    def handle_entityref(self, name: str) -> None:  # pragma: no cover
        self._write(unescape(f"&{name};"))

    # --- constructs -----------------------------------------------------

    def _close_list_item(self, body: str) -> None:
        body = body.strip()
        if not body:
            return
        kind, counter = (self._list_stack[-1] if self._list_stack else ["ul", 0])
        counter += 1
        if self._list_stack:
            self._list_stack[-1][1] = counter

        if kind == "ol":
            marker = f"{counter}. "
        elif kind == "task":
            marker = "- [x] " if getattr(self, "_task_complete", False) else "- [ ] "
            self._task_complete = False
        else:
            marker = "- "

        lines = body.split("\n")
        rendered = [marker + lines[0]]
        # Continuation lines (including any nested list) indent by the marker
        # width so Markdown keeps them inside this item.
        pad = " " * len(marker)
        rendered.extend(pad + line if line.strip() else "" for line in lines[1:])
        self._write("\n" + "\n".join(rendered))

    def _close_link(self) -> None:
        text = self._pop().strip()
        href = self._link_href.pop() if self._link_href else ""
        fallback = getattr(self, "_link_title", "")
        self._link_title = ""
        label = text or fallback or href
        if not label:
            return
        if href:
            self._write(f"[{label}]({href})")
        else:
            self._write(label)

    def _close_image(self) -> None:
        macro = self._macros.pop() if self._macros else _Macro("__image__")
        url = macro.params.get("url", "")
        filename = macro.params.get("filename", "")
        if url:
            self._write(f"![]({url})")
            return
        if not filename:
            return
        src = self._resolve_attachment(filename)
        if src:
            self._write(f"![{filename}]({src})")
        else:
            self._write(f"`[image: {filename}]`")

    def _start_macro(self, name: str) -> None:
        if name in DROPPED_MACROS:
            self.dropped_macros.add(name)
            self._skip_depth = 1
            self._skip_tag = "ac:structured-macro"
            return
        self._macros.append(_Macro(name))

    def _close_macro(self) -> None:
        if not self._macros:
            return
        macro = self._macros.pop()
        name = macro.name

        if name in CODE_MACROS:
            lang = macro.params.get("language", "") if name == "code" else ""
            body = macro.body.strip("\n")
            self._break()
            self._write(f"```{lang}\n{body}\n```")
            self._break()
            return

        if name in ADMONITION_MACROS:
            label = macro.params.get("title") or ADMONITION_MACROS[name]
            body = _normalise(macro.body).strip()
            self._break()
            block = f"**{label}**" + (f"\n\n{body}" if body else "")
            self._write(_prefix_lines(block, "> "))
            self._break()
            return

        if name == "expand":
            title = macro.params.get("title") or "Details"
            body = _normalise(macro.body).strip()
            self._break()
            self._write(f"**{title}**\n\n{body}" if body else f"**{title}**")
            self._break()
            return

        if name == "status":
            title = macro.params.get("title", "").strip()
            if title:
                self._write(f"`{title}`")
            return

        if name in ("jira", "jira-issues"):
            key = macro.params.get("key") or macro.params.get("jqlquery", "")
            self._write(f"`JIRA: {key}`" if key else "`JIRA issue`")
            return

        if name in ("excerpt", "excerpt-include", "section", "column",
                    "align", "div", "span", "unmigrated-inline-wiki-markup"):
            # Layout-only wrappers: keep the content, drop the wrapper.
            self._write(macro.body)
            return

        # Unrecognised macro. Emit whatever body it had rather than dropping
        # user content, and record the name so the log can say what happened.
        self.unknown_macros.add(name)
        if macro.body.strip():
            self._write(macro.body)
        elif macro.params:
            params = ", ".join(f"{k}={v}" for k, v in sorted(macro.params.items()))
            self._write(f"`[{name} macro: {params}]`")
        else:
            self._write(f"`[{name} macro]`")

    # --- resolvers ------------------------------------------------------

    def _resolve_page(self, title: str, space: str) -> str:
        if self._link_resolver is None:
            return ""
        return self._link_resolver(title, space)

    def _resolve_attachment(self, filename: str) -> str:
        if self._attachment_resolver is None:
            return ""
        return self._attachment_resolver(filename)


@dataclass
class _Macro:
    name: str
    params: dict[str, str] = field(default_factory=dict)
    body: str = ""


class _Table:
    """Accumulates cells, then renders a GitHub-flavoured pipe table."""

    def __init__(self) -> None:
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self.header_seen = False

    def start_row(self) -> None:
        self._row = []

    def add_cell(self, content: str) -> None:
        if self._row is None:
            self._row = []
        # A cell can legitimately contain paragraphs or a list; Markdown
        # tables are single-line, so soft-wrap with <br> and escape pipes.
        text = _normalise(content).strip()
        text = text.replace("|", "\\|")
        text = re.sub(r"\n{2,}", "<br><br>", text)
        text = text.replace("\n", "<br>")
        self._row.append(text or " ")

    def end_row(self) -> None:
        if self._row:
            self.rows.append(self._row)
        self._row = None

    def render(self) -> str:
        if not self.rows:
            return ""
        width = max(len(r) for r in self.rows)
        rows = [r + [" "] * (width - len(r)) for r in self.rows]
        if self.header_seen:
            header, body = rows[0], rows[1:]
        else:
            # Markdown has no headerless table; an empty header row is the
            # least-bad rendering and keeps the columns aligned.
            header, body = [" "] * width, rows
        lines = [
            "| " + " | ".join(header) + " |",
            "|" + "|".join(["---"] * width) + "|",
        ]
        lines.extend("| " + " | ".join(r) + " |" for r in body)
        return "\n".join(lines)


_EMOTICONS = {
    "tick": "✅", "cross": "❌", "check": "✅",
    "warning": "⚠️", "information": "ℹ️", "question": "❓",
    "smile": "🙂", "sad": "🙁", "thumbs-up": "👍", "thumbs-down": "👎",
    "light-on": "💡", "star": "⭐", "red-star": "⭐",
}


def _prefix_lines(text: str, prefix: str) -> str:
    """Prefix every line, leaving blank lines as a bare (stripped) prefix."""
    out = []
    for line in text.split("\n"):
        out.append(prefix + line if line.strip() else prefix.rstrip())
    return "\n".join(out)


def _normalise(text: str) -> str:
    """Collapse the whitespace noise block emission leaves behind.

    Trailing whitespace is stripped except for the deliberate two-space hard
    line break, and runs of blank lines collapse to one (markdownlint MD009,
    MD012, MD047).
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for line in text.split("\n"):
        stripped = line.rstrip()
        if stripped and line.endswith("  "):
            lines.append(stripped + "  ")
        else:
            lines.append(stripped)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def convert_storage(
    storage_xhtml: str,
    link_resolver: LinkResolver | None = None,
    attachment_resolver: AttachmentResolver | None = None,
) -> ConversionResult:
    """Convert one page's storage-format body to Markdown.

    Never raises on malformed markup: :class:`html.parser.HTMLParser` is
    lenient, and unbalanced sinks are flushed in :meth:`StorageConverter.result`.
    """
    converter = StorageConverter(link_resolver, attachment_resolver)
    converter.feed(storage_xhtml or "")
    return converter.result()
