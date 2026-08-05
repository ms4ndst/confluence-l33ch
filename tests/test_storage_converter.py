"""Tests for the Confluence storage-format → Markdown converter."""

from app.storage_converter import convert_storage


def md(xhtml: str, **kwargs) -> str:
    return convert_storage(xhtml, **kwargs).markdown


def test_headings_and_paragraphs():
    out = md("<h1>Title</h1><p>Body text.</p><h2>Sub</h2><p>More.</p>")
    assert out == "# Title\n\nBody text.\n\n## Sub\n\nMore."


def test_inline_formatting():
    out = md("<p>A <strong>bold</strong> and <em>italic</em> and <code>x=1</code>.</p>")
    assert out == "A **bold** and *italic* and `x=1`."


def test_unordered_list():
    out = md("<ul><li>one</li><li>two</li></ul>")
    assert out == "- one\n- two"


def test_ordered_list_numbering():
    out = md("<ol><li>first</li><li>second</li><li>third</li></ol>")
    assert out == "1. first\n2. second\n3. third"


def test_nested_list_is_indented():
    out = md("<ul><li>outer<ul><li>inner</li></ul></li></ul>")
    assert out == "- outer\n  - inner"


def test_table_with_header():
    out = md(
        "<table><tbody>"
        "<tr><th>Name</th><th>Value</th></tr>"
        "<tr><td>a</td><td>1</td></tr>"
        "</tbody></table>"
    )
    assert out == (
        "| Name | Value |\n"
        "|---|---|\n"
        "| a | 1 |"
    )


def test_headerless_table_gets_empty_header_row():
    out = md("<table><tbody><tr><td>a</td><td>b</td></tr></tbody></table>")
    lines = out.split("\n")
    assert lines[1] == "|---|---|"
    assert lines[2] == "| a | b |"


def test_table_cell_pipes_are_escaped():
    out = md("<table><tbody><tr><td>a|b</td></tr></tbody></table>")
    assert "a\\|b" in out


def test_code_macro_becomes_fenced_block_with_language():
    out = md(
        '<ac:structured-macro ac:name="code">'
        '<ac:parameter ac:name="language">python</ac:parameter>'
        "<ac:plain-text-body><![CDATA[print('hi')]]></ac:plain-text-body>"
        "</ac:structured-macro>"
    )
    assert out == "```python\nprint('hi')\n```"


def test_noformat_macro_has_no_language():
    out = md(
        '<ac:structured-macro ac:name="noformat">'
        "<ac:plain-text-body><![CDATA[raw text]]></ac:plain-text-body>"
        "</ac:structured-macro>"
    )
    assert out == "```\nraw text\n```"


def test_info_macro_becomes_blockquote():
    out = md(
        '<ac:structured-macro ac:name="info">'
        "<ac:rich-text-body><p>Heads up.</p></ac:rich-text-body>"
        "</ac:structured-macro>"
    )
    assert out == "> **Info**\n>\n> Heads up."


def test_admonition_title_parameter_wins():
    out = md(
        '<ac:structured-macro ac:name="note">'
        '<ac:parameter ac:name="title">Careful</ac:parameter>'
        "<ac:rich-text-body><p>Text.</p></ac:rich-text-body>"
        "</ac:structured-macro>"
    )
    assert out.startswith("> **Careful**")


def test_toc_macro_is_dropped():
    result = convert_storage(
        '<p>Before</p><ac:structured-macro ac:name="toc">'
        '<ac:parameter ac:name="maxLevel">3</ac:parameter>'
        "</ac:structured-macro><p>After</p>"
    )
    assert result.markdown == "Before\n\nAfter"
    assert "toc" in result.dropped_macros


def test_unknown_macro_keeps_body_and_is_reported():
    result = convert_storage(
        '<ac:structured-macro ac:name="mystery">'
        "<ac:rich-text-body><p>Kept.</p></ac:rich-text-body>"
        "</ac:structured-macro>"
    )
    assert "Kept." in result.markdown
    assert result.unknown_macros == {"mystery"}


def test_external_link():
    out = md('<p><a href="https://example.com">Example</a></p>')
    assert out == "[Example](https://example.com)"


def test_page_link_uses_resolver():
    out = md(
        "<p><ac:link><ri:page ri:content-title=\"Other Page\" "
        'ri:space-key="DOCS"/>'
        "<ac:plain-text-link-body><![CDATA[see this]]></ac:plain-text-link-body>"
        "</ac:link></p>",
        link_resolver=lambda title, space: f"{space}/{title}.md",
    )
    assert out == "[see this](DOCS/Other Page.md)"


def test_page_link_without_body_falls_back_to_title():
    out = md(
        '<p><ac:link><ri:page ri:content-title="Target"/></ac:link></p>',
        link_resolver=lambda title, space: "target.md",
    )
    assert out == "[Target](target.md)"


def test_attachment_image_uses_resolver_and_is_reported():
    result = convert_storage(
        '<p><ac:image><ri:attachment ri:filename="diagram.png"/></ac:image></p>',
        attachment_resolver=lambda name: f"https://host/download/{name}",
    )
    assert result.markdown == "![diagram.png](https://host/download/diagram.png)"
    assert result.attachments == {"diagram.png"}


def test_image_without_resolver_degrades_to_placeholder():
    out = md('<ac:image><ri:attachment ri:filename="pic.png"/></ac:image>')
    assert out == "`[image: pic.png]`"


def test_task_list_checkboxes():
    out = md(
        "<ac:task-list>"
        "<ac:task><ac:task-status>complete</ac:task-status>"
        "<ac:task-body>done thing</ac:task-body></ac:task>"
        "<ac:task><ac:task-status>incomplete</ac:task-status>"
        "<ac:task-body>todo thing</ac:task-body></ac:task>"
        "</ac:task-list>"
    )
    assert out == "- [x] done thing\n- [ ] todo thing"


def test_blockquote():
    out = md("<blockquote><p>Quoted.</p></blockquote>")
    assert out == "> Quoted."


def test_status_macro_renders_inline_code():
    out = md(
        '<p>State: <ac:structured-macro ac:name="status">'
        '<ac:parameter ac:name="title">DONE</ac:parameter>'
        "</ac:structured-macro></p>"
    )
    assert out == "State: `DONE`"


def test_hard_line_break_is_preserved():
    out = md("<p>line one<br/>line two</p>")
    assert out == "line one  \nline two"


def test_entities_are_decoded():
    out = md("<p>a &amp; b &lt; c</p>")
    assert out == "a & b < c"


def test_blank_input_is_empty_string():
    assert md("") == ""


def test_malformed_markup_does_not_raise():
    out = md("<p>unclosed <strong>bold <ul><li>item")
    assert "item" in out


def test_no_more_than_one_blank_line():
    out = md("<p>a</p><p></p><p></p><p>b</p>")
    assert "\n\n\n" not in out
