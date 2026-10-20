"""Job handlers. Each must be idempotent: delivery is at-least-once."""

import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from jinja2 import UndefinedError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.repositories.jobs import JobRepository
from app.repositories.rate_limits import RateLimitRepository
from app.services.email import EmailService, PermanentEmailError
from app.services.email_templates import CUSTOMISABLE_SYSTEM_KEYS, load_override
from app.services.integration_runner import run_scheduled
from app.services.invoices import InvoiceEmailError, deliver_invoice_email
from app.services.notification_delivery import NotificationJobError, deliver_notification
from app.services.payment_reminders import (
    ReminderEmailError,
    deliver_reminder_email,
    run_scheduled_reminders,
)
from app.services.recurring import run_due as run_due_recurring
from app.services.statement_sends import StatementEmailError, deliver_statement_email
from app.services.timesheet_reminders import run_for_all_practices


class PermanentJobError(Exception):
    """The job can never succeed; it is marked ``failed`` and not retried."""


type Handler = Callable[[AsyncSession, Settings, dict[str, Any]], Awaitable[dict[str, Any] | None]]


# Names the email service sets itself; a payload may not override them.
_RESERVED_CONTEXT = frozenset(
    {
        "template",
        "to",
        "token",
        "name",
        "link",
        "subject",
        "app_name",
        "frontend_url",
        "override",
        "organization_id",
        "user_id",
        "attachments",
    }
)


def _template_context(payload: dict[str, Any]) -> dict[str, Any]:
    """Extra template variables of a ``send_email`` job (``payload["context"]``)."""
    context = payload.get("context") or {}
    if not isinstance(context, dict):
        raise PermanentJobError("send_email context must be an object")
    return {str(k): v for k, v in context.items() if k not in _RESERVED_CONTEXT}


async def send_email(
    session: AsyncSession, settings: Settings, payload: dict[str, Any]
) -> dict[str, Any] | None:
    try:
        template = payload["template"]
        organization_id = (
            uuid.UUID(str(payload["organization_id"])) if payload.get("organization_id") else None
        )
        # A practice's own version of its practice-facing emails (invitations, account setup).
        override = (
            await load_override(session, organization_id, template)
            if organization_id and template in CUSTOMISABLE_SYSTEM_KEYS
            else None
        )
        await EmailService(settings).send_template(
            template,
            to=payload["to"],
            name=payload.get("name", ""),
            token=payload.get("token"),
            override=override,
            organization_id=organization_id,
            **_template_context(payload),
        )
    except (PermanentEmailError, KeyError, ValueError, UndefinedError) as exc:
        raise PermanentJobError(str(exc)) from exc
    # The one-time token must not outlive the job.
    return {k: v for k, v in payload.items() if k != "token"}


async def send_notification(
    session: AsyncSession, settings: Settings, payload: dict[str, Any]
) -> dict[str, Any] | None:
    """Email or SMS a notification to one person. A refused address fails the job; a provider
    outage raises and is retried with backoff."""
    try:
        return await deliver_notification(session, settings, payload)
    except (NotificationJobError, PermanentEmailError) as exc:
        raise PermanentJobError(str(exc)) from exc


async def send_invoice_email(
    session: AsyncSession, settings: Settings, payload: dict[str, Any]
) -> dict[str, Any] | None:
    """Render an issued invoice's PDF and email it; sets ``sent_at``. Idempotent."""
    try:
        return await deliver_invoice_email(session, settings, payload)
    except (InvoiceEmailError, PermanentEmailError, UndefinedError) as exc:
        raise PermanentJobError(str(exc)) from exc


async def send_timesheet_reminders(
    session: AsyncSession, settings: Settings, payload: dict[str, Any]
) -> dict[str, Any] | None:
    """Daily, for every practice: remind staff whose timesheet for the last completed week is
    missing and approvers with timesheets waiting. Idempotent: a person is reminded once per
    week (``timesheet_reminders``), so a repeated or late run sends nothing twice."""
    return dict(await run_for_all_practices(session))


async def run_recurring_invoices(
    session: AsyncSession, settings: Settings, payload: dict[str, Any]
) -> dict[str, Any] | None:
    """Daily, for every practice: draft the invoice of each recurring schedule that is due
    (never issuing it). Idempotent: one run row per schedule and date."""
    return dict(await run_due_recurring(session))


