"""Tests for output-path construction and intra-wiki link rewriting."""

from pathlib import Path

from app.confluence_client import Credentials, PageRef
from app.worker import ExportOptions, ExportWorker, sanitize_filename


def _worker(tmp_path: Path, pages: list[PageRef], **opts) -> ExportWorker:
    return ExportWorker(
        pages,
        credentials=Credentials(base_url="https://wiki.example.com"),
        space_key="DOCS",
        options=ExportOptions(output_dir=tmp_path, **opts),
    )


def test_sanitize_strips_invalid_characters():
    assert sanitize_filename('a/b:c*d?"e<f>g|h') == "a_b_c_d_e_f_g_h"


def test_sanitize_collapses_repeats_and_trims():
    assert sanitize_filename("  a///b .") == "a_b"


def test_sanitize_falls_back_for_empty_result():
    assert sanitize_filename("...") == "untitled"


def test_sanitize_caps_length():
    assert len(sanitize_filename("x" * 500)) == 180


def test_destination_is_title_and_id(tmp_path):
    page = PageRef(id="123", title="My Page")
    worker = _worker(tmp_path, [page])
    assert worker._destination(page, ".md") == tmp_path / "My Page_123.md"


def test_flat_layout_ignores_ancestors(tmp_path):
    page = PageRef(id="9", title="Child", ancestor_titles=("Parent",))
    worker = _worker(tmp_path, [page], mirror_tree=False)
    assert worker._destination(page, ".md") == tmp_path / "Child_9.md"


def test_mirror_layout_uses_ancestor_folders(tmp_path):
    page = PageRef(id="9", title="Child", ancestor_titles=("Top", "Middle"))
    worker = _worker(tmp_path, [page], mirror_tree=True)
    assert worker._destination(page, ".md") == (
        tmp_path / "Top" / "Middle" / "Child_9.md"
    )


def test_link_to_exported_page_becomes_relative(tmp_path):
    a = PageRef(id="1", title="Alpha")
    b = PageRef(id="2", title="Beta")
    worker = _worker(tmp_path, [a, b])
    resolve = worker._link_resolver_for(a, worker._build_link_index())
    assert resolve("Beta", "DOCS") == "Beta_2.md"


def test_link_lookup_is_case_insensitive(tmp_path):
    a = PageRef(id="1", title="Alpha")
    b = PageRef(id="2", title="Beta")
    worker = _worker(tmp_path, [a, b])
    resolve = worker._link_resolver_for(a, worker._build_link_index())
    assert resolve("  bETA ", "DOCS") == "Beta_2.md"


def test_link_across_mirrored_folders_walks_up(tmp_path):
    a = PageRef(id="1", title="Alpha", ancestor_titles=("Top",))
    b = PageRef(id="2", title="Beta")
    worker = _worker(tmp_path, [a, b], mirror_tree=True)
    resolve = worker._link_resolver_for(a, worker._build_link_index())
    assert resolve("Beta", "DOCS") == "../Beta_2.md"


def test_link_outside_export_falls_back_to_confluence_url(tmp_path):
    a = PageRef(id="1", title="Alpha")
    worker = _worker(tmp_path, [a])
    resolve = worker._link_resolver_for(a, worker._build_link_index())
    assert resolve("Not Exported", "OTHER") == (
        "https://wiki.example.com/display/OTHER/Not%20Exported"
    )


def test_link_rewriting_can_be_disabled(tmp_path):
    a = PageRef(id="1", title="Alpha")
    b = PageRef(id="2", title="Beta")
    worker = _worker(tmp_path, [a, b], resolve_links=False)
    resolve = worker._link_resolver_for(a, worker._build_link_index())
    assert resolve("Beta", "DOCS").startswith("https://wiki.example.com/display/")


def test_attachment_resolver_points_at_the_page(tmp_path):
    page = PageRef(id="42", title="Alpha")
    worker = _worker(tmp_path, [page])
    resolve = worker._attachment_resolver_for(page)
    assert resolve("a b.png") == (
        "https://wiki.example.com/download/attachments/42/a%20b.png"
    )


def test_front_matter_contains_traceable_fields(tmp_path):
    page = PageRef(id="7", title='Quote "Test"', last_updated="2025-01-02T03:04:05Z")
    worker = _worker(tmp_path, [page])
    fm = worker._front_matter(page, {"version": {"number": 3}})
    assert fm.startswith("---") and fm.endswith("---")
    assert 'page_id: "7"' in fm
    assert "source: https://wiki.example.com/pages/viewpage.action?pageId=7" in fm
    assert "updated: 2025-01-02T03:04:05Z" in fm
    assert "version: 3" in fm
    # Double quotes in a title would break the YAML scalar.
    assert '"Quote \'Test\'"' in fm


def test_index_lists_every_page_with_depth_indent(tmp_path):
    pages = [
        PageRef(id="1", title="Root", is_root=True, depth=0),
        PageRef(id="2", title="Child", depth=1, ancestor_titles=()),
    ]
    worker = _worker(tmp_path, pages)
    path = worker._write_index()
    text = path.read_text(encoding="utf-8")
    assert "- [Root](Root_1.md)" in text
    assert "  - [Child](Child_2.md)" in text


def test_format_flags(tmp_path):
    md_only = ExportOptions(output_dir=tmp_path, export_format="md")
    pdf_only = ExportOptions(output_dir=tmp_path, export_format="pdf")
    both = ExportOptions(output_dir=tmp_path, export_format="both")
    assert (md_only.wants_md, md_only.wants_pdf) == (True, False)
    assert (pdf_only.wants_md, pdf_only.wants_pdf) == (False, True)
    assert (both.wants_md, both.wants_pdf) == (True, True)
