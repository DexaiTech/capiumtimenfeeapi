"""Rich text: the sanitiser rebuilds the editor's HTML from an allow-list, and the plain text
kept beside it reads well."""

import pytest

from app.utils.rich_text import html_to_text, safe_link, sanitize_html


def test_the_formatting_the_editor_makes_is_kept():
    html = (
        "<h2>Title</h2><p>Some <strong>bold</strong>, <em>italic</em>, <u>under</u> and "
        "<s>struck</s> and <code>code</code>.</p><ul><li><p>one</p></li><li><p>two</p></li></ul>"
        "<ol><li>a</li></ol><blockquote><p>quote</p></blockquote><pre><code>x = 1</code></pre>"
    )
    assert sanitize_html(html) == html


def test_a_safe_link_gets_our_attributes_and_nothing_else():
    clean = sanitize_html(
        '<p><a href="https://example.com/a?b=1&c=2" onclick="x()" style="x">go</a></p>'
    )
    assert clean == (
        '<p><a href="https://example.com/a?b=1&amp;c=2" target="_blank" '
        'rel="noopener noreferrer nofollow">go</a></p>'
    )


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        " javascript:alert(1)",
        "java\tscript:alert(1)",
        "java\nscript:alert(1)",
        "\x01javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:x",
        "//evil.example/x",
        "/relative/path",
        "https://",
        "",
    ],
)
def test_an_unsafe_link_is_only_its_text(href):
    clean = sanitize_html(f'<p><a href="{href}">click</a></p>')
    assert clean == "<p>click</p>"
    assert safe_link(href) is None


def test_mailto_and_http_links_are_allowed():
    assert safe_link("mailto:a@example.com") == "mailto:a@example.com"
    assert safe_link("http://example.com") == "http://example.com"


@pytest.mark.parametrize(
    "payload",
    [
        "<script>alert(1)</script>",
        "<SCRIPT SRC=//evil.example/x.js></SCRIPT>",
        "<img src=x onerror=alert(1)>",
        "<svg onload=alert(1)><circle/></svg>",
        "<iframe src='javascript:alert(1)'></iframe>",
        "<style>body{display:none}</style>",
        "<object data='x'></object><embed src='x'>",
        "<math><mi xlink:href='javascript:alert(1)'>x</mi></math>",
        "<form action='x'><input name=a></form>",
        "<p onmouseover='alert(1)'>hover</p>",
        "<a href='x' onclick='alert(1)'>x</a>",
        "<b style='background:url(javascript:alert(1))'>x</b>",
        "<<script>script>alert(1)<</script>/script>",
        "<img/src/onerror=alert(1)>",
        "<noscript><p title='</noscript><img src=x onerror=alert(1)>'>",
    ],
)
def test_active_content_never_survives(payload):
    clean = sanitize_html(f"<p>ok</p>{payload}")
    lowered = clean.lower()
    for needle in (
        "<script",
        "onerror",
        "onload",
        "onclick",
        "onmouseover",
        "javascript:",
        "<iframe",
        "<svg",
        "<img",
        "<style",
        "<object",
        "<embed",
        "<form",
        "<input",
    ):
        assert needle not in lowered, (payload, clean)
    assert clean.startswith("<p>ok</p>")


def test_text_is_escaped_not_dropped():
    assert sanitize_html("<p>1 &lt; 2 &amp; 3 &gt; 2 <b>&lt;script&gt;</b></p>") == (
        "<p>1 &lt; 2 &amp; 3 &gt; 2 <strong>&lt;script&gt;</strong></p>"
    )


def test_tags_are_balanced_and_stray_end_tags_ignored():
    assert sanitize_html("<p><strong>open</p></strong></em>") == "<p><strong>open</strong></p>"
    assert sanitize_html("<ul><li>x") == "<ul><li>x</li></ul>"


def test_aliases_become_the_standard_tags():
    assert sanitize_html("<div><b>x</b><i>y</i><strike>z</strike></div>") == (
        "<p><strong>x</strong><em>y</em><s>z</s></p>"
    )


def test_control_characters_are_removed():
    assert sanitize_html("<p>a\x00b\x07c</p>") == "<p>abc</p>"


def test_nothing_but_blank_paragraphs_is_empty():
    assert sanitize_html("<p></p><p><br></p>") == ""
    assert sanitize_html("   ") == ""
    assert sanitize_html("<p></p><p>x</p><p></p>") == "<p>x</p>"


def test_very_deep_nesting_is_cut_off():
    html = "<blockquote>" * 200 + "deep" + "</blockquote>" * 200
    clean = sanitize_html(html)
    assert clean.count("<blockquote>") == 20 and "deep" in clean


def test_plain_text_keeps_paragraphs_lists_and_breaks():
    html = (
        "<h1>Steps</h1><p>First line<br>second line</p><ul><li><p>one</p></li><li>two</li></ul>"
        "<ol><li>a</li><li>b</li></ol><blockquote><p>said</p></blockquote>"
    )
    assert html_to_text(html) == (
        "Steps\n\nFirst line\nsecond line\n\n- one\n- two\n\n1. a\n2. b\n\n> said"
    )


def test_plain_text_unescapes_entities():
    assert html_to_text("<p>1 &lt; 2 &amp; <strong>3</strong></p>") == "1 < 2 & 3"
