"""Ready-made notifications for the types of the catalogue.

Each helper builds the title, body, link, dedupe key and merge values of one type and calls
:meth:`NotificationService.notify` inside a savepoint, so a problem raising a notification can
never fail the transaction of the business action that triggered it (it is logged instead).
Modules that raise budget alerts, timesheet reminders and payment reminders call the helpers
below; ``docs/modules/notifications.md`` lists the signatures.
"""

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.notification_types import NotificationType
from app.repositories.notifications import NotificationSettingsRepository
from app.services.notification_settings import ADMIN_PERMISSIONS
from app.services.notifications import (
    NOTIFY_JOB,
    NotificationService,
    NotifyResult,
    clip,
    dedupe_key,
)

log = get_logger(__name__)

# Paths inside the practice's app (relative to /{slug}).
LINK_TIMESHEET = "/time"
LINK_APPROVALS = "/time/approvals"
LINK_EXPENSE_APPROVALS = "/expenses/approvals"
LINK_MY_EXPENSES = "/expenses/mine"


def link_job(job_id: uuid.UUID) -> str:
    return f"/jobs/{job_id}"


def link_invoice(invoice_id: uuid.UUID) -> str:
    return f"/billing/invoices/{invoice_id}"


def uk_date(day: date) -> str:
    return day.strftime("%d/%m/%Y")


def hours(minutes: int) -> str:
    return f"{minutes // 60}:{minutes % 60:02d}"


async def notify_safely(
    session: AsyncSession,
    *,
    recipients_from: Callable[[], Awaitable[Sequence[uuid.UUID]]] | None = None,
    **kwargs: Any,
) -> NotifyResult | None:
    """:meth:`NotificationService.notify` in a savepoint; an error is logged, not raised.
    ``recipients_from`` looks the recipients up inside the savepoint too."""
    try:
        async with session.begin_nested():
            if recipients_from is not None:
                kwargs["recipients"] = await recipients_from()
            return await NotificationService(session).notify(**kwargs)
    except Exception:
        log.exception("notification_failed", type=str(kwargs.get("type")))
        return None


async def timesheet_reminder(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    recipients: Sequence[uuid.UUID],
    week_start: date,
) -> NotifyResult | None:
    """Tell people their timesheet for the week is still open. Once per person and week."""
    return await notify_safely(
        session,
        organization_id=organization_id,
        type=NotificationType.TIMESHEET_REMINDER,
        recipients=recipients,
        title="Timesheet reminder",
        body=f"Your timesheet for the week starting {uk_date(week_start)} has not been submitted.",
        link=LINK_TIMESHEET,
        dedupe_key=dedupe_key(NotificationType.TIMESHEET_REMINDER, week_start.isoformat()),
        merge={"week_start": uk_date(week_start)},
    )


async def timesheet_returned(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    timesheet_id: uuid.UUID,
    owner_id: uuid.UUID,
    approver_name: str,
    week_start: date,
    note: str | None,
    decided_at: datetime,
) -> NotifyResult | None:
    """Tell the owner of a rejected timesheet. Once per rejection."""
    reason = f": {clip(note, 300)}" if note else "."
    return await notify_safely(
        session,
        organization_id=organization_id,
        type=NotificationType.TIMESHEET_RETURNED,
        recipients=[owner_id],
        title="Timesheet returned",
        body=(
            f"{approver_name} returned your timesheet for the week starting "
            f"{uk_date(week_start)}{reason}"
        ),
        link=LINK_TIMESHEET,
        entity_type="timesheet",
        entity_id=timesheet_id,
        payload={"week_start": week_start.isoformat()},
        dedupe_key=dedupe_key(
            NotificationType.TIMESHEET_RETURNED, timesheet_id, decided_at.isoformat()
        ),
        merge={
            "week_start": uk_date(week_start),
            "approver_name": approver_name,
            "decision_note": note or "",
        },
    )


async def approvers_of(
    session: AsyncSession, organization_id: uuid.UUID, submitter_id: uuid.UUID
) -> list[uuid.UUID]:
    """Who decides on the person's timesheet: their manager when the manager can approve,
    else everyone who holds ``timesheet.approve`` (never the person themselves)."""
    repo = NotificationSettingsRepository(session)
    approvers = await repo.members_with_any_permission(organization_id, ("timesheet.approve",))
    manager = await repo.manager_of(organization_id, submitter_id)
    if manager is not None and manager != submitter_id and manager in approvers:
        return [manager]
    return [approver for approver in approvers if approver != submitter_id]


