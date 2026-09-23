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


def test_mirror_layout_puts_parent_page_inside_its_folder(tmp_path):
    parent = PageRef(id="8", title="Middle", ancestor_titles=("Top",))
    child = PageRef(id="9", title="Child", ancestor_titles=("Top", "Middle"))
    worker = _worker(tmp_path, [parent, child], mirror_tree=True)
    assert worker._destination(parent, ".md") == (
        tmp_path / "Top" / "Middle" / "Middle.md"
    )
    assert worker._destination(child, ".md") == (
        tmp_path / "Top" / "Middle" / "Child_9.md"
    )


def test_subpage_named_like_its_parent_folder_is_disambiguated(tmp_path):
    parent = PageRef(id="8", title="Docs")
    child = PageRef(id="9", title="Docs", ancestor_titles=("Docs",))
    worker = _worker(
        tmp_path, [parent, child], mirror_tree=True, include_page_id=False
    )
    assert worker._destination(parent, ".md") == tmp_path / "Docs" / "Docs.md"
    assert worker._destination(child, ".md") == tmp_path / "Docs" / "Docs (2).md"


def test_parent_page_stays_flat_without_mirror_layout(tmp_path):
    parent = PageRef(id="8", title="Parent")
    child = PageRef(id="9", title="Child", ancestor_titles=("Parent",))
    worker = _worker(tmp_path, [parent, child], mirror_tree=False)
    assert worker._destination(parent, ".md") == tmp_path / "Parent_8.md"


def test_page_id_can_be_omitted_from_filenames(tmp_path):
    page = PageRef(id="123", title="My Page")
    worker = _worker(tmp_path, [page], include_page_id=False)
    assert worker._destination(page, ".md") == tmp_path / "My Page.md"


def test_omitting_page_id_disambiguates_same_titled_siblings(tmp_path):
    a = PageRef(id="1", title="Duplicate")
    b = PageRef(id="2", title="Duplicate")
    c = PageRef(id="3", title="Duplicate")
    worker = _worker(tmp_path, [a, b, c], include_page_id=False)
    assert worker._destination(a, ".md") == tmp_path / "Duplicate.md"
    assert worker._destination(b, ".md") == tmp_path / "Duplicate (2).md"
    assert worker._destination(c, ".md") == tmp_path / "Duplicate (3).md"


def test_omitting_page_id_only_disambiguates_within_the_same_folder(tmp_path):
    a = PageRef(id="1", title="Duplicate", ancestor_titles=("Alpha",))
    b = PageRef(id="2", title="Duplicate", ancestor_titles=("Beta",))
    worker = _worker(tmp_path, [a, b], mirror_tree=True, include_page_id=False)
    assert worker._destination(a, ".md") == tmp_path / "Alpha" / "Duplicate.md"
    assert worker._destination(b, ".md") == tmp_path / "Beta" / "Duplicate.md"


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


def test_link_outside_export_can_degrade_to_plain_text(tmp_path):
    a = PageRef(id="1", title="Alpha")
    worker = _worker(tmp_path, [a], link_out_of_scope=False)
    resolve = worker._link_resolver_for(a, worker._build_link_index())
    assert resolve("Not Exported", "OTHER") == ""


def test_link_outside_export_plain_text_leaves_in_scope_links_alone(tmp_path):
    a = PageRef(id="1", title="Alpha")
    b = PageRef(id="2", title="Beta")
    worker = _worker(tmp_path, [a, b], link_out_of_scope=False)
    resolve = worker._link_resolver_for(a, worker._build_link_index())
    assert resolve("Beta", "DOCS") == "Beta_2.md"


def test_attachment_resolver_points_at_the_page(tmp_path):
    page = PageRef(id="42", title="Alpha")
    worker = _worker(tmp_path, [page])
    resolve = worker._attachment_resolver_for(page)
    assert resolve("a b.png") == (
        "https://wiki.example.com/download/attachments/42/a%20b.png"
    )


