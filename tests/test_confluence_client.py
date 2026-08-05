"""Tests for the client's pure helpers — no network involved."""

from app.confluence_client import Credentials, _page_ref


def _item(page_id="9", title="Page", ancestors=None, when="2025-01-01T00:00:00Z"):
    return {
        "id": page_id,
        "title": title,
        "ancestors": ancestors or [],
        "history": {"lastUpdated": {"when": when}},
    }


def test_api_root_joins_without_double_slashes():
    creds = Credentials(base_url="https://wiki.example.com/", api_path="/rest/api/")
    assert creds.api_root == "https://wiki.example.com/rest/api"


def test_has_auth_needs_a_pat_or_a_cookie():
    assert not Credentials().has_auth
    assert Credentials(pat="t").has_auth
    assert Credentials(cookie="JSESSIONID=x").has_auth


def test_bearer_header_by_default():
    client_headers = _headers(Credentials(pat="tok"))
    assert client_headers["Authorization"] == "Bearer tok"


def test_basic_header_encodes_username_and_token():
    creds = Credentials(pat="tok", auth_mode="basic", username="me")
    # base64("me:tok")
    assert _headers(creds)["Authorization"] == "Basic bWU6dG9r"


def test_basic_falls_back_to_bearer_without_a_username():
    creds = Credentials(pat="tok", auth_mode="basic")
    assert _headers(creds)["Authorization"] == "Bearer tok"


def test_cookie_is_sent_as_a_header():
    assert _headers(Credentials(cookie="a=1"))["Cookie"] == "a=1"


def test_no_authorization_header_without_a_pat():
    assert "Authorization" not in _headers(Credentials(cookie="a=1"))


def test_default_user_agent_is_used_when_none_was_captured():
    assert "ConfluenceL33ch" in _headers(Credentials(pat="t"))["User-Agent"]


def test_captured_user_agent_overrides_the_default():
    creds = Credentials(cookie="a=1", user_agent="Mozilla/5.0 (Chromium test)")
    assert _headers(creds)["User-Agent"] == "Mozilla/5.0 (Chromium test)"


def _headers(creds):
    from app.confluence_client import ConfluenceClient

    return ConfluenceClient(creds)._headers()


def test_page_ref_reads_title_and_timestamp():
    ref = _page_ref(_item(title="Alpha"))
    assert (ref.id, ref.title) == ("9", "Alpha")
    assert ref.last_updated == "2025-01-01T00:00:00Z"
    assert ref.last_updated_dt is not None


def test_page_ref_title_falls_back_to_the_id():
    ref = _page_ref({"id": "42"})
    assert ref.title == "page-42"


def test_depth_and_ancestors_are_relative_to_the_root():
    item = _item(ancestors=[
        {"id": "1", "title": "Space Home"},
        {"id": "2", "title": "Root"},
        {"id": "3", "title": "Section"},
    ])
    ref = _page_ref(item, root_id="2")
    assert ref.depth == 2
    assert ref.ancestor_titles == ("Section",)


def test_direct_child_of_the_root_has_no_relative_ancestors():
    item = _item(ancestors=[{"id": "2", "title": "Root"}])
    ref = _page_ref(item, root_id="2")
    assert ref.depth == 1
    assert ref.ancestor_titles == ()


def test_full_ancestry_is_used_when_the_root_is_absent():
    item = _item(ancestors=[{"id": "1", "title": "A"}, {"id": "2", "title": "B"}])
    ref = _page_ref(item, root_id="999")
    assert ref.depth == 2
    assert ref.ancestor_titles == ("A", "B")


def test_unparseable_timestamp_yields_none():
    ref = _page_ref(_item(when="not a date"))
    assert ref.last_updated_dt is None


def test_pdf_candidates_cover_rest_and_ui_endpoints():
    from app.confluence_client import ConfluenceClient

    urls = ConfluenceClient(
        Credentials(base_url="https://wiki.example.com")
    ).pdf_url_candidates("55")
    assert urls[0] == "https://wiki.example.com/rest/api/content/55/export/pdf"
    assert any("flyingpdf" in u for u in urls)
    assert all("55" in u for u in urls)
