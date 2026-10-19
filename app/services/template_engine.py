"""Plain ``{{ field }}`` substitution for the templates a practice edits.

Nothing is evaluated. A template is text with placeholders, a placeholder is the name of a
merge field, and rendering is one regular-expression substitution. There are no attributes,
calls, filters or statements to abuse, so a template can never run code or reach anything that
is not in the values it is given. Values are HTML-escaped in HTML and left as they are in
plain text (and in the subject, where line breaks are removed).
"""

import html
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass

_PLACEHOLDER = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_FIELD = re.compile(r"\s*([a-z][a-z0-9_]{0,39})\s*")
_STATEMENT_DELIMITERS = ("{%", "%}", "{#", "#}")
# Markup that can run script or leave the message when the practice's HTML is mailed out.
_UNSAFE_HTML = re.compile(
    r"<\s*/?\s*(?:script|iframe|object|embed|form|base|meta|link)\b"
    r"|javascript\s*:"
    r"|<[^>]*\son[a-z]+\s*=",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Problem:
    code: str
    message: str


def fields_used(source: str) -> set[str]:
    """The merge fields the source names (placeholders that are plain field names)."""
    found = (_FIELD.fullmatch(match.group(1)) for match in _PLACEHOLDER.finditer(source))
    return {field.group(1) for field in found if field}


def check(source: str, allowed: Collection[str]) -> list[Problem]:
    """What is wrong with ``source`` as a template: unknown fields, anything but plain fields,
    statements and comments, stray braces."""
    problems: list[Problem] = []
    unknown: set[str] = set()
    for match in _PLACEHOLDER.finditer(source):
        inner = match.group(1)
        field = _FIELD.fullmatch(inner)
        if field is None:
            shown = " ".join(inner.split())[:40]
            problems.append(
                Problem(
                    "invalid_placeholder",
                    f"{{{{ {shown} }}}} is not a merge field. Placeholders are plain field "
                    "names such as {{ recipient_name }}: no attributes, filters, calls or logic.",
                )
            )
        elif field.group(1) not in allowed and field.group(1) not in unknown:
            unknown.add(field.group(1))
            problems.append(
                Problem(
                    "unknown_merge_field",
                    f"{{{{ {field.group(1)} }}}} is not available in this template. "
                    f"Available fields: {', '.join(sorted(allowed))}.",
                )
            )
    rest = _PLACEHOLDER.sub("", source)
    if any(delimiter in rest for delimiter in _STATEMENT_DELIMITERS):
        problems.append(
            Problem(
                "template_logic",
                "Template statements and comments ({% … %}, {# … #}) are not allowed: "
                "only {{ field }} placeholders.",
            )
        )
    if "{{" in rest or "}}" in rest:
        problems.append(
            Problem("unbalanced_placeholder", "A {{ or }} is not part of a complete placeholder.")
        )
    return problems


def check_html(source: str) -> list[Problem]:
    if _UNSAFE_HTML.search(source):
        return [
            Problem(
                "unsafe_html",
                "Scripts, frames, forms, event handlers and javascript: links are not allowed "
                "in an email.",
            )
        ]
    return []


def render(source: str, values: Mapping[str, str], *, escape: bool) -> str:
    """Substitute the placeholders. ``source`` has passed :func:`check`; anything that is not a
    plain field name renders as nothing rather than being interpreted."""

    def replace(match: re.Match[str]) -> str:
        field = _FIELD.fullmatch(match.group(1))
        value = values.get(field.group(1), "") if field else ""
        return html.escape(value, quote=True) if escape else value

    return _PLACEHOLDER.sub(replace, source)


def one_line(value: str) -> str:
    """A header value: line breaks and runs of spaces collapsed."""
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())
