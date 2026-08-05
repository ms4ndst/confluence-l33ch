"""Tests for parsing a pasted cURL command / Cookie header."""

from app.cookie_import import cookie_names, parse_cookie_input, probe_url


CURL_BASH = (
    "curl 'https://confluence.example.com/rest/api/user/current' \\\n"
    "  -H 'Accept: application/json' \\\n"
    "  -H 'Cookie: JSESSIONID=ABC123; crowd.token_key=XYZ; "
    "atlassian.xsrf.token=t1' \\\n"
    "  -H 'User-Agent: Mozilla/5.0' \\\n"
    "  --compressed"
)

CURL_CMD = (
    'curl "https://confluence.example.com/rest/api/space/DOCS/content" ^\n'
    '  -H "Accept: application/json" ^\n'
    '  -H "Cookie: JSESSIONID=ABC123; crowd.token_key=XYZ"'
)


def test_extracts_cookie_from_bash_curl():
    result = parse_cookie_input(CURL_BASH)
    assert result.ok
    assert result.cookie_header == (
        "JSESSIONID=ABC123; crowd.token_key=XYZ; atlassian.xsrf.token=t1"
    )


def test_extracts_base_url_from_curl():
    assert parse_cookie_input(CURL_BASH).base_url == "https://confluence.example.com"


def test_handles_cmd_style_quotes_and_carets():
    result = parse_cookie_input(CURL_CMD)
    assert result.cookie_header == "JSESSIONID=ABC123; crowd.token_key=XYZ"
    assert result.base_url == "https://confluence.example.com"


def test_ignores_non_cookie_headers():
    result = parse_cookie_input(CURL_BASH)
    assert "Mozilla" not in result.cookie_header
    assert "application/json" not in result.cookie_header


def test_accepts_a_bare_cookie_header_line():
    result = parse_cookie_input("Cookie: JSESSIONID=ABC123; foo=bar")
    assert result.cookie_header == "JSESSIONID=ABC123; foo=bar"


def test_accepts_just_the_value():
    result = parse_cookie_input("JSESSIONID=ABC123; foo=bar")
    assert result.cookie_header == "JSESSIONID=ABC123; foo=bar"


def test_header_prefix_is_case_insensitive():
    assert parse_cookie_input("COOKIE: a=1").cookie_header == "a=1"


def test_curl_cookie_flag_is_supported():
    result = parse_cookie_input("curl https://x.example.com -b 'JSESSIONID=Q1'")
    assert result.cookie_header == "JSESSIONID=Q1"


def test_trailing_semicolon_is_trimmed():
    assert parse_cookie_input("a=1; b=2;").cookie_header == "a=1; b=2"


def test_empty_and_whitespace_input():
    assert not parse_cookie_input("").ok
    assert not parse_cookie_input("   \n  ").ok


def test_text_without_name_value_pairs_is_rejected():
    assert not parse_cookie_input("just some prose without pairs").ok
    assert not parse_cookie_input("Cookie: notpairs").ok


def test_context_path_is_preserved_but_rest_suffix_dropped():
    result = parse_cookie_input(
        "curl 'https://host.example.com/confluence/rest/api/user/current' "
        "-H 'Cookie: JSESSIONID=1'"
    )
    assert result.base_url == "https://host.example.com/confluence"


def test_display_url_is_reduced_to_the_origin():
    result = parse_cookie_input(
        "curl 'https://host.example.com/display/DOCS/Some+Page' "
        "-H 'Cookie: JSESSIONID=1'"
    )
    assert result.base_url == "https://host.example.com"


def test_wiki_rest_path_is_dropped():
    result = parse_cookie_input(
        "curl 'https://host.example.com/wiki/rest/api/content' "
        "-H 'Cookie: JSESSIONID=1'"
    )
    assert result.base_url == "https://host.example.com"


def test_multiline_header_block_paste():
    pasted = (
        "Accept: application/json\n"
        "Accept-Language: en-GB\n"
        "Cookie: JSESSIONID=ABC; crowd.token_key=DEF\n"
        "User-Agent: Mozilla/5.0\n"
    )
    assert parse_cookie_input(pasted).cookie_header == (
        "JSESSIONID=ABC; crowd.token_key=DEF"
    )


def test_cookie_names_lists_names_only():
    assert cookie_names("JSESSIONID=A; crowd.token_key=B; x=1") == [
        "JSESSIONID", "crowd.token_key", "x",
    ]


def test_cookie_names_of_empty_string():
    assert cookie_names("") == []


def test_no_base_url_when_the_paste_has_no_url():
    result = parse_cookie_input("Cookie: JSESSIONID=1")
    assert result.ok and result.base_url == ""


def test_user_agent_extracted_from_curl():
    assert parse_cookie_input(CURL_BASH).user_agent == "Mozilla/5.0"


def test_user_agent_extracted_from_raw_header_block():
    pasted = (
        "Cookie: JSESSIONID=ABC\n"
        'Sec-Ch-Ua: "Microsoft Edge";v="151"\n'
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) Edg/151.0\n"
    )
    result = parse_cookie_input(pasted)
    assert result.user_agent == (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Edg/151.0"
    )
    assert result.cookie_header == "JSESSIONID=ABC"


def test_user_agent_absent_is_empty_not_an_error():
    result = parse_cookie_input("Cookie: JSESSIONID=1")
    assert result.ok and result.user_agent == ""


def test_probe_url_is_built_from_the_connection_fields():
    assert probe_url("https://confluence.example.com", "/rest/api") == (
        "https://confluence.example.com/rest/api/user/current"
    )


def test_probe_url_tolerates_slashes_and_context_paths():
    assert probe_url("https://host.example.com/confluence/", "rest/api/") == (
        "https://host.example.com/confluence/rest/api/user/current"
    )


def test_probe_url_placeholder_when_base_url_is_empty():
    assert probe_url("", "/rest/api") == (
        "https://<your-confluence-host>/rest/api/user/current"
    )


def test_probe_url_defaults_the_api_path():
    assert probe_url("https://h.example.com", "") == (
        "https://h.example.com/rest/api/user/current"
    )
