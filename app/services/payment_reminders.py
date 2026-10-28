"""Payment reminders: emails to clients about their unpaid invoices.

One email per client lists the client's overdue invoices; each invoice gets a
``payment_reminders`` row (the rows of an email share a ``batch_id``). The email is sent by the
``send_payment_reminder_email`` job, so nothing is sent from a request. At most one reminder per
invoice and day (practice time zone) is queued or sent: a unique index decides when two requests
race. The daily scheduled job (``run_scheduled_reminders``) follows, for practices that switched
it on, the practice's reminder schedule: days counted from the due date (negative before it, 0 on
it, positive after it). An unpaid invoice is reminded once for each day of the schedule as it
comes, until it is paid; a practice without a schedule has the one reminder
``payment_reminder_days_after_due`` days after the due date. Before and on the due date the
friendly "payment due soon" note goes; after it, the reminder.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.context import RequestContext
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.db.models import Client, Invoice, Organization, PaymentReminder
from app.db.session import unit_of_work
from app.repositories.billing_common import BillingReferenceRepository
from app.repositories.payment_reminders import PaymentReminderRepository, ReminderFilters
from app.schemas.billing import ClientRef, PersonRef
from app.schemas.debtors import (
    ReminderClient,
    ReminderInvoice,
    ReminderInvoiceRef,
    ReminderRead,
    ReminderRequest,
    ReminderResult,
    ReminderSkipped,
    ReminderSource,
    ReminderStatus,
    ReminderTemplate,
    SkipReason,
)
from app.services import audit, invoice_pdf, policy
from app.services import jobs as job_queue
from app.services.billing_common import now_utc, record, today_for
from app.services.debtors import READ_CODES
from app.services.email import EmailService, PermanentEmailError
from app.services.email_templates import default_content, load_override
from app.services.invoices import addressee_for, billing_recipients
from app.services.policy import OrgContext
from app.services.template_catalogue import TEMPLATES
from app.services.template_render import RenderedEmail, render_email
from app.utils.ids import new_id
from app.utils.pagination import PageParams
from app.utils.reminder_schedule import effective_schedule, latest_step_reached

log = get_logger(__name__)

SEND_JOB = "send_payment_reminder_email"
DEFAULT_TEMPLATE: ReminderTemplate = "client_payment_reminder"
UPCOMING_TEMPLATE: ReminderTemplate = "client_payment_upcoming"
# A queued reminder older than this no longer silences its invoice for the scheduled job.
STALE_AFTER = timedelta(days=1)


class ReminderEmailError(Exception):
    """The reminder email can never be sent (bad payload, unknown practice)."""


@dataclass(slots=True)
class _Due:
    invoice: Invoice
    days_overdue: int
    outstanding: int


@dataclass(slots=True)
class _ClientPlan:
    client: Client
    to: list[str]
    dues: list[_Due] = field(default_factory=list)

    @property
    def total_due(self) -> int:
        return sum(d.outstanding for d in self.dues)


def _ineligible(invoice: Invoice, today: date, *, upcoming: bool = False) -> SkipReason | None:
    """Why an invoice cannot be reminded about, or None when it is overdue (with ``upcoming``:
    or about to fall due) and owes money."""
    if invoice.status != "issued":
        return "not_issued"
    if invoice.balance <= 0:
        return "no_balance"
    if invoice.due_date is None or (invoice.due_date >= today and not upcoming):
        return "not_overdue"
    return None


def _skip(invoice: Invoice, reason: SkipReason) -> ReminderSkipped:
    return ReminderSkipped(
        invoice_id=invoice.id, number=invoice.number, client_id=invoice.client_id, reason=reason
    )


def _due(invoice: Invoice, today: date) -> _Due:
    """``days_overdue`` is negative for an invoice that has not fallen due yet."""
    assert invoice.due_date is not None
    return _Due(invoice, (today - invoice.due_date).days, invoice.balance)


async def plan_reminders(
    session: AsyncSession,
    organization: Organization,
    invoices: Sequence[Invoice],
    today: date,
    *,
    upcoming: bool = False,
) -> tuple[list[_ClientPlan], list[ReminderSkipped]]:
    """Group the invoices that can be reminded about by client and say why the others cannot:
    not issued, nothing owed, not overdue, already reminded today, or no address to write to.
    With ``upcoming`` an invoice that has not fallen due yet can be reminded about too."""
    refs = BillingReferenceRepository(session)
    skipped: list[ReminderSkipped] = []
    eligible: list[Invoice] = []
    for invoice in invoices:
        reason = _ineligible(invoice, today, upcoming=upcoming)
        if reason is None:
            eligible.append(invoice)
        else:
            skipped.append(_skip(invoice, reason))
    already = await PaymentReminderRepository(session).reminded_on(
        organization.id, [i.id for i in eligible], today
    )
    by_client: dict[uuid.UUID, list[Invoice]] = {}
    for invoice in eligible:
        if invoice.id in already:
            skipped.append(_skip(invoice, "already_reminded_today"))
        else:
            by_client.setdefault(invoice.client_id, []).append(invoice)
    clients = await refs.clients_by_ids(organization.id, by_client)
    plans: list[_ClientPlan] = []
    for client_id, items in by_client.items():
        client = clients[client_id]
        to = await billing_recipients(refs, organization.id, client)
        if not to:
            skipped += [_skip(i, "no_recipient") for i in items]
            continue
        items.sort(key=lambda i: (i.due_date or today, i.number or "", i.id))
        plans.append(_ClientPlan(client, to, [_due(i, today) for i in items]))
    plans.sort(key=lambda p: (p.client.name.lower(), p.client.id))
    return plans, skipped


def _days_label(days: int) -> str:
    if days > 0:
        return f"{days} day overdue" if days == 1 else f"{days} days overdue"
    if days == 0:
        return "today"
    return "tomorrow" if days == -1 else f"in {-days} days"


async def render_reminder(
    session: AsyncSession,
    settings: Settings,
    organization: Organization,
    client: Client,
    template: str,
    dues: Sequence[_Due],
) -> RenderedEmail:
    """The email as it goes out: the practice's version of the template, else the built-in."""
    spec = TEMPLATES[template]
    content = await load_override(session, organization.id, template) or default_content(spec)
    refs = BillingReferenceRepository(session)
    currency = dues[0].invoice.currency if dues else organization.currency
    lines = [
        f"{d.invoice.number} - {invoice_pdf.money(d.outstanding, d.invoice.currency)} - due "
        f"{invoice_pdf.short_date(d.invoice.due_date)} ({_days_label(d.days_overdue)})"
        for d in dues
        if d.invoice.due_date is not None
    ]
    values = {
        "recipient_name": await addressee_for(refs, organization.id, client) or client.name,
        "practice_name": organization.name,
        "app_name": settings.app_name,
        "client_name": client.name,
        "invoice_count": str(len(dues)),
        "invoice_list": "\n".join(lines),
        "total_due": invoice_pdf.money(sum(d.outstanding for d in dues), currency),
        "bank_details": organization.bank_details or "",
    }
    return render_email(
        content,
        {name: values.get(name, "") for name in spec.fields},
        brand=organization.name,
        app_name=settings.app_name,
    )


