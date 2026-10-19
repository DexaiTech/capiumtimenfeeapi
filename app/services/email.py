"""Transactional email: Jinja2 templates rendered to HTML and text, handed to the gateway.

Only the worker calls this. Request handlers enqueue a ``send_email`` job instead. The gateway
(``app.services.messaging``) is SMTP or the recording mock, by ``MESSAGING_BACKEND``.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from app.core.config import Settings
from app.core.logging import get_logger
from app.services.messaging import (
    Attachment,
    MessageGateway,
    OutboundEmail,
    PermanentSendError,
    build_gateway,
)
from app.services.template_catalogue import TEMPLATES as PRACTICE_TEMPLATES
from app.services.template_engine import one_line
from app.services.template_render import TEMPLATE_DIR, TemplateContent, render_email

log = get_logger(__name__)

__all__ = ["TEMPLATES", "Attachment", "EmailService", "PermanentEmailError", "Template"]

# The name the worker and the invoice delivery catch.
PermanentEmailError = PermanentSendError


@dataclass(frozen=True, slots=True)
class Template:
    subject: str
    link_path: str | None = None  # frontend path the one-time token is appended to
    token_in_path: bool = False  # ``<path>/<token>`` instead of ``<path>?token=<token>``


TEMPLATES: dict[str, Template] = {
    "verification": Template("Verify your email address", "/verify-email"),
    "password_reset": Template("Reset your password", "/reset-password"),
    "welcome": Template("Welcome to {app_name}"),
    "account_created": Template("Your {app_name} account is ready", "/reset-password"),
    "staff_invitation": Template(
        "You are invited to {organization_name} on {app_name}", "/reset-password"
    ),
    "account_exists": Template("You already have an account"),
    "password_changed": Template("Your password was changed"),
    "invoice": Template("Invoice {invoice_number} from {practice_name}"),
}


def _header_safe(value: str) -> str:
    """Strip line breaks so user-supplied text cannot inject headers."""
    return one_line(value)


class EmailService:
    def __init__(self, settings: Settings, gateway: MessageGateway | None = None) -> None:
        self.settings = settings
        self.gateway = gateway or build_gateway(settings)
        self._env = Environment(
            loader=FileSystemLoader(TEMPLATE_DIR),
            autoescape=select_autoescape(enabled_extensions=("html",), default_for_string=True),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def render(self, template: str, context: dict[str, Any]) -> tuple[str, str, str]:
        """Return ``(subject, html, text)``."""
        spec = TEMPLATES[template]
        ctx = {
            "app_name": self.settings.app_name,
            "frontend_url": self.settings.frontend_url,
            **context,
        }
        # Only the subject's own placeholders are filled; a missing one is a KeyError.
        subject = _header_safe(spec.subject.format_map(ctx))
        html = self._env.get_template(f"{template}.html").render(subject=subject, **ctx)
        text = self._env.get_template(f"{template}.txt").render(subject=subject, **ctx)
        return subject, html, text

    def build_link(self, template: str, token: str | None) -> str | None:
        """Links always start from FRONTEND_URL, never from request data."""
        spec = TEMPLATES[template]
        if spec.link_path is None or token is None:
            return None
        separator = "/" if spec.token_in_path else "?token="
        return f"{self.settings.frontend_url}{spec.link_path}{separator}{token}"

    async def send_template(
        self,
        template: str,
        *,
        to: str,
        name: str,
        token: str | None = None,
        attachments: Sequence[Attachment] = (),
        override: TemplateContent | None = None,
        organization_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
        **extra: Any,
    ) -> None:
        """Render and send. With ``override`` (a practice's version of ``template``) the practice's
        text is used instead of the built-in design."""
        if template not in TEMPLATES:
            raise PermanentEmailError(f"unknown template {template!r}")
        link = self.build_link(template, token)
        context = {"name": name, "link": link, **extra}
        if override is None:
            subject, html, text = self.render(template, context)
        else:
            values = self.practice_values(template, context)
            rendered = render_email(
                override,
                values,
                brand=values.get("practice_name") or self.settings.app_name,
                app_name=self.settings.app_name,
            )
            subject, html, text = rendered.subject, rendered.html, rendered.text
        await self.send(
            to=to,
            subject=subject,
            html=html,
            text=text,
            template=template,
            attachments=attachments,
            organization_id=organization_id,
            user_id=user_id,
            # The one-time link is not kept by a gateway that records messages.
            secrets=(link,) if link else (),
        )

    def practice_values(self, template: str, context: dict[str, Any]) -> dict[str, str]:
        """The merge-field values of a practice-facing system email, from its Jinja context."""
        spec = PRACTICE_TEMPLATES[template]
        values = {field: str(context.get(field) or "") for field in spec.fields}
        values["recipient_name"] = str(context.get("name") or "there")
        values["practice_name"] = str(
            context.get("practice_name") or context.get("organization_name") or ""
        )
        values["app_name"] = self.settings.app_name
        values["login_url"] = f"{self.settings.frontend_url}/login"
        return {field: values.get(field, "") for field in spec.fields}

    async def send(
        self,
        *,
        to: str,
        subject: str,
        html: str,
        text: str,
        template: str,
        attachments: Sequence[Attachment] = (),
        organization_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
        secrets: tuple[str, ...] = (),
    ) -> None:
        await self.gateway.send_email(
            OutboundEmail(
                to=to,
                subject=subject,
                html=html,
                text=text,
                template=template,
                attachments=attachments,
                organization_id=organization_id,
                user_id=user_id,
                secrets=secrets,
            )
        )
