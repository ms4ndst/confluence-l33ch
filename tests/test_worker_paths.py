"""Tests for output-path construction and intra-wiki link rewriting."""

from pathlib import Path

from app.confluence_client import Credentials, PageRef
from app.worker import ExportOptions, ExportWorker, sanitize_filename


def _worker(
    tmp_path: Path, pages: list[PageRef], space_key: str = "DOCS", **opts
) -> ExportWorker:
    return ExportWorker(
        pages,
        credentials=Credentials(base_url="https://wiki.example.com"),
        space_key=space_key,
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


def test_leading_numbers_are_padded_to_sort_within_a_folder(tmp_path):
    pages = [
        PageRef(id="4", title="4. Context"),
        PageRef(id="10", title="10. Improvement"),
        PageRef(id="99", title="Appendix"),
    ]
    worker = _worker(tmp_path, pages, pad_numbers=True, include_page_id=False)
    assert worker._destination(pages[0], ".md") == tmp_path / "04. Context.md"
    assert worker._destination(pages[1], ".md") == tmp_path / "10. Improvement.md"
    assert worker._destination(pages[2], ".md") == tmp_path / "Appendix.md"


def test_date_style_names_do_not_widen_number_padding(tmp_path):
    pages = [
        PageRef(id="1", title="7. Notes"),
        PageRef(id="2", title="12. Summary"),
        PageRef(id="3", title="2023-04-04 Meeting notes"),
        PageRef(id="4", title="2014.12.10 Upgrade meeting"),
    ]
    worker = _worker(tmp_path, pages, pad_numbers=True, include_page_id=False)
    assert worker._destination(pages[0], ".md") == tmp_path / "07. Notes.md"
    assert worker._destination(pages[2], ".md") == (
        tmp_path / "2023-04-04 Meeting notes.md"
    )
    assert worker._destination(pages[3], ".md") == (
        tmp_path / "2014.12.10 Upgrade meeting.md"
    )


def test_number_padding_is_off_by_default(tmp_path):
    pages = [PageRef(id="4", title="4. Context"), PageRef(id="10", title="10. X")]
    worker = _worker(tmp_path, pages, include_page_id=False)
    assert worker._destination(pages[0], ".md") == tmp_path / "4. Context.md"


def test_number_padding_applies_to_mirrored_folders_per_level(tmp_path):
    # "4. Context" and "10. Improvement" are siblings at the top level; the
    # subpages inside "4. Context" only go up to 9, so they stay unpadded.
    ctx = PageRef(id="4", title="4. Context")
    imp = PageRef(id="10", title="10. Improvement")
    sub = PageRef(id="41", title="1. Scope", ancestor_titles=("4. Context",))
    worker = _worker(
        tmp_path, [ctx, imp, sub], mirror_tree=True, pad_numbers=True
    )
    assert worker._destination(ctx, ".md") == (
        tmp_path / "04. Context" / "04. Context.md"
    )
    assert worker._destination(sub, ".md") == (
        tmp_path / "04. Context" / "1. Scope_41.md"
    )


def test_padded_names_are_used_for_links(tmp_path):
    a = PageRef(id="1", title="1. Alpha")
    b = PageRef(id="2", title="10. Beta")
    worker = _worker(tmp_path, [a, b], pad_numbers=True, include_page_id=False)
    resolve = worker._link_resolver_for(a, worker._build_link_index())
    assert resolve("1. Alpha", "DOCS") == "01.%20Alpha.md"


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


def test_parse_space_keys():
    from app.confluence_client import parse_space_keys

    assert parse_space_keys(" VSA, cepl ,,VSA ") == ["VSA", "cepl"]
    assert parse_space_keys("") == []


def test_several_spaces_get_their_own_folders(tmp_path):
    a = PageRef(id="1", title="Home", space_key="VSA")
    b = PageRef(id="2", title="Home", space_key="CEPL")
    worker = _worker(tmp_path, [a, b], space_key="VSA, CEPL")
    assert worker._destination(a, ".md") == tmp_path / "VSA" / "Home_1.md"
    assert worker._destination(b, ".md") == tmp_path / "CEPL" / "Home_2.md"


def test_several_spaces_mirrored_layout_is_per_space(tmp_path):
    parent = PageRef(id="1", title="Docs", space_key="VSA")
    child = PageRef(id="2", title="Intro", ancestor_titles=("Docs",), space_key="VSA")
    # Same title in another space, without children: must not become a folder.
    other = PageRef(id="3", title="Docs", space_key="CEPL")
    worker = _worker(
        tmp_path, [parent, child, other], space_key="VSA, CEPL", mirror_tree=True
    )
    assert worker._destination(parent, ".md") == tmp_path / "VSA" / "Docs" / "Docs.md"
    assert worker._destination(child, ".md") == tmp_path / "VSA" / "Docs" / "Intro_2.md"
    assert worker._destination(other, ".md") == tmp_path / "CEPL" / "Docs_3.md"


def test_links_resolve_within_the_linking_pages_space(tmp_path):
    a1 = PageRef(id="1", title="Alpha", space_key="VSA")
    b1 = PageRef(id="2", title="Beta", space_key="VSA")
    b2 = PageRef(id="3", title="Beta", space_key="CEPL")
    worker = _worker(tmp_path, [a1, b1, b2], space_key="VSA, CEPL")
    resolve = worker._link_resolver_for(a1, worker._build_link_index())
    assert resolve("Beta", "") == "Beta_2.md"
    assert resolve("Beta", "CEPL") == "../CEPL/Beta_3.md"