class _StubDownloadClient:
    def __init__(self, content=b"binary-data", error=None):
        self.content = content
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def download_attachment(self, page_id, filename):
        self.calls.append((page_id, filename))
        if self.error is not None:
            raise self.error
        return self.content


def test_download_images_writes_into_central_folder(tmp_path):
    page = PageRef(id="42", title="Alpha")
    worker = _worker(tmp_path, [page], download_images=True)
    client = _StubDownloadClient()
    resolve = worker._attachment_resolver_for(page, client)

    result = resolve("diagram.png")

    assert result == "images/42_diagram.png"
    assert (tmp_path / "images" / "42_diagram.png").read_bytes() == b"binary-data"
    assert client.calls == [("42", "diagram.png")]


def test_download_images_is_relative_to_mirrored_page_location(tmp_path):
    page = PageRef(id="9", title="Child", ancestor_titles=("Top", "Middle"))
    worker = _worker(tmp_path, [page], mirror_tree=True, download_images=True)
    client = _StubDownloadClient()
    resolve = worker._attachment_resolver_for(page, client)

    assert resolve("pic.png") == "../../images/9_pic.png"


def test_download_images_caches_repeat_lookups(tmp_path):
    page = PageRef(id="42", title="Alpha")
    worker = _worker(tmp_path, [page], download_images=True)
    client = _StubDownloadClient()
    resolve = worker._attachment_resolver_for(page, client)

    assert resolve("diagram.png") == resolve("diagram.png")
    assert client.calls == [("42", "diagram.png")]


def test_download_images_falls_back_to_remote_link_on_failure(tmp_path):
    from app.confluence_client import ConfluenceError

    page = PageRef(id="42", title="Alpha")
    worker = _worker(tmp_path, [page], download_images=True)
    client = _StubDownloadClient(error=ConfluenceError("boom"))
    logs: list[str] = []
    worker.log.connect(logs.append)
    resolve = worker._attachment_resolver_for(page, client)

    result = resolve("diagram.png")

    assert result == (
        "https://wiki.example.com/download/attachments/42/diagram.png"
    )
    assert not (tmp_path / "images" / "42_diagram.png").exists()
    assert any("Could not download" in line for line in logs)


def test_attachment_link_resolver_points_at_the_page(tmp_path):
    page = PageRef(id="42", title="Alpha")
    worker = _worker(tmp_path, [page])
    resolve = worker._attachment_link_resolver_for(page)
    assert resolve("report.pdf") == (
        "https://wiki.example.com/download/attachments/42/report.pdf"
    )


def test_download_linked_files_writes_into_central_folder(tmp_path):
    page = PageRef(id="42", title="Alpha")
    worker = _worker(tmp_path, [page], download_linked_files=True)
    client = _StubDownloadClient()
    resolve = worker._attachment_link_resolver_for(page, client)

    result = resolve("report.pdf")

    assert result == "files/42_report.pdf"
    assert (tmp_path / "files" / "42_report.pdf").read_bytes() == b"binary-data"
    assert client.calls == [("42", "report.pdf")]


def test_download_linked_files_is_independent_of_download_images(tmp_path):
    page = PageRef(id="42", title="Alpha")
    worker = _worker(tmp_path, [page], download_images=True)
    client = _StubDownloadClient()
    resolve = worker._attachment_link_resolver_for(page, client)

    assert resolve("report.pdf") == (
        "https://wiki.example.com/download/attachments/42/report.pdf"
    )
    assert client.calls == []


def test_download_linked_files_falls_back_to_remote_link_on_failure(tmp_path):
    from app.confluence_client import ConfluenceError

    page = PageRef(id="42", title="Alpha")
    worker = _worker(tmp_path, [page], download_linked_files=True)
    client = _StubDownloadClient(error=ConfluenceError("boom"))
    logs: list[str] = []
    worker.log.connect(logs.append)
    resolve = worker._attachment_link_resolver_for(page, client)

    result = resolve("report.pdf")

    assert result == (
        "https://wiki.example.com/download/attachments/42/report.pdf"
    )
    assert not (tmp_path / "files" / "42_report.pdf").exists()
    assert any("Could not download" in line for line in logs)


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
