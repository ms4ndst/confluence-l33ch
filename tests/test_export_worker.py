"""End-to-end test of the export worker against a stubbed REST client."""

import json

import pytest

from app import worker as worker_module
from app.confluence_client import Credentials, PageRef
from app.worker import STATE_FILENAME, ExportOptions, ExportWorker


STORAGE = {
    "1": "<h1>Alpha</h1><p>See <ac:link><ri:page ri:content-title=\"Beta\"/>"
         "</ac:link>.</p>",
    "2": "<p>Beta body.</p>",
}


class StubClient:
    """Stands in for ConfluenceClient; records what was asked for."""

    instances: list["StubClient"] = []

    def __init__(self, credentials, timeout=30):
        self.credentials = credentials
        self.body_calls: list[str] = []
        self.pdf_calls: list[str] = []
        StubClient.instances.append(self)

    def storage_body(self, page_id):
        self.body_calls.append(page_id)
        return STORAGE[page_id], {"version": {"number": 1}}

    def export_pdf(self, page_id):
        self.pdf_calls.append(page_id)
        return b"%PDF-1.4 stub"


@pytest.fixture(autouse=True)
def _stub_client(monkeypatch):
    StubClient.instances.clear()
    monkeypatch.setattr(worker_module, "ConfluenceClient", StubClient)


PAGES = [
    PageRef(id="1", title="Alpha", last_updated="2025-01-01T00:00:00Z",
            is_root=True, depth=0),
    PageRef(id="2", title="Beta", last_updated="2025-01-02T00:00:00Z", depth=1),
]


def _run(tmp_path, pages=None, **opts):
    results = {}
    worker = ExportWorker(
        pages if pages is not None else PAGES,
        credentials=Credentials(base_url="https://wiki.example.com"),
        space_key="DOCS",
        options=ExportOptions(output_dir=tmp_path, **opts),
    )
    logs: list[str] = []
    worker.log.connect(logs.append)
    worker.finished.connect(
        lambda s, f, k: results.update(success=s, failure=f, skipped=k)
    )
    worker.run()
    results["logs"] = logs
    return results


def test_markdown_files_are_written(tmp_path):
    result = _run(tmp_path)
    assert (result["success"], result["failure"], result["skipped"]) == (2, 0, 0)
    assert (tmp_path / "Alpha_1.md").is_file()
    assert (tmp_path / "Beta_2.md").is_file()


def test_front_matter_and_body_are_both_present(tmp_path):
    _run(tmp_path)
    text = (tmp_path / "Alpha_1.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert 'page_id: "1"' in text
    assert "# Alpha" in text


def test_front_matter_can_be_disabled(tmp_path):
    _run(tmp_path, front_matter=False)
    text = (tmp_path / "Alpha_1.md").read_text(encoding="utf-8")
    assert text.startswith("# Alpha")


def test_internal_link_points_at_the_sibling_file(tmp_path):
    _run(tmp_path)
    text = (tmp_path / "Alpha_1.md").read_text(encoding="utf-8")
    assert "[Beta](Beta_2.md)" in text


def test_index_is_written(tmp_path):
    _run(tmp_path)
    index = (tmp_path / "index.md").read_text(encoding="utf-8")
    assert "- [Alpha](Alpha_1.md)" in index
    assert "  - [Beta](Beta_2.md)" in index


def test_index_can_be_disabled(tmp_path):
    _run(tmp_path, write_index=False)
    assert not (tmp_path / "index.md").exists()


def test_state_records_every_page_timestamp(tmp_path):
    _run(tmp_path)
    state = json.loads((tmp_path / STATE_FILENAME).read_text(encoding="utf-8"))
    assert state["pages"] == {
        "1": "2025-01-01T00:00:00Z",
        "2": "2025-01-02T00:00:00Z",
    }
    assert state["space_key"] == "DOCS"
    assert state["last_sync"]


def test_second_run_skips_unchanged_pages(tmp_path):
    _run(tmp_path)
    StubClient.instances.clear()
    result = _run(tmp_path, skip_unchanged=True)
    assert (result["success"], result["skipped"]) == (0, 2)
    # Nothing was fetched the second time round.
    assert StubClient.instances[0].body_calls == []


def test_changed_page_is_re_exported(tmp_path):
    _run(tmp_path)
    changed = [PAGES[0], PageRef(id="2", title="Beta",
                                 last_updated="2025-06-01T00:00:00Z", depth=1)]
    StubClient.instances.clear()
    result = _run(tmp_path, pages=changed, skip_unchanged=True)
    assert (result["success"], result["skipped"]) == (1, 1)
    assert StubClient.instances[0].body_calls == ["2"]


def test_overwrite_off_fails_on_existing_file(tmp_path):
    _run(tmp_path)
    result = _run(tmp_path, overwrite=False)
    assert result["failure"] == 2
    assert any("already exists" in line for line in result["logs"])


def test_pdf_format_writes_pdf_only(tmp_path):
    result = _run(tmp_path, export_format="pdf")
    assert result["success"] == 2
    assert (tmp_path / "Alpha_1.pdf").read_bytes() == b"%PDF-1.4 stub"
    assert not (tmp_path / "Alpha_1.md").exists()
    # index.md is a Markdown artefact; a PDF-only run has nothing to index.
    assert not (tmp_path / "index.md").exists()


def test_both_format_writes_md_and_pdf(tmp_path):
    _run(tmp_path, export_format="both")
    assert (tmp_path / "Alpha_1.md").is_file()
    assert (tmp_path / "Alpha_1.pdf").is_file()


def test_mirrored_layout_creates_folders(tmp_path):
    pages = [
        PageRef(id="1", title="Alpha", is_root=True),
        PageRef(id="2", title="Beta", depth=1, ancestor_titles=("Alpha",)),
    ]
    _run(tmp_path, pages=pages, mirror_tree=True)
    assert (tmp_path / "Alpha_1.md").is_file()
    assert (tmp_path / "Alpha" / "Beta_2.md").is_file()
    # The link from Alpha into the subfolder is relative and points down.
    text = (tmp_path / "Alpha_1.md").read_text(encoding="utf-8")
    assert "(Alpha/Beta_2.md)" in text


def test_cancel_stops_before_the_next_page(tmp_path):
    worker = ExportWorker(
        PAGES,
        credentials=Credentials(base_url="https://wiki.example.com"),
        space_key="DOCS",
        options=ExportOptions(output_dir=tmp_path),
    )
    worker.page_done.connect(lambda *_: worker.cancel())
    finished = {}
    worker.finished.connect(
        lambda s, f, k: finished.update(success=s, failure=f, skipped=k)
    )
    worker.run()
    assert finished["success"] == 1
    assert not (tmp_path / "Beta_2.md").exists()


def test_empty_body_is_reported_as_a_failure(tmp_path):
    pages = [PageRef(id="3", title="Empty")]
    STORAGE["3"] = "   "
    try:
        result = _run(tmp_path, pages=pages)
    finally:
        del STORAGE["3"]
    assert result["failure"] == 1
    assert any("no storage-format body" in line for line in result["logs"])


def test_unknown_macros_are_reported_once(tmp_path):
    STORAGE["4"] = '<ac:structured-macro ac:name="gliffy"/>'
    pages = [PageRef(id="4", title="Diagram")]
    try:
        result = _run(tmp_path, pages=pages)
    finally:
        del STORAGE["4"]
    assert any("gliffy (1)" in line for line in result["logs"])
