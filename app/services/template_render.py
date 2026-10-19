"""Turning a template (a practice's override or a built-in plain one) into a complete email."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.services import template_engine

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "email"

# The layout is a trusted file of this repository; the practice's text only fills ``content``.
_LAYOUT = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=True,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


@dataclass(frozen=True, slots=True)
class TemplateContent:
    subject: str
    html: str
    text: str


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    subject: str
    html: str
    text: str


def render_email(
    content: TemplateContent, values: Mapping[str, str], *, brand: str, app_name: str
) -> RenderedEmail:
    """Render with ``values``. The HTML is the message placed in the shared layout, with every
    value escaped; the subject is one line; the text body is plain text."""
    subject = template_engine.one_line(
        template_engine.render(content.subject, values, escape=False)
    )
    message = template_engine.render(content.html, values, escape=True)
    html = _LAYOUT.get_template("_message.html").render(
        subject=subject, brand=brand, app_name=app_name, content=message
    )
    text = template_engine.render(content.text, values, escape=False)
    return RenderedEmail(subject=subject, html=html, text=text)
