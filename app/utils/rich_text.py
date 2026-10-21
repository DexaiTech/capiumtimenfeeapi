"""Rich text from the editor: sanitised HTML, and the plain text that goes with it.

The browser's editor sends HTML, which is never trusted. ``sanitize_html`` *rebuilds* it: only the
tags on the allow-list are written, without attributes (a link keeps a checked ``href`` and gets
``rel`` and ``target`` of our own), every piece of text is escaped again, and scripts, styles and
other active content are dropped together with what they contain. Nothing the client wrote is
copied through, so a payload has nowhere to hide. ``html_to_text`` turns the sanitised HTML into
the plain text kept beside it (search, excerpts, emails and the audit log read that).
"""

import re
import unicodedata
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit

_ALLOWED = frozenset(
    {
        "p",
        "br",
        "strong",
        "em",
        "u",
        "s",
        "code",
        "pre",
        "blockquote",
        "ul",
        "ol",
        "li",
        "h1",
        "h2",
        "h3",
        "a",
    }
)
# Tags the editors and browsers use for the same thing.
_ALIASES = {
    "b": "strong",
    "i": "em",
    "strike": "s",
    "del": "s",
    "div": "p",
    "h4": "h3",
    "h5": "h3",
    "h6": "h3",
}
# Active or invisible content: dropped with everything inside.
_DROPPED_WITH_CONTENT = frozenset(
    {
        "script",
        "style",
        "iframe",
        "object",
        "embed",
        "noscript",
        "template",
        "svg",
        "math",
        "head",
        "title",
        "textarea",
        "select",
        "button",
        "form",
        "frame",
        "frameset",
        "applet",
        "base",
        "link",
        "meta",
    }
)
_VOID = frozenset({"br"})
_BLOCKS = frozenset({"p", "pre", "blockquote", "ul", "ol", "li", "h1", "h2", "h3"})
_LINK_SCHEMES = frozenset({"http", "https", "mailto"})
MAX_DEPTH = 20
MAX_LINK_LENGTH = 2000
_INVISIBLE = re.compile("[\x00-\x20\x7f-\x9f\u200b-\u200f\u2028\u2029\ufeff]")


def _clean_text(text: str) -> str:
    """Text without control characters (tab and line breaks stay) and without NUL."""
    return "".join(
        c for c in text if c in "\n\t" or unicodedata.category(c) not in {"Cc", "Cs", "Cn"}
    )


def safe_link(href: str | None) -> str | None:
    """The address when it is an http, https or mailto link; None for anything else
    (``javascript:``, ``data:``, relative links, protocol-relative ones)."""
    if not href:
        return None
    candidate = _INVISIBLE.sub("", href)
    if not candidate or len(candidate) > MAX_LINK_LENGTH:
        return None
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    if parts.scheme.lower() not in _LINK_SCHEMES:
        return None
    if parts.scheme.lower() in {"http", "https"} and not parts.netloc:
        return None
    return candidate


class _Sanitiser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = _ALIASES.get(tag, tag)
        if self._skip_depth:
            if tag in _DROPPED_WITH_CONTENT:
                self._skip_depth += 1
            return
        if tag in _DROPPED_WITH_CONTENT:
            self._skip_depth = 1
            return
        if tag not in _ALLOWED:
            return
        if tag in _VOID:
            self.out.append("<br>")
            return
        if len(self.stack) >= MAX_DEPTH:
            return
        if tag == "a":
            href = safe_link(dict(attrs).get("href"))
            if href is None:
                # A link that is not safe is just its text.
                self.stack.append("a:text")
                return
            self.out.append(
                f'<a href="{escape(href, quote=True)}" target="_blank" '
                'rel="noopener noreferrer nofollow">'
            )
        else:
            self.out.append(f"<{tag}>")
        self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = _ALIASES.get(tag, tag)
        if tag in _VOID or (tag in _ALLOWED and tag not in _DROPPED_WITH_CONTENT):
            self.handle_starttag(tag, attrs)
            if tag not in _VOID:
                self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = _ALIASES.get(tag, tag)
        if self._skip_depth:
            if tag in _DROPPED_WITH_CONTENT:
                self._skip_depth -= 1
            return
        if tag not in _ALLOWED or tag in _VOID:
            return
        wanted = tag
        # Close up to the matching open tag; an end tag that was never opened is ignored.
        names = [("a:text" if s == "a:text" and tag == "a" else s) for s in self.stack]
        if wanted in names:
            while self.stack:
                top = self.stack.pop()
                if top == "a:text":
                    if wanted == "a":
                        break
                    continue
                self.out.append(f"</{top}>")
                if top == wanted:
                    break
        elif wanted == "a" and "a:text" in self.stack:
            while self.stack:
                top = self.stack.pop()
                if top == "a:text":
                    break
                self.out.append(f"</{top}>")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = _clean_text(data)
        if text:
            self.out.append(escape(text, quote=False))

    def finish(self) -> str:
        self.close()
        while self.stack:
            top = self.stack.pop()
            if top != "a:text":
                self.out.append(f"</{top}>")
        return "".join(self.out)


_EMPTY_PARAGRAPH = re.compile(r"<p>(?:\s|<br>)*</p>")


def sanitize_html(html: str) -> str:
    """The allow-listed rebuild of ``html``; empty paragraphs at either end are removed and a
    document with no text in it (and no list or quote) becomes ''."""
    parser = _Sanitiser()
    parser.feed(html)
    clean = parser.finish().strip()
    # TipTap writes <p></p> for a blank line: those at the start and the end are noise.
    previous = None
    while previous != clean:
        previous = clean
        clean = re.sub(r"^(?:<p>(?:\s|<br>)*</p>)+", "", clean).strip()
        clean = re.sub(r"(?:<p>(?:\s|<br>)*</p>)+$", "", clean).strip()
    return clean if html_to_text(clean).strip() else ""


class _Plain(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.lists: list[list[int]] = []  # [0] for a bullet list, [n] for an ordered one
        # Just after a "- ", "1. " or "> " marker: the paragraph inside stays on that line.
        self.marker_open = False

    def _break(self, count: int = 1) -> None:
        if self.marker_open:
            return
        text = "".join(self.parts)
        if not text:
            return
        have = len(text) - len(text.rstrip("\n"))
        self.parts.append("\n" * max(0, count - have))

    def _marker(self, text: str) -> None:
        self.parts.append(text)
        self.marker_open = True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.parts.append("\n")
        elif tag in {"ul", "ol"}:
            self._break(1 if self.lists else 2)
            self.lists.append([0 if tag == "ul" else 1])
        elif tag == "li":
            self.marker_open = False
            self._break()
            if self.lists and self.lists[-1][0]:
                self._marker(f"{self.lists[-1][0]}. ")
                self.lists[-1][0] += 1
            else:
                self._marker("- ")
        elif tag == "blockquote":
            self._break(2)
            self._marker("> ")
        elif tag in _BLOCKS:
            self._break(2)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"ul", "ol"} and self.lists:
            self.lists.pop()
            self._break(1 if self.lists else 2)
        elif tag in _BLOCKS:
            self.marker_open = False
            # A paragraph inside a list item is part of the item.
            self._break(1 if tag == "li" or (tag == "p" and self.lists) else 2)

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.marker_open = False
        self.parts.append(data)


def html_to_text(html: str) -> str:
    """The plain text of sanitised HTML: blocks on their own lines, lists as ``- `` or ``1. ``."""
    parser = _Plain()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