async def approval_pending(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    timesheet_id: uuid.UUID,
    submitter_id: uuid.UUID,
    submitter_name: str,
    week_start: date,
    total_minutes: int,
    submitted_at: datetime,
) -> NotifyResult | None:
    """Tell the approvers a timesheet waits for them. Once per submission."""
    return await notify_safely(
        session,
        organization_id=organization_id,
        type=NotificationType.APPROVAL_PENDING,
        recipients_from=lambda: approvers_of(session, organization_id, submitter_id),
        title="Timesheet waiting for approval",
        body=(
            f"{submitter_name} submitted their timesheet for the week starting "
            f"{uk_date(week_start)} ({hours(total_minutes)} hours)."
        ),
        link=LINK_APPROVALS,
        entity_type="timesheet",
        entity_id=timesheet_id,
        payload={"week_start": week_start.isoformat(), "total_minutes": total_minutes},
        dedupe_key=dedupe_key(
            NotificationType.APPROVAL_PENDING, timesheet_id, submitted_at.isoformat()
        ),
        merge={
            "submitter_name": submitter_name,
            "week_start": uk_date(week_start),
            "total_hours": hours(total_minutes),
        },
    )


async def budget_alert(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    recipients: Sequence[uuid.UUID],
    job_id: uuid.UUID,
    job_name: str,
    client_name: str,
    percent: int,
    kind: str | None = None,
    crossing: str | None = None,
) -> NotifyResult | None:
    """Tell people a job used ``percent`` % of its budget. Once per job and threshold.

    ``kind`` (``minutes`` or ``fee``) names the budget and is part of the key; a ``crossing``
    token (one per time the threshold is crossed) lets a threshold that was crossed again
    after usage fell back below it notify again.
    """
    what = {"minutes": "hours budget", "fee": "fee budget"}.get(kind or "", "budget")
    return await notify_safely(
        session,
        organization_id=organization_id,
        type=NotificationType.BUDGET_ALERT,
        recipients=recipients,
        title=f"Budget alert: {job_name}",
        body=f"{job_name} for {client_name} has used {percent}% of its {what}.",
        link=link_job(job_id),
        entity_type="job",
        entity_id=job_id,
        payload={"percent": percent, **({"kind": kind} if kind else {})},
        dedupe_key=dedupe_key(
            NotificationType.BUDGET_ALERT, job_id, percent, *(p for p in (kind, crossing) if p)
        ),
        merge={"client_name": client_name, "job_name": job_name, "budget_percent": percent},
    )


async def invoice_delivery(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    sender_id: uuid.UUID,
    invoice_id: uuid.UUID,
    invoice_number: str,
    client_name: str,
    amount_due: str,
    sent_to: Sequence[str],
    request_key: str,
) -> NotifyResult | None:
    """Tell the sender their invoice email was delivered. Once per ``request_key`` (the send
    request it answers)."""
    addresses = ", ".join(sent_to)
    return await notify_safely(
        session,
        organization_id=organization_id,
        type=NotificationType.INVOICE_DELIVERY,
        recipients=[sender_id],
        title=f"Invoice {invoice_number} sent",
        body=f"Emailed to {addresses} for {client_name} ({amount_due}).",
        link=link_invoice(invoice_id),
        entity_type="invoice",
        entity_id=invoice_id,
        payload={"sent_to": list(sent_to)},
        dedupe_key=dedupe_key(NotificationType.INVOICE_DELIVERY, invoice_id, request_key),
        merge={
            "client_name": client_name,
            "invoice_number": invoice_number,
            "amount_due": amount_due,
            "sent_to": addresses,
        },
    )


async def payment_reminder(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    recipients: Sequence[uuid.UUID],
    invoice_id: uuid.UUID,
    invoice_number: str,
    client_name: str,
    amount_due: str,
    due_date: date,
    days_overdue: int,
    stage: str,
) -> NotifyResult | None:
    """Tell people an invoice is overdue. Once per invoice and ``stage`` (a label for the
    reminder, e.g. ``"first"`` or the date it is sent)."""
    return await notify_safely(
        session,
        organization_id=organization_id,
        type=NotificationType.PAYMENT_REMINDER,
        recipients=recipients,
        title=f"Payment reminder: invoice {invoice_number}",
        body=(
            f"Invoice {invoice_number} for {client_name} ({amount_due}) was due on "
            f"{uk_date(due_date)} and is {days_overdue} days overdue."
        ),
        link=link_invoice(invoice_id),
        entity_type="invoice",
        entity_id=invoice_id,
        payload={"days_overdue": days_overdue},
        dedupe_key=dedupe_key(NotificationType.PAYMENT_REMINDER, invoice_id, stage),
        merge={
            "client_name": client_name,
            "invoice_number": invoice_number,
            "amount_due": amount_due,
            "due_date": uk_date(due_date),
            "days_overdue": days_overdue,
        },
    )


@dataclass(frozen=True, slots=True)
class FailedJob:
    job_id: uuid.UUID
    organization_id: uuid.UUID | None
    job_type: str
    attempts: int
    status: str  # ``dead`` (retries used up) or ``failed`` (could never succeed)


_JOB_LABELS = {
    "send_invoice_email": "Emailing an invoice",
    "send_payment_reminder_email": "Emailing a payment reminder",
    "send_statement_email": "Emailing a statement",
    "send_email": "Sending an email",
}


