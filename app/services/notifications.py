"""In-app notifications, and the one small API the other modules use to raise them.

A module that wants to tell someone something calls :meth:`NotificationService.notify` (or one of
the ready-made helpers in ``app.services.notification_events``) inside its own transaction.
``notify`` then, for each recipient:

* does nothing at all when the practice has the type switched off, or when the person is not an
  active member who can sign in;
* adds a row to the person's inbox when their in-app channel is on for the type;
* queues a ``send_notification`` job (``background_jobs``, same transaction) for each of email and
  SMS that is on for them.

A ``dedupe_key`` makes it idempotent: a person who already has a notification with the key gets
nothing again (no second row, no second email), so a trigger that fires repeatedly is safe.
"""

import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.core.notification_types import NotificationType, get_type
from app.db.models import Notification
from app.db.session import unit_of_work
from app.repositories.jobs import JobRepository
from app.repositories.notifications import NotificationRepository, NotificationSettingsRepository
from app.services import jobs as job_queue
from app.services.notification_settings import effective_channels, type_enabled
from app.services.policy import OrgContext
from app.utils.ids import new_id
from app.utils.pagination import Page, PageParams

log = get_logger(__name__)

NOTIFY_JOB = "send_notification"
TITLE_LIMIT, BODY_LIMIT, MERGE_VALUE_LIMIT, LINK_LIMIT = 200, 2000, 500, 300


@dataclass(slots=True)
class NotifyResult:
    created: int = 0  # inbox rows added
    duplicates: int = 0  # people who already had this ``dedupe_key``
    emails_queued: int = 0
    sms_queued: int = 0
    unavailable: int = 0  # recipients who cannot be notified (not active, or no login)
    suppressed: bool = False  # the practice has the type switched off


def clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def check_link(link: str | None) -> str | None:
    """A link is a path inside the app, never an address that leaves it."""
    if link is None:
        return None
    unsafe = (
        not link.startswith("/")
        or link.startswith("//")
        or "\\" in link
        or len(link) > LINK_LIMIT
        or any(ch.isspace() or ord(ch) < 32 for ch in link)
    )
    if unsafe:
        raise ValueError(f"link must be a path inside the app such as /time/approvals: {link!r}")
    return link


def dedupe_key(type_: str, *parts: object) -> str:
    """``type:part:part`` , the usual shape of a ``dedupe_key``."""
    return ":".join([str(type_), *(str(part) for part in parts)])[:200]