async def _queue(
    session: AsyncSession,
    organization: Organization,
    plan: _ClientPlan,
    *,
    template: str,
    source: str,
    today: date,
    user_id: uuid.UUID | None,
    actor: OrgContext | None = None,
    rctx: RequestContext | None = None,
) -> list[_Due]:
    """Log the reminders of one email and queue its job. Returns the invoices that were
    reminded: those another request had already reminded today are left out."""
    batch_id = new_id()
    reminders = PaymentReminderRepository(session)
    inserted = await reminders.claim(
        [
            {
                "organization_id": organization.id,
                "invoice_id": due.invoice.id,
                "batch_id": batch_id,
                "template": template,
                "source": source,
                "sent_on": today,
                "recipients": plan.to,
                "days_overdue": max(due.days_overdue, 0),
                "amount_due": due.outstanding,
                "sent_by": user_id,
            }
            for due in plan.dues
        ]
    )
    queued = [d for d in plan.dues if d.invoice.id in inserted]
    if not queued:
        return []
    await job_queue.enqueue(
        session,
        SEND_JOB,
        {
            "organization_id": str(organization.id),
            "batch_id": str(batch_id),
            "requested_by": str(user_id) if user_id else None,
        },
        organization_id=organization.id,
        dedupe_key=f"{SEND_JOB}:{batch_id}",
    )
    meta: dict[str, Any] = {
        "client_name": plan.client.name,
        "template": template,
        "source": source,
        "batch_id": str(batch_id),
        "to": plan.to,
        "total_due": sum(d.outstanding for d in queued),
        "invoices": [
            {
                "invoice_id": str(d.invoice.id),
                "number": d.invoice.number,
                "days_overdue": d.days_overdue,
                "outstanding": d.outstanding,
            }
            for d in queued
        ],
    }
    if actor is not None and rctx is not None:
        await record(
            session,
            actor,
            rctx,
            "payment_reminder.queued",
            target_type="client",
            target_id=plan.client.id,
            meta=meta,
        )
    else:
        await audit.record(
            session,
            "payment_reminder.queued",
            organization_id=organization.id,
            target_type="client",
            target_id=plan.client.id,
            meta=meta,
        )
    return queued