async def send_payment_reminders(
    session: AsyncSession, settings: Settings, payload: dict[str, Any]
) -> dict[str, Any] | None:
    """Daily, for every practice that switched payment reminders on: queue the reminder email
    of each client with overdue invoices that were never reminded and are at least
    ``payment_reminder_days_after_due`` days past due. Idempotent: an invoice is reminded at
    most once a day (a unique index), and a scheduled reminder only once ever."""
    return dict(await run_scheduled_reminders(session))


async def send_payment_reminder_email(
    session: AsyncSession, settings: Settings, payload: dict[str, Any]
) -> dict[str, Any] | None:
    """Email one client's reminder about its overdue invoices (a batch of ``payment_reminders``
    rows) and mark the rows ``sent``. Idempotent: only rows still ``queued`` are sent."""
    try:
        return await deliver_reminder_email(session, settings, payload)
    except (ReminderEmailError, PermanentEmailError, UndefinedError) as exc:
        raise PermanentJobError(str(exc)) from exc


async def send_statement_email(
    session: AsyncSession, settings: Settings, payload: dict[str, Any]
) -> dict[str, Any] | None:
    """Draw a client's statement PDF and email it; marks the ``statement_sends`` row ``sent``.
    Idempotent: a send that is no longer ``queued`` does nothing."""
    try:
        return await deliver_statement_email(session, settings, payload)
    except (StatementEmailError, PermanentEmailError, UndefinedError) as exc:
        raise PermanentJobError(str(exc)) from exc


PARTITIONED_TABLES = ("audit_logs",)


async def process_integration_items(
    session: AsyncSession, settings: Settings, payload: dict[str, Any]
) -> dict[str, Any] | None:
    """Every minute: queue the issued invoices and credit notes of the practices that have
    bookkeeping on, then send every integration item that is due (bookkeeping documents, practice
    management statuses) with the retry backoff. Failures stay on the items and never reach
    billing; the job itself succeeds."""
    return await run_scheduled(session)


async def create_partitions(
    session: AsyncSession, settings: Settings, payload: dict[str, Any]
) -> None:
    """Keep monthly partitions three months ahead."""
    for table in PARTITIONED_TABLES:
        await session.execute(
            text(
                "SELECT create_monthly_partition(:parent, (date_trunc('month', now()) "
                "+ make_interval(months => m))::date) FROM generate_series(0, 3) AS m"
            ),
            {"parent": table},
        )


async def purge_retention(
    session: AsyncSession, settings: Settings, payload: dict[str, Any]
) -> None:
    """Housekeeping only. Financial records (time, invoices, payments, audit) are never
    deleted by a job: UK practices must keep them for at least six years."""
    await JobRepository(session).purge_finished(timedelta(days=14))
    await RateLimitRepository(session).purge(timedelta(days=1))
    await session.execute(
        text(
            "DELETE FROM user_sessions WHERE (revoked_at < now() - interval '30 days') "
            "OR (expires_at < now() - interval '30 days')"
        )
    )
    for table in ("email_verification_tokens", "password_reset_tokens"):
        await session.execute(
            text(f"DELETE FROM {table} WHERE expires_at < now() - interval '30 days'")  # noqa: S608
        )
    # A bell inbox older than six months is clutter, and the mock gateway's log is a dev aid.
    await session.execute(
        text("DELETE FROM notifications WHERE created_at < now() - interval '180 days'")
    )
    await session.execute(
        text("DELETE FROM message_log WHERE created_at < now() - interval '90 days'")
    )


HANDLERS: dict[str, Handler] = {
    "send_email": send_email,
    "send_invoice_email": send_invoice_email,
    "send_notification": send_notification,
    "send_timesheet_reminders": send_timesheet_reminders,
    "send_payment_reminders": send_payment_reminders,
    "run_recurring_invoices": run_recurring_invoices,
    "send_payment_reminder_email": send_payment_reminder_email,
    "send_statement_email": send_statement_email,
    "process_integration_items": process_integration_items,
    "create_partitions": create_partitions,
    "purge_retention": purge_retention,
}

# (job type, period in seconds). The slot number is part of the dedupe key.
PERIODIC: tuple[tuple[str, int], ...] = (
    ("create_partitions", 86_400),
    ("purge_retention", 86_400),
    ("send_timesheet_reminders", 86_400),
    ("send_payment_reminders", 86_400),
    ("run_recurring_invoices", 86_400),
    ("process_integration_items", 60),
)
