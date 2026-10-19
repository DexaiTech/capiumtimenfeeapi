"""Delivering a queued notification by email or SMS: the ``send_notification`` job.

The job carries ids and the notification's text, never an address or a number: the recipient is
looked up when it runs, so a person who has left (or who switched the channel's number off)
since the job was queued is not messaged. Everything goes through the messaging gateway.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import Organization
from app.repositories.notifications import NotificationSettingsRepository
from app.services.email import EmailService
from app.services.email_templates import default_content, load_override
from app.services.messaging import OutboundSms, build_gateway
from app.services.notifications import clip
from app.services.template_catalogue import TEMPLATES
from app.services.template_render import render_email

SMS_LIMIT = 320


class NotificationJobError(Exception):
    """The job can never succeed (bad payload, unknown template): it is failed, not retried."""


def absolute_link(settings: Settings, slug: str, link: str | None) -> str:
    """Where a notification's link points in a message: the practice's app on FRONTEND_URL."""
    return f"{settings.frontend_url}/{slug}{link}" if link else ""


def sms_text(practice_name: str, title: str, body: str, link: str) -> str:
    text = f"{practice_name}: {title}"
    if body:
        text += f" - {body}"
    return clip(f"{text} {link}".strip(), SMS_LIMIT)


async def deliver_notification(
    session: AsyncSession, settings: Settings, payload: dict[str, Any]
) -> dict[str, Any]:
    try:
        channel = str(payload["channel"])
        type_ = str(payload["type"])
        organization_id = uuid.UUID(str(payload["organization_id"]))
        user_id = uuid.UUID(str(payload["user_id"]))
        title = str(payload["title"])
        body = str(payload.get("body") or "")
        merge = {str(k): str(v) for k, v in (payload.get("merge") or {}).items()}
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise NotificationJobError(f"bad payload: {exc}") from exc
    spec = TEMPLATES.get(type_)
    if spec is None or channel not in ("email", "sms"):
        raise NotificationJobError(f"cannot deliver {type_!r} by {channel!r}")

    organization = await session.get(Organization, organization_id)
    people = await NotificationSettingsRepository(session).recipients(organization_id, [user_id])
    recipient = people.get(user_id)
    if organization is None or organization.deleted_at is not None or recipient is None:
        return {**payload, "skipped": "recipient_unavailable"}
    link = absolute_link(settings, organization.slug, payload.get("link"))

    if channel == "sms":
        if not recipient.mobile_number:
            return {**payload, "skipped": "no_mobile_number"}
        result = await build_gateway(settings).send_sms(
            OutboundSms(
                to=recipient.mobile_number,
                body=sms_text(organization.name, title, body, link),
                template=type_,
                organization_id=organization_id,
                user_id=user_id,
            )
        )
        return {**payload, "provider_ref": result.provider_ref}

    values = {name: merge.get(name, "") for name in spec.fields}
    values.update(
        recipient_name=recipient.full_name,
        practice_name=organization.name,
        app_name=settings.app_name,
        link=link,
    )
    content = await load_override(session, organization_id, type_) or default_content(spec)
    rendered = render_email(content, values, brand=organization.name, app_name=settings.app_name)
    await EmailService(settings).send(
        to=recipient.email,
        subject=rendered.subject,
        html=rendered.html,
        text=rendered.text,
        template=type_,
        organization_id=organization_id,
        user_id=user_id,
    )
    return payload