class PaymentReminderService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.reminders = PaymentReminderRepository(session)
        self.refs = BillingReferenceRepository(session)

    async def send(
        self, ctx: OrgContext, data: ReminderRequest, rctx: RequestContext
    ) -> ReminderResult:
        """Queue the reminders (or, with ``dry_run``, only show what would go out)."""
        organization = ctx.organization
        today = today_for(organization)
        if data.dry_run:
            plans, skipped = await self._plan(ctx, data, today)
            return ReminderResult(
                dry_run=True,
                template=data.template,
                emails_queued=0,
                invoices_queued=0,
                clients=[
                    await self._preview(organization, p, data.template, p.dues) for p in plans
                ],
                skipped=skipped,
            )
        async with unit_of_work(self.session):
            plans, skipped = await self._plan(ctx, data, today)
            clients: list[ReminderClient] = []
            for plan in plans:
                queued = await _queue(
                    self.session,
                    organization,
                    plan,
                    template=data.template,
                    source="manual",
                    today=today,
                    user_id=ctx.user.id,
                    actor=ctx,
                    rctx=rctx,
                )
                queued_ids = {q.invoice.id for q in queued}
                skipped += [
                    _skip(d.invoice, "already_reminded_today")
                    for d in plan.dues
                    if d.invoice.id not in queued_ids
                ]
                if queued:
                    clients.append(await self._preview(organization, plan, data.template, queued))
        return ReminderResult(
            dry_run=False,
            template=data.template,
            emails_queued=len(clients),
            invoices_queued=sum(len(c.invoices) for c in clients),
            clients=clients,
            skipped=skipped,
        )

    async def history(
        self, ctx: OrgContext, filters: ReminderFilters, params: PageParams
    ) -> tuple[list[ReminderRead], str | None]:
        policy.ensure_any(ctx, *READ_CODES)
        rows, next_cursor = await self.reminders.history(ctx.organization_id, filters, params)
        clients = await self.refs.clients_by_ids(ctx.organization_id, {r.client_id for r in rows})
        names = await self.refs.member_names(ctx.organization_id, [r[0].sent_by for r in rows])
        items: list[ReminderRead] = []
        for row in rows:
            reminder: PaymentReminder = row[0]
            client = clients[row.client_id]
            items.append(
                ReminderRead(
                    id=reminder.id,
                    client=ClientRef.from_model(client),
                    invoice=ReminderInvoiceRef(id=reminder.invoice_id, number=row.number),
                    template=reminder.template,
                    source=cast(ReminderSource, reminder.source),
                    status=cast(ReminderStatus, reminder.status),
                    sent_on=reminder.sent_on,
                    to=list(reminder.recipients),
                    days_overdue=reminder.days_overdue,
                    amount_due=reminder.amount_due,
                    sent_by=(
                        PersonRef(user_id=reminder.sent_by, full_name=names.get(reminder.sent_by))
                        if reminder.sent_by
                        else None
                    ),
                    sent_at=reminder.sent_at,
                    error=reminder.error,
                    created_at=reminder.created_at,
                )
            )
        return items, next_cursor

    # --- Internals ---------------------------------------------------------------------

    async def _plan(
        self, ctx: OrgContext, data: ReminderRequest, today: date
    ) -> tuple[list[_ClientPlan], list[ReminderSkipped]]:
        org_id = ctx.organization_id
        if data.client_ids is not None:
            wanted = set(data.client_ids)
            clients = await self.refs.clients_by_ids(org_id, wanted)
            if len(clients) != len(wanted) or any(c.deleted_at for c in clients.values()):
                raise NotFoundError("Client not found.")
            invoices = await self.reminders.overdue_invoices(org_id, wanted, today)
        else:
            assert data.invoice_ids is not None
            ordered = list(dict.fromkeys(data.invoice_ids))
            found = await self.reminders.invoices_by_ids(org_id, ordered)
            if len(found) != len(ordered):
                raise NotFoundError("Invoice not found.")
            invoices = [found[i] for i in ordered]
        return await plan_reminders(self.session, ctx.organization, invoices, today)

    async def _preview(
        self, organization: Organization, plan: _ClientPlan, template: str, dues: Sequence[_Due]
    ) -> ReminderClient:
        rendered = await render_reminder(
            self.session, self.settings, organization, plan.client, template, dues
        )
        return ReminderClient(
            client=ClientRef.from_model(plan.client),
            to=plan.to,
            subject=rendered.subject,
            text=rendered.text,
            total_due=sum(d.outstanding for d in dues),
            invoices=[
                ReminderInvoice(
                    invoice_id=d.invoice.id,
                    number=d.invoice.number,
                    due_date=d.invoice.due_date,
                    days_overdue=d.days_overdue,
                    outstanding=d.outstanding,
                )
                for d in dues
            ],
        )