async def failed_job(session: AsyncSession, job: FailedJob) -> NotifyResult | None:
    """Tell the practice's administrators a background job failed for good. Once per job.

    Jobs of no practice, and the delivery of notifications themselves (which would only
    notify about their own failure), are skipped.
    """
    if job.organization_id is None or job.job_type == NOTIFY_JOB:
        return None
    label = _JOB_LABELS.get(job.job_type, job.job_type.replace("_", " ").capitalize())
    organization_id = job.organization_id
    return await notify_safely(
        session,
        organization_id=organization_id,
        type=NotificationType.FAILED_JOB,
        recipients_from=lambda: NotificationSettingsRepository(session).members_with_any_permission(
            organization_id, ADMIN_PERMISSIONS
        ),
        title="A background job failed",
        body=f"{label} failed after {job.attempts} attempt(s) and will not be retried.",
        entity_type="background_job",
        entity_id=job.job_id,
        payload={"job_type": job.job_type, "status": job.status, "attempts": job.attempts},
        dedupe_key=dedupe_key(NotificationType.FAILED_JOB, job.job_id),
        merge={"job_type": label, "attempts": job.attempts},
    )


async def expense_approvers_of(
    session: AsyncSession, organization_id: uuid.UUID, claimant_id: uuid.UUID
) -> list[uuid.UUID]:
    """Who decides on the person's expense claim: their manager when the manager can approve,
    else everyone who holds ``expense.approve`` and ``expense.read_all`` (never the claimant)."""
    repo = NotificationSettingsRepository(session)
    approvers = await repo.members_with_any_permission(organization_id, ("expense.approve",))
    manager = await repo.manager_of(organization_id, claimant_id)
    if manager is not None and manager != claimant_id and manager in approvers:
        return [manager]
    everyone = await repo.members_with_any_permission(
        organization_id, ("expense.read_all",), among=approvers
    )
    return [approver for approver in everyone if approver != claimant_id]


async def expense_pending(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    claim_id: uuid.UUID,
    claimant_id: uuid.UUID,
    claimant_name: str,
    claim_title: str,
    claim_total: str,
    submitted_at: datetime,
) -> NotifyResult | None:
    """Tell the approvers an expense claim waits for them. Once per submission."""
    return await notify_safely(
        session,
        organization_id=organization_id,
        type=NotificationType.EXPENSE_PENDING,
        recipients_from=lambda: expense_approvers_of(session, organization_id, claimant_id),
        title="Expense claim waiting for approval",
        body=f"{claimant_name} submitted the expense claim {claim_title} ({claim_total}).",
        link=LINK_EXPENSE_APPROVALS,
        entity_type="expense_claim",
        entity_id=claim_id,
        payload={"claim_total": claim_total},
        dedupe_key=dedupe_key(NotificationType.EXPENSE_PENDING, claim_id, submitted_at.isoformat()),
        merge={
            "claimant_name": claimant_name,
            "claim_title": claim_title,
            "claim_total": claim_total,
        },
    )


async def expense_approved(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    claim_id: uuid.UUID,
    claimant_id: uuid.UUID,
    approver_name: str,
    claim_title: str,
    claim_total: str,
    decided_at: datetime,
) -> NotifyResult | None:
    """Tell the claimant their expense claim was approved. Once per approval."""
    return await notify_safely(
        session,
        organization_id=organization_id,
        type=NotificationType.EXPENSE_APPROVED,
        recipients=[claimant_id],
        title="Expense claim approved",
        body=f"{approver_name} approved your expense claim {claim_title} ({claim_total}).",
        link=LINK_MY_EXPENSES,
        entity_type="expense_claim",
        entity_id=claim_id,
        payload={"claim_total": claim_total},
        dedupe_key=dedupe_key(NotificationType.EXPENSE_APPROVED, claim_id, decided_at.isoformat()),
        merge={
            "decider_name": approver_name,
            "claim_title": claim_title,
            "claim_total": claim_total,
        },
    )


async def expense_returned(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    claim_id: uuid.UUID,
    claimant_id: uuid.UUID,
    approver_name: str,
    claim_title: str,
    claim_total: str,
    reason: str,
    decided_at: datetime,
) -> NotifyResult | None:
    """Tell the claimant their expense claim was returned, with the reason. Once per return."""
    return await notify_safely(
        session,
        organization_id=organization_id,
        type=NotificationType.EXPENSE_RETURNED,
        recipients=[claimant_id],
        title="Expense claim returned",
        body=(
            f"{approver_name} returned your expense claim {claim_title} "
            f"({claim_total}): {clip(reason, 300)}"
        ),
        link=LINK_MY_EXPENSES,
        entity_type="expense_claim",
        entity_id=claim_id,
        payload={"claim_total": claim_total},
        dedupe_key=dedupe_key(NotificationType.EXPENSE_RETURNED, claim_id, decided_at.isoformat()),
        merge={
            "decider_name": approver_name,
            "claim_title": claim_title,
            "claim_total": claim_total,
            "claim_reason": reason,
        },
    )