class NotificationService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.inbox = NotificationRepository(session)
        self.settings = NotificationSettingsRepository(session)

    # --- Raising a notification (the API of the other modules) ----------------------------

    async def notify(
        self,
        *,
        organization_id: uuid.UUID,
        type: NotificationType | str,
        recipients: Iterable[uuid.UUID],
        title: str,
        body: str = "",
        link: str | None = None,
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
        payload: Mapping[str, Any] | None = None,
        dedupe_key: str | None = None,
        merge: Mapping[str, object] | None = None,
    ) -> NotifyResult:
        """Notify ``recipients`` (user ids of members of the practice).

        ``title`` and ``body`` are the in-app text (and the SMS), ``link`` the path inside the
        app it opens (relative to ``/{practice slug}``), ``payload`` extra JSON kept on the row.
        ``merge`` holds the values of the email template's merge fields
        (``{"job_name": "…", "budget_percent": 80}``); the recipient's name, the practice name
        and the link are filled in for every email. Does not commit: the caller's transaction
        carries the rows and the jobs.
        """
        spec = get_type(str(type))
        title = clip(title, TITLE_LIMIT)
        if not title:
            raise ValueError("a notification needs a title")
        body = clip(body, BODY_LIMIT)
        link = check_link(link)
        if (entity_type is None) != (entity_id is None):
            raise ValueError("entity_type and entity_id go together")
        ids = list(dict.fromkeys(recipients))
        result = NotifyResult()

        setting = await self.settings.practice_setting(organization_id, spec.code)
        if not type_enabled(setting):
            result.suppressed = True
            return result
        people = await self.settings.recipients(organization_id, ids)
        result.unavailable = len(ids) - len(people)
        preferences = await self.settings.preferences_for_type(
            organization_id, list(people), spec.code
        )
        channels = {
            user_id: effective_channels(spec, setting, preferences.get(user_id))
            for user_id in people
        }

        in_app = [user_id for user_id in people if channels[user_id].in_app]
        extra = _json_safe(payload)
        inserted = await self.inbox.insert_new(
            [
                {
                    "id": new_id(),
                    "organization_id": organization_id,
                    "user_id": user_id,
                    "type": spec.code,
                    "title": title,
                    "body": body,
                    "link": link,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "payload": extra,
                    "dedupe_key": dedupe_key,
                }
                for user_id in in_app
            ]
        )
        result.created = len(inserted)
        result.duplicates = len(in_app) - len(inserted)

        # Email and SMS go to everyone who wants them, except people whose inbox row was a
        # duplicate: they have been told already.
        delivery = {
            user_id: wanted
            for user_id, wanted in channels.items()
            if not (wanted.in_app and user_id not in inserted)
        }
        jobs = []
        for user_id, wanted in delivery.items():
            # No number, no SMS (the person may add one later; the job is not worth queueing).
            sms_on = wanted.sms and bool(people[user_id].mobile_number)
            for channel, on in (("email", wanted.email), ("sms", sms_on)):
                if on:
                    key = f"notify:{channel}:{user_id}:{dedupe_key}" if dedupe_key else None
                    jobs.append((channel, user_id, key))
        keys = [key for _, _, key in jobs if key]
        seen = await JobRepository(self.session).existing_dedupe_keys(keys) if keys else set()
        values = {str(k): clip(str(v), MERGE_VALUE_LIMIT) for k, v in (merge or {}).items()}
        for channel, user_id, key in jobs:
            if key in seen:
                continue
            queued = await job_queue.enqueue(
                self.session,
                NOTIFY_JOB,
                {
                    "channel": channel,
                    "type": spec.code,
                    "organization_id": str(organization_id),
                    "user_id": str(user_id),
                    "notification_id": str(inserted[user_id]) if user_id in inserted else None,
                    "title": title,
                    "body": body,
                    "link": link,
                    "merge": values,
                },
                organization_id=organization_id,
                dedupe_key=key,
            )
            if queued and channel == "email":
                result.emails_queued += 1
            elif queued:
                result.sms_queued += 1
        return result

    # --- The inbox of the signed-in person -----------------------------------------------

    async def list_own(
        self, ctx: OrgContext, *, unread_only: bool, params: PageParams
    ) -> Page[Notification]:
        return await self.inbox.list_page(
            ctx.organization_id, ctx.user.id, unread_only=unread_only, params=params
        )

    async def unread_count(self, ctx: OrgContext) -> int:
        return await self.inbox.unread_count(ctx.organization_id, ctx.user.id)

    async def mark_read(self, ctx: OrgContext, notification_id: uuid.UUID) -> Notification:
        """Idempotent: a read notification keeps its ``read_at``. Someone else's is 404."""
        async with unit_of_work(self.session):
            row = await self.inbox.get(ctx.organization_id, ctx.user.id, notification_id)
            if row is None:
                raise NotFoundError("Notification not found.")
            if row.read_at is None:
                row.read_at = datetime.now(UTC)
                await self.session.flush()
        return row

    async def mark_all_read(self, ctx: OrgContext) -> int:
        async with unit_of_work(self.session):
            return await self.inbox.mark_all_read(ctx.organization_id, ctx.user.id)


def _json_safe(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """The payload as JSON can hold it (ids and dates become text)."""
    loaded = json.loads(json.dumps(dict(payload or {}), default=str))
    assert isinstance(loaded, dict)
    return loaded
