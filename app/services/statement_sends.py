"""Emailing client statements, and the list of clients a statement can go to.

``send`` only queues: it logs the request in ``statement_sends`` and enqueues a
``send_statement_email`` job; the worker draws the statement PDF (the client's invoice template:
letterhead, bank details, footer) and emails it. A client that already has a statement queued
in the last ten minutes is not queued again, whatever the period.
"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.context import RequestContext
from app.db.models import StatementSend
from app.db.session import unit_of_work
from app.repositories.billing_common import BillingReferenceRepository
from app.repositories.statement_sends import StatementFilters, StatementSendRepository
from app.schemas.billing import ClientRef
from app.schemas.statement_sends import (
    SortOrder,
    StatementRow,
    StatementSendRead,
    StatementSendRequest,
    StatementSort,
    StatementsRead,
    StatementTotals,
)
from app.services import invoice_pdf
from app.services import jobs as job_queue
from app.services.billing_common import get_client_or_404, invalid, now_utc, record, today_for
from app.services.email import Attachment, EmailService, PermanentEmailError
from app.services.email_templates import default_content, load_override
from app.services.invoices import addressee_for, billing_recipients
from app.services.policy import OrgContext
from app.services.statements import StatementService
from app.services.template_catalogue import TEMPLATES
from app.services.template_render import render_email
from app.utils.pagination import PageParams

SEND_JOB = "send_statement_email"
TEMPLATE = "client_statement"
# A client with a statement queued this recently is not queued another.
DUPLICATE_WINDOW = timedelta(minutes=10)


class StatementEmailError(Exception):
    """The statement email can never be sent (bad payload, unknown practice or client)."""


@dataclass(frozen=True, slots=True)
class StatementListQuery:
    client_id: uuid.UUID | None = None
    partner_user_id: uuid.UUID | None = None
    manager_user_id: uuid.UUID | None = None
    min_balance: int | None = None
    overdue_only: bool = False
    never_sent: bool = False
    q: str | None = None
    sort: StatementSort = "client"
    order: SortOrder | None = None


def _scope(f: StatementFilters) -> str:
    """Identifies the filters a cursor was issued for."""
    raw = "|".join(
        str(v)
        for v in (
            f.today,
            f.client_id,
            f.partner_user_id,
            f.manager_user_id,
            f.min_balance,
            f.overdue_only,
            f.never_sent,
            f.q,
        )
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:10]


def _read(send: StatementSend, *, queued: bool) -> StatementSendRead:
    return StatementSendRead(
        id=send.id,
        client_id=send.client_id,
        to=list(send.recipients),
        date_from=send.date_from,
        date_to=send.date_to,
        closing_balance=send.closing_balance,
        queued=queued,
    )


class StatementSendService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.sends = StatementSendRepository(session)
        self.statements = StatementService(session)
        self.refs = BillingReferenceRepository(session)

    async def send(
        self,
        ctx: OrgContext,
        client_id: uuid.UUID,
        data: StatementSendRequest,
        rctx: RequestContext,
    ) -> StatementSendRead:
        """Queue the client's statement for email: 422 ``NO_RECIPIENT`` when there is nobody to
        send it to, 404 for an unknown client."""
        organization = ctx.organization
        client = await get_client_or_404(self.refs, ctx.organization_id, client_id)
        to = (
            [str(a) for a in data.to]
            if data.to
            else await billing_recipients(self.refs, ctx.organization_id, client)
        )
        if not to:
            raise invalid(
                "NO_RECIPIENT",
                "to",
                "The client has no billing contact or email: give the addresses to send to.",
            )
        statement = await self.statements.build(organization, client, data.date_from, data.date_to)
        async with unit_of_work(self.session):
            await self.sends.lock_client(ctx.organization_id, client.id)
            waiting = await self.sends.queued_since(
                ctx.organization_id, client.id, now_utc() - DUPLICATE_WINDOW
            )
            if waiting is not None:
                return _read(waiting, queued=False)
            send = await self.sends.add(
                StatementSend(
                    organization_id=ctx.organization_id,
                    client_id=client.id,
                    date_from=statement.date_from,
                    date_to=statement.date_to,
                    recipients=to,
                    closing_balance=statement.closing_balance,
                    sent_by=ctx.user.id,
                )
            )
            await job_queue.enqueue(
                self.session,
                SEND_JOB,
                {
                    "organization_id": str(ctx.organization_id),
                    "statement_send_id": str(send.id),
                    "requested_by": str(ctx.user.id),
                },
                organization_id=ctx.organization_id,
                dedupe_key=f"{SEND_JOB}:{send.id}",
            )
            await record(
                self.session,
                ctx,
                rctx,
                "statement.send_queued",
                target_type="client",
                target_id=client.id,
                meta={
                    "statement_send_id": str(send.id),
                    "client_name": client.name,
                    "to": to,
                    "date_from": statement.date_from.isoformat() if statement.date_from else None,
                    "date_to": statement.date_to.isoformat(),
                    "closing_balance": statement.closing_balance,
                },
            )
        return _read(send, queued=True)

    async def list_statements(
        self, ctx: OrgContext, query: StatementListQuery, params: PageParams
    ) -> tuple[StatementsRead, str | None]:
        today = today_for(ctx.organization)
        filters = StatementFilters(
            today=today,
            client_id=query.client_id,
            partner_user_id=query.partner_user_id,
            manager_user_id=query.manager_user_id,
            min_balance=query.min_balance,
            overdue_only=query.overdue_only,
            never_sent=query.never_sent,
            q=query.q,
        )
        descending = (query.order or ("asc" if query.sort == "client" else "desc")) == "desc"
        rows, next_cursor = await self.sends.page(
            ctx.organization_id,
            filters,
            sort=query.sort,
            scope=_scope(filters),
            descending=descending,
            params=params,
        )
        totals = await self.sends.totals(ctx.organization_id, filters)
        data = StatementsRead(
            totals=StatementTotals(
                client_count=totals["client_count"],
                outstanding=totals["outstanding"],
                overdue=totals["overdue"],
                on_account=totals["on_account"],
                balance=totals["balance"],
            ),
            rows=[
                StatementRow(
                    client=ClientRef(id=r.client_id, code=r.client_code, name=r.client_name),
                    partner_user_id=r.partner_user_id,
                    manager_user_id=r.manager_user_id,
                    outstanding=r.outstanding,
                    on_account=r.on_account,
                    balance=r.balance,
                    overdue=r.overdue,
                    oldest_overdue_due_date=r.oldest_overdue_due_date,
                    oldest_overdue_days=(
                        max(0, (today - r.oldest_overdue_due_date).days)
                        if r.oldest_overdue_due_date
                        else None
                    ),
                    last_statement_sent_at=r.last_sent_at,
                    last_statement_to=list(r.last_sent_to) if r.last_sent_to else None,
                    has_recipient=bool(r.has_recipient),
                )
                for r in rows
            ],
        )
        return data, next_cursor


# --- The email job -------------------------------------------------------------------------


async def deliver_statement_email(
    session: AsyncSession, settings: Settings, payload: dict[str, Any]
) -> dict[str, Any]:
    """The ``send_statement_email`` job: draw the statement and email it as a PDF.

    Idempotent: a send that is no longer ``queued`` does nothing. A refused address marks the
    send ``failed`` (the job itself then succeeds, so the mark is kept); a provider outage
    raises and is retried with backoff.
    """
    try:
        organization_id = uuid.UUID(str(payload["organization_id"]))
        send_id = uuid.UUID(str(payload["statement_send_id"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise StatementEmailError(f"bad payload: {exc}") from exc
    refs = BillingReferenceRepository(session)
    organization = await refs.get_organization(organization_id)
    if organization is None:
        raise StatementEmailError("organization not found")
    sends = StatementSendRepository(session)
    send = await sends.get(organization_id, send_id)
    if send is None:
        raise StatementEmailError("statement send not found")
    if send.status != "queued":
        return {**payload, "skipped": f"already_{send.status}"}
    client = await refs.get_client(organization_id, send.client_id)
    if client is None:
        raise StatementEmailError("client not found")
    statements = StatementService(session)
    statement = await statements.build(organization, client, send.date_from, send.date_to)
    filename, pdf = await statements.render(organization, client, statement)
    spec = TEMPLATES[TEMPLATE]
    content = await load_override(session, organization_id, TEMPLATE) or default_content(spec)
    values = {
        "recipient_name": await addressee_for(refs, organization_id, client) or client.name,
        "practice_name": organization.name,
        "app_name": settings.app_name,
        "client_name": client.name,
        "statement_date": invoice_pdf.short_date(statement.date_to),
        "statement_balance": invoice_pdf.money(statement.closing_balance, statement.currency),
        "bank_details": organization.bank_details or "",
    }
    rendered = render_email(
        content,
        {name: values.get(name, "") for name in spec.fields},
        brand=organization.name,
        app_name=settings.app_name,
    )
    try:
        await EmailService(settings).send(
            to=", ".join(send.recipients),
            subject=rendered.subject,
            html=rendered.html,
            text=rendered.text,
            template=TEMPLATE,
            attachments=[Attachment(filename, pdf, "application/pdf")],
            organization_id=organization_id,
        )
    except PermanentEmailError as exc:
        send.status = "failed"
        send.error = str(exc)[:500]
        await session.flush()
        return {**payload, "failed": str(exc)[:200]}
    send.status = "sent"
    send.sent_at = now_utc()
    send.closing_balance = statement.closing_balance
    await session.flush()
    return {**payload, "sent_at": send.sent_at.isoformat()}


__all__ = ["StatementSendService", "deliver_statement_email"]
