"""Tests for scope resolution, against a stubbed REST client."""

from datetime import datetime

import pytest

from app import discovery as discovery_module
from app.confluence_client import ConfluenceError, Credentials, PageRef, WhoAmI
from app.discovery import DiscoveryRequest, DiscoveryWorker


class StubClient:
    whoami_result = WhoAmI(True, "Tester", "Authenticated as Tester.")
    root_page = {"title": "Root", "history": {"lastUpdated": {"when": "2025-01-01T00:00:00Z"}}}
    resolve_result = "100"
    resolve_error: Exception | None = None
    descendant_pages: list[PageRef] = []
    space_result: list[PageRef] = []

    def __init__(self, credentials, timeout=30):
        self.credentials = credentials
        self.space_calls: list[tuple] = []

    def whoami(self):
        return self.whoami_result

    def resolve_page_id(self, space_key, page_id="", title=""):
        if self.resolve_error is not None:
            raise self.resolve_error
        return self.resolve_result

    def get_page(self, page_id, with_body=True):
        return self.root_page

    def descendants(self, root_id, should_cancel=None, on_batch=None):
        return list(self.descendant_pages)

    def space_pages(self, space_key, modified_since=None, should_cancel=None,
                    on_batch=None):
        self.space_calls.append((space_key, modified_since))
        return list(self.space_result)


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    monkeypatch.setattr(discovery_module, "ConfluenceClient", StubClient)
    StubClient.whoami_result = WhoAmI(True, "Tester", "Authenticated as Tester.")
    StubClient.resolve_error = None
    StubClient.resolve_result = "100"
    StubClient.descendant_pages = []
    StubClient.space_result = []


def _run(request):
    worker = DiscoveryWorker(request)
    captured = {}
    logs: list[str] = []
    worker.log.connect(logs.append)
    worker.finished.connect(lambda pages, err: captured.update(pages=pages, error=err))
    worker.run()
    captured["logs"] = logs
    return captured


def _request(**kwargs):
    base = dict(credentials=Credentials(pat="token"), space_key="DOCS")
    base.update(kwargs)
    return DiscoveryRequest(**base)


def test_request_detects_subtree_mode():
    assert _request(top_page_id="5").is_subtree
    assert _request(top_page_title="Some Page").is_subtree
    assert not _request().is_subtree


def test_subtree_puts_the_root_first_and_flags_it():
    StubClient.descendant_pages = [PageRef(id="101", title="Child", depth=1)]
    result = _run(_request(top_page_id="100"))
    assert result["error"] == ""
    pages = result["pages"]
    assert [p.id for p in pages] == ["100", "101"]
    assert pages[0].is_root and pages[0].title == "Root"
    assert pages[0].last_updated == "2025-01-01T00:00:00Z"


def test_unresolvable_root_is_an_error():
    StubClient.resolve_result = ""
    result = _run(_request(top_page_title="Nope"))
    assert result["pages"] == []
    assert "Could not resolve the top page" in result["error"]


def test_client_error_is_forwarded_verbatim():
    StubClient.resolve_error = ConfluenceError("HTTP 404 from …")
    result = _run(_request(top_page_id="1"))
    assert result["error"] == "HTTP 404 from …"


def test_unexpected_exception_is_labelled_with_its_type():
    StubClient.resolve_error = ValueError("boom")
    result = _run(_request(top_page_id="1"))
    assert result["error"] == "ValueError: boom"


def test_space_scan_requires_a_space_key():
    result = _run(_request(space_key=""))
    assert "space key is required" in result["error"]


def test_space_scan_passes_the_modified_since_filter():
    since = datetime(2025, 5, 1)
    StubClient.space_result = [PageRef(id="7", title="Page")]
    result = _run(_request(modified_since=since))
    assert [p.id for p in result["pages"]] == ["7"]
    assert any("modified since" in line for line in result["logs"])


def test_anonymous_session_warns_but_continues():
    StubClient.whoami_result = WhoAmI(False, "", "Treated as Anonymous.")
    StubClient.space_result = [PageRef(id="7", title="Page")]
    result = _run(_request())
    assert result["error"] == ""
    assert any("Continuing unauthenticated" in line for line in result["logs"])