# --- The scheduled job ---------------------------------------------------------------------


async def run_scheduled_reminders(session: AsyncSession) -> dict[str, int]:
    """The daily job: every practice that switched reminders on queues the reminders that are
    due. A practice that fails is logged and skipped; the others still run. Idempotent: an
    invoice is reminded once, and never twice in a day."""
    stmt = (
        select(Organization.id)
        .where(Organization.deleted_at.is_(None), Organization.payment_reminders_enabled.is_(True))
        .order_by(Organization.id)
    )
    organization_ids = list((await session.execute(stmt)).scalars())
    totals = {"practices": 0, "failed": 0, "emails": 0, "invoices": 0}
    for organization_id in organization_ids:
        try:
            async with unit_of_work(session):
                emails, invoices = await queue_scheduled(session, organization_id)
        except Exception:
            log.exception("payment_reminders_failed", organization_id=str(organization_id))
            totals["failed"] += 1
            continue
        totals["practices"] += 1
        totals["emails"] += emails
        totals["invoices"] += invoices
    return totals


async def queue_scheduled(session: AsyncSession, organization_id: uuid.UUID) -> tuple[int, int]:
    """Queue one practice's scheduled reminders; returns ``(emails, invoices)``. Does not
    commit: the caller's transaction carries the rows.

    An unpaid invoice is reminded when a day of the schedule has come (the due date plus that
    many days) and it has not been reminded since: once for each day as it arrives, never twice
    for the same one, and never once it is paid."""
    organization = await BillingReferenceRepository(session).get_organization(organization_id)
    if organization is None or not organization.payment_reminders_enabled:
        return 0, 0
    today = today_for(organization)
    schedule = effective_schedule(
        organization.payment_reminder_schedule, organization.payment_reminder_days_after_due
    )
    repo = PaymentReminderRepository(session)
    candidates = await repo.scheduled_candidates(
        organization.id, today=today, first_day=schedule[0]
    )
    last = await repo.last_reminded_on(
        organization.id, [i.id for i in candidates], stale_before=now_utc() - STALE_AFTER
    )
    due: list[Invoice] = []
    for invoice in candidates:
        assert invoice.due_date is not None
        step = latest_step_reached(schedule, invoice.due_date, today)
        if step is not None and (invoice.id not in last or last[invoice.id] < step):
            due.append(invoice)
    emails = reminded = 0
    for template, upcoming, group in (
        (DEFAULT_TEMPLATE, False, [i for i in due if i.due_date and i.due_date < today]),
        (UPCOMING_TEMPLATE, True, [i for i in due if i.due_date and i.due_date >= today]),
    ):
        plans, _ = await plan_reminders(session, organization, group, today, upcoming=upcoming)
        for plan in plans:
            queued = await _queue(
                session,
                organization,
                plan,
                template=template,
                source="scheduled",
                today=today,
                user_id=None,
            )
            if queued:
                emails += 1
                reminded += len(queued)
    return emails, reminded


# --- The email job -------------------------------------------------------------------------


async def deliver_reminder_email(
    session: AsyncSession, settings: Settings, payload: dict[str, Any]
) -> dict[str, Any]:
    """The ``send_payment_reminder_email`` job: send the email of one batch of reminders.

    Idempotent: only rows still ``queued`` are sent. An invoice that was paid, credited or
    voided since it was queued is left out (its row becomes ``skipped``); a refused address marks
    the rows ``failed`` (the job itself then succeeds, so the rows are kept); a provider outage
    raises and is retried.
    """
    try:
        organization_id = uuid.UUID(str(payload["organization_id"]))
        batch_id = uuid.UUID(str(payload["batch_id"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise ReminderEmailError(f"bad payload: {exc}") from exc
    refs = BillingReferenceRepository(session)
    organization = await refs.get_organization(organization_id)
    if organization is None:
        raise ReminderEmailError("organization not found")
    reminders = PaymentReminderRepository(session)
    queued = [r for r in await reminders.batch(organization_id, batch_id) if r.status == "queued"]
    if not queued:
        return {**payload, "skipped": "nothing_queued"}
    invoices = await reminders.invoices_by_ids(organization_id, [r.invoice_id for r in queued])
    today = today_for(organization)
    pending: list[tuple[PaymentReminder, _Due]] = []
    for row in queued:
        invoice = invoices.get(row.invoice_id)
        upcoming = row.template == UPCOMING_TEMPLATE
        if invoice is None or _ineligible(invoice, today, upcoming=upcoming) is not None:
            row.status = "skipped"
            row.error = "The invoice no longer needs a reminder."
        else:
            pending.append((row, _due(invoice, today)))
    await session.flush()
    if not pending:
        return {**payload, "skipped": "nothing_to_remind"}
    client = await refs.get_client(organization_id, pending[0][1].invoice.client_id)
    if client is None:
        raise ReminderEmailError("client not found")
    dues = [due for _, due in pending]
    template = pending[0][0].template
    rendered = await render_reminder(session, settings, organization, client, template, dues)
    to = list(pending[0][0].recipients)
    try:
        await EmailService(settings).send(
            to=", ".join(to),
            subject=rendered.subject,
            html=rendered.html,
            text=rendered.text,
            template=template,
            organization_id=organization_id,
        )
    except PermanentEmailError as exc:
        for row, _ in pending:
            row.status = "failed"
            row.error = str(exc)[:500]
        await session.flush()
        return {**payload, "failed": str(exc)[:200]}
    now = now_utc()
    for row, due in pending:
        row.status = "sent"
        row.sent_at = now
        row.days_overdue = max(due.days_overdue, 0)
        row.amount_due = due.outstanding
    await session.flush()
    return {**payload, "sent_at": now.isoformat(), "invoices": len(pending)}
