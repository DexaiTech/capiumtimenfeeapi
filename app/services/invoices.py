"""Invoices: drafts built from WIP, lines, totals, issue, void, PDF and email.

Every change to a draft runs under the invoice row lock and ends by recomputing the
invoice totals from its lines. WIP items are locked (time entries, then disbursements,
each in id order) after the invoice, the order every billing flow uses.

How billed values reconcile: a line's ``net_amount`` is apportioned across the WIP items
it bills in proportion to their WIP values (``money.apportion``), so the billed values of
a line always sum to its net exactly; ``billed - wip`` per item is its write-up/off.
"""

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.context import RequestContext
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.db.models import (
    Client,
    Disbursement,
    Invoice,
    InvoiceApproval,
    InvoiceLine,
    Job,
    Organization,
    TimeEntry,
    WipAllocation,
)
from app.db.session import unit_of_work
from app.repositories.billing_common import BillingReferenceRepository
from app.repositories.invoice_templates import InvoiceTemplateRepository
from app.repositories.invoices import InvoiceFilters, InvoiceRepository
from app.repositories.wip import WipRepository
from app.schemas.billing import PersonRef, hours_of
from app.schemas.bulk import BulkChangeResult, unique_ids
from app.schemas.invoices import (
    ApprovalEventRead,
    CreditNoteSummary,
    DisplayStatus,
    InvoiceAddWip,
    InvoiceApprove,
    InvoiceBulkAction,
    InvoiceCreate,
    InvoiceIssue,
    InvoiceListSummary,
    InvoicePaymentRead,
    InvoiceRead,
    InvoiceReject,
    InvoiceSend,
    InvoiceSent,
    InvoiceStatusCounts,
    InvoiceSubmit,
    InvoiceSummaryRead,
    InvoiceUpdate,
    InvoiceVoid,
    InvoiceWipTotals,
    LineCreate,
    LineOverride,
    LineRead,
    LineUpdate,
    LineWipRead,
    VatSummaryRow,
    WipSelection,
)
from app.services import (
    billing_notifications,
    document_templates,
    invoice_pdf,
    notification_events,
    policy,
)
from app.services import jobs as job_queue
from app.services.audit import AuditRow
from app.services.billing_common import (
    VatResolver,
    conflict,
    get_client_or_404,
    invalid,
    now_utc,
    record,
    today_for,
)
from app.services.common import record_audit_many
from app.services.email import Attachment, EmailService
from app.services.email_templates import load_override
from app.services.policy import OrgContext
from app.services.sequences import next_number
from app.services.wip import LockedItems, lock_items, lock_wip_items, set_items_status
from app.utils.ids import new_id
from app.utils.money import apportion, line_net, round_half_up, vat_on
from app.utils.pagination import PageParams

SEND_JOB = "send_invoice_email"
MAX_DESCRIPTION = 1000
WIP_KINDS = frozenset({"time", "fixed_fee", "disbursement"})


class InvoiceEmailError(Exception):
    """The invoice email can never be sent (unknown invoice, not issued, no recipient)."""


def display_status(invoice: Invoice, today: date) -> DisplayStatus:
    """Derived, never stored. Overdue wins over part paid: it is what credit control
    acts on. A fully credited invoice is ``credited`` (not ``paid``)."""
    if invoice.status != "issued":
        return cast(DisplayStatus, invoice.status)  # draft, awaiting_approval, approved, void
    if invoice.credited_total > 0 and invoice.credited_total == invoice.gross_total:
        return "credited"
    if invoice.balance == 0:
        return "paid"
    if invoice.due_date is not None and invoice.due_date < today:
        return "overdue"
    if invoice.paid_total > 0:
        return "part_paid"
    return "issued"


def approval_needed(organization: Organization, invoice: Invoice) -> bool:
    """Whether the invoice must be approved before it can be issued: the practice requires
    approval and the gross total reaches the threshold (0: every invoice). An invoice already
    waiting for approval, or approved, counts as needing it."""
    if invoice.status in ("awaiting_approval", "approved"):
        return True
    return (
        invoice.status == "draft"
        and organization.invoice_approval_required
        and invoice.gross_total >= organization.invoice_approval_threshold
    )


def preparers(invoice: Invoice, approvals: Sequence[InvoiceApproval]) -> set[uuid.UUID]:
    """The people who may not approve the invoice: whoever created it and whoever submitted it
    last."""
    people = {invoice.created_by} if invoice.created_by else set()
    submitted = [a for a in approvals if a.action == "submitted"]
    if submitted:
        people.add(submitted[-1].actor_user_id)
    return people


def _short_date(day: date) -> str:
    return f"{day.day} {day.strftime('%b %Y')}"


def _period(days: Sequence[date]) -> str:
    first, last = min(days), max(days)
    if first == last:
        return _short_date(first)
    return f"{_short_date(first)} to {_short_date(last)}"


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_DESCRIPTION else text[: MAX_DESCRIPTION - 3] + "..."


def _unit_for(net: int, quantity: Decimal) -> int:
    return net if quantity == 1 else round_half_up(Decimal(net) / quantity)


@dataclass(slots=True)
class _PlannedLine:
    kind: Literal["time", "fixed_fee", "disbursement"]
    description: str
    job_id: uuid.UUID | None
    service_id: uuid.UUID | None
    quantity: Decimal
    unit_amount: int
    net_amount: int
    time_entries: list[TimeEntry] = field(default_factory=list)
    disbursements: list[Disbursement] = field(default_factory=list)
    line_rate_id: uuid.UUID | None = None
    validate_rate: bool = False
    overridden: bool = False


def _time_line(
    entries: list[TimeEntry], description: str, job: Job | None, service_id: uuid.UUID | None
) -> _PlannedLine:
    minutes = sum(e.minutes for e in entries)
    value = sum(e.charge_value for e in entries)
    quantity = hours_of(minutes)
    # The average hourly rate of the time on the line.
    unit = round_half_up(Decimal(value) * 60 / Decimal(minutes)) if minutes else 0
    return _PlannedLine(
        kind="time",
        description=_clip(description),
        job_id=job.id if job else None,
        service_id=service_id,
        quantity=quantity,
        unit_amount=unit,
        net_amount=value,
        time_entries=entries,
    )


def plan_lines(
    items: LockedItems,
    jobs: dict[uuid.UUID, Job],
    group_by: str,
    fixed_fee_billed: dict[uuid.UUID, int],
) -> list[_PlannedLine]:
    """The lines that bill ``items``, before overrides.

    Time on fixed-fee jobs always becomes one ``fixed_fee`` line per job (whatever
    ``group_by`` says): the fee less what earlier live invoices already billed of it.
    """
    fixed: dict[uuid.UUID, list[TimeEntry]] = defaultdict(list)
    hourly: list[TimeEntry] = []
    for entry in sorted(items.time_entries, key=lambda e: (e.entry_date, e.id)):
        job = jobs.get(entry.job_id) if entry.job_id else None
        if job is not None and job.billing_basis == "fixed":
            fixed[job.id].append(entry)
        else:
            hourly.append(entry)

    lines: list[_PlannedLine] = []
    for job_id, entries in fixed.items():
        job = jobs[job_id]
        fee = max((job.fixed_fee or 0) - fixed_fee_billed.get(job_id, 0), 0)
        lines.append(
            _PlannedLine(
                kind="fixed_fee",
                description=_clip(f"{job.name} (fixed fee)"),
                job_id=job.id,
                service_id=job.service_id,
                quantity=Decimal(1),
                unit_amount=fee,
                net_amount=fee,
                time_entries=entries,
            )
        )

    if group_by == "job":
        by_job: dict[uuid.UUID | None, list[TimeEntry]] = defaultdict(list)
        for entry in hourly:
            by_job[entry.job_id].append(entry)
        for group_job_id, entries in by_job.items():
            job = jobs.get(group_job_id) if group_job_id else None
            name = job.name if job else "Professional services"
            period = _period([e.entry_date for e in entries])
            lines.append(
                _time_line(entries, f"{name} ({period})", job, job.service_id if job else None)
            )
    elif group_by == "entry":
        for entry in hourly:
            job = jobs.get(entry.job_id) if entry.job_id else None
            label = entry.description or (job.name if job else "Professional services")
            lines.append(
                _time_line(
                    [entry],
                    f"{_short_date(entry.entry_date)}: {label}",
                    job,
                    job.service_id if job else None,
                )
            )
    elif hourly:
        services = {jobs[e.job_id].service_id if e.job_id in jobs else None for e in hourly}
        service_id = services.pop() if len(services) == 1 else None
        period = _period([e.entry_date for e in hourly])
        lines.append(_time_line(hourly, f"Professional services ({period})", None, service_id))

    for disbursement in sorted(items.disbursements, key=lambda d: (d.incurred_on, d.id)):
        text = disbursement.description
        if disbursement.supplier:
            text = f"{text} ({disbursement.supplier})"
        lines.append(
            _PlannedLine(
                kind="disbursement",
                description=_clip(text),
                job_id=disbursement.job_id,
                service_id=None,
                quantity=Decimal(1),
                unit_amount=disbursement.amount,
                net_amount=disbursement.amount,
                disbursements=[disbursement],
                line_rate_id=disbursement.vat_rate_id,
            )
        )
    return lines


def apply_overrides(lines: list[_PlannedLine], overrides: Sequence[LineOverride]) -> None:
    seen: set[int] = set()
    for index, override in enumerate(overrides):
        where = f"overrides.{index}"
        if override.disbursement_id:
            matches = [
                i
                for i, line in enumerate(lines)
                if any(d.id == override.disbursement_id for d in line.disbursements)
            ]
        elif override.time_entry_id:
            matches = [
                i
                for i, line in enumerate(lines)
                if any(e.id == override.time_entry_id for e in line.time_entries)
            ]
        else:
            matches = [
                i
                for i, line in enumerate(lines)
                if line.job_id == override.job_id and line.kind in ("time", "fixed_fee")
            ]
        if not matches:
            raise invalid("OVERRIDE_NO_LINE", where, "No line of this draft bills that item.")
        if len(matches) > 1:
            raise invalid(
                "OVERRIDE_AMBIGUOUS",
                where,
                "Several lines bill this job; choose one by time_entry_id.",
            )
        target = matches[0]
        if target in seen:
            raise invalid("OVERRIDE_DUPLICATE", where, "This line is overridden twice.")
        seen.add(target)
        line = lines[target]
        if override.net_amount is not None:
            line.net_amount = override.net_amount
            line.unit_amount = _unit_for(override.net_amount, line.quantity)
            line.overridden = True
        if override.description:
            line.description = _clip(override.description)
        if "vat_rate_id" in override.model_fields_set:
            line.line_rate_id = override.vat_rate_id
            line.validate_rate = override.vat_rate_id is not None


def apportion_allocations(allocations: Sequence[WipAllocation], net_amount: int) -> None:
    """Spread ``net_amount`` over the allocations by WIP value, in id order."""
    ordered = sorted(allocations, key=lambda a: a.id)
    for allocation, billed in zip(
        ordered, apportion(net_amount, [a.wip_value for a in ordered]), strict=True
    ):
        allocation.billed_value = billed


class InvoiceService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.invoices = InvoiceRepository(session)
        self.wip = WipRepository(session)
        self.refs = BillingReferenceRepository(session)

    # --- Reads -------------------------------------------------------------------------

    async def list_invoices(
        self, ctx: OrgContext, filters: InvoiceFilters, params: PageParams
    ) -> tuple[list[InvoiceSummaryRead], str | None]:
        page = await self.invoices.list_page(
            ctx.organization_id, filters, today=today_for(ctx.organization), params=params
        )
        return await self.summaries(ctx, page.items), page.next_cursor

    async def summaries(
        self, ctx: OrgContext, invoices: Sequence[Invoice]
    ) -> list[InvoiceSummaryRead]:
        clients = await self.refs.clients_by_ids(
            ctx.organization_id, [i.client_id for i in invoices]
        )
        today = today_for(ctx.organization)
        return [
            InvoiceSummaryRead.build(
                i,
                clients.get(i.client_id),
                display_status(i, today),
                approval_needed(ctx.organization, i),
            )
            for i in invoices
        ]

    async def list_summary(self, ctx: OrgContext, filters: InvoiceFilters) -> InvoiceListSummary:
        """Counts per display status and what is owed, for the status tabs of the list."""
        figures = await self.invoices.summary(
            ctx.organization_id, filters, today=today_for(ctx.organization)
        )
        counts = figures.by_status
        return InvoiceListSummary(
            counts=InvoiceStatusCounts(
                draft=counts["draft"],
                awaiting_approval=counts["awaiting_approval"],
                approved=counts["approved"],
                issued=counts["issued"],
                part_paid=counts["part_paid"],
                paid=counts["paid"],
                credited=counts["credited"],
                overdue=counts["overdue"],
                void=counts["void"],
                total=figures.total,
            ),
            outstanding=figures.outstanding,
            overdue=figures.overdue,
            awaiting_approval_gross=figures.awaiting_approval_gross,
        )

    async def get(self, ctx: OrgContext, invoice_id: uuid.UUID) -> InvoiceRead:
        return await self._detail(ctx, await self._get(ctx, invoice_id))

    async def _get(self, ctx: OrgContext, invoice_id: uuid.UUID, lock: bool = False) -> Invoice:
        invoice = await self.invoices.get(ctx.organization_id, invoice_id, for_update=lock)
        if invoice is None:
            raise NotFoundError("Invoice not found.")
        return invoice

    async def _detail(self, ctx: OrgContext, invoice: Invoice) -> InvoiceRead:
        org_id = ctx.organization_id
        client = await self.refs.get_client(org_id, invoice.client_id)
        lines = await self.invoices.lines(org_id, invoice.id)
        allocations = await self.wip.allocations_for_invoice(org_id, invoice.id)
        by_line: dict[uuid.UUID, list[WipAllocation]] = defaultdict(list)
        for allocation in allocations:
            if allocation.invoice_line_id:
                by_line[allocation.invoice_line_id].append(allocation)
        line_reads = []
        for line in lines:
            mine = by_line.get(line.id, [])
            wip = None
            if mine or line.kind in WIP_KINDS:
                wip_value = sum(a.wip_value for a in mine)
                billed = sum(a.billed_value for a in mine)
                wip = LineWipRead(
                    item_count=len(mine),
                    time_entry_ids=[a.time_entry_id for a in mine if a.time_entry_id],
                    disbursement_ids=[a.disbursement_id for a in mine if a.disbursement_id],
                    wip_value=wip_value,
                    billed_value=billed,
                    adjustment=billed - wip_value,
                )
            line_reads.append(LineRead.from_model(line, wip))
        wip_total = sum(a.wip_value for a in allocations)
        billed_total = sum(a.billed_value for a in allocations)
        payments = await self.invoices.payment_allocations(org_id, invoice.id)
        credit_notes = await self.invoices.credit_notes(org_id, invoice.id)
        approvals = await self.invoices.approvals(org_id, invoice.id)
        names = await self.refs.member_names(org_id, [a.actor_user_id for a in approvals])
        summary = InvoiceSummaryRead.build(
            invoice,
            client,
            display_status(invoice, today_for(ctx.organization)),
            approval_needed(ctx.organization, invoice),
        )
        return InvoiceRead(
            **summary.model_dump(),
            notes=invoice.notes,
            issued_at=invoice.issued_at,
            issued_by=invoice.issued_by,
            voided_at=invoice.voided_at,
            void_reason=invoice.void_reason,
            created_by=invoice.created_by,
            approvals=[
                ApprovalEventRead(
                    id=a.id,
                    action=cast(Any, a.action),
                    actor=PersonRef(user_id=a.actor_user_id, full_name=names.get(a.actor_user_id)),
                    note=a.note,
                    gross_total=a.gross_total,
                    created_at=a.created_at,
                )
                for a in approvals
            ],
            can_approve=self._can_approve(ctx, invoice, approvals),
            lines=line_reads,
            vat_summary=[
                VatSummaryRow(vat_rate_bp=bp, net=net, vat=vat)
                for bp, net, vat in vat_summary(lines)
            ],
            wip=InvoiceWipTotals(
                wip_value=wip_total, billed_value=billed_total, adjustment=billed_total - wip_total
            ),
            payments=[
                InvoicePaymentRead(
                    allocation_id=a.id,
                    payment_id=p.id,
                    received_on=p.received_on,
                    method=p.method,
                    reference=p.reference,
                    amount=a.amount,
                )
                for a, p in payments
            ],
            credit_notes=[
                CreditNoteSummary(
                    id=c.id,
                    number=c.number,
                    issue_date=c.issue_date,
                    reason=c.reason,
                    gross_total=c.gross_total,
                )
                for c in credit_notes
            ],
        )

    @staticmethod
    def _can_approve(
        ctx: OrgContext, invoice: Invoice, approvals: Sequence[InvoiceApproval]
    ) -> bool:
        if invoice.status != "awaiting_approval" or not policy.has_permission(
            ctx, "invoice.approve"
        ):
            return False
        return policy.is_owner(ctx) or ctx.user.id not in preparers(invoice, approvals)

    # --- Drafts ------------------------------------------------------------------------

    async def create_draft(
        self, ctx: OrgContext, data: InvoiceCreate, rctx: RequestContext
    ) -> InvoiceRead:
        organization = ctx.organization
        if data.issue_date and data.due_date and data.due_date < data.issue_date:
            raise ValidationError.for_field("due_date", "The due date is before the issue date.")
        async with unit_of_work(self.session):
            client = await get_client_or_404(self.refs, ctx.organization_id, data.client_id)
            items = await lock_wip_items(
                self.session, organization, client, data.time_entry_ids, data.disbursement_ids
            )
            invoice = await self.invoices.add(
                Invoice(
                    organization_id=ctx.organization_id,
                    client_id=client.id,
                    status="draft",
                    issue_date=data.issue_date,
                    due_date=data.due_date,
                    reference=data.reference,
                    notes=data.notes,
                    currency=organization.currency,
                    net_total=0,
                    vat_total=0,
                    gross_total=0,
                    paid_total=0,
                    credited_total=0,
                    created_by=ctx.user.id,
                )
            )
            planned = await self._plan(ctx, items, data)
            await self._write_lines(ctx, invoice, client, planned, start_position=1)
            set_items_status(items, "in_draft")
            await self._recompute(invoice)
            await record(
                self.session,
                ctx,
                rctx,
                "invoice.draft_created",
                target_type="invoice",
                target_id=invoice.id,
                meta=_meta(invoice, client, wip_value=items.wip_value, item_count=items.count),
            )
            detail = await self._detail(ctx, invoice)
        return detail

    async def add_wip(
        self, ctx: OrgContext, invoice_id: uuid.UUID, data: InvoiceAddWip, rctx: RequestContext
    ) -> InvoiceRead:
        async with unit_of_work(self.session):
            invoice = await self._locked_draft(ctx, invoice_id)
            client = await get_client_or_404(self.refs, ctx.organization_id, invoice.client_id)
            items = await lock_wip_items(
                self.session, ctx.organization, client, data.time_entry_ids, data.disbursement_ids
            )
            planned = await self._plan(ctx, items, data)
            # A fixed fee is billed once per draft: more time on that job joins its line.
            existing: dict[uuid.UUID, InvoiceLine] = {
                line.job_id: line
                for line in await self.invoices.lines(ctx.organization_id, invoice.id)
                if line.kind == "fixed_fee" and line.job_id
            }
            new_lines = []
            for plan in planned:
                target = (
                    existing.get(plan.job_id) if plan.kind == "fixed_fee" and plan.job_id else None
                )
                if target is None:
                    new_lines.append(plan)
                    continue
                await self.wip.add_allocations(self._allocations(ctx, invoice, target.id, plan))
                if plan.overridden:
                    target.net_amount = plan.net_amount
                    target.unit_amount = plan.unit_amount
                    target.vat_amount = vat_on(target.net_amount, target.vat_rate_bp)
                    target.description = plan.description
                await self._reapportion(ctx, target)
            start = await self.invoices.max_position(ctx.organization_id, invoice.id) + 1
            await self._write_lines(ctx, invoice, client, new_lines, start_position=start)
            set_items_status(items, "in_draft")
            await self._recompute(invoice)
            await record(
                self.session,
                ctx,
                rctx,
                "invoice.wip_added",
                target_type="invoice",
                target_id=invoice.id,
                meta=_meta(invoice, client, wip_value=items.wip_value, item_count=items.count),
            )
            detail = await self._detail(ctx, invoice)
        return detail

    async def update(
        self, ctx: OrgContext, invoice_id: uuid.UUID, data: InvoiceUpdate, rctx: RequestContext
    ) -> InvoiceRead:
        fields = data.model_fields_set
        async with unit_of_work(self.session):
            invoice = await self._get(ctx, invoice_id, lock=True)
            if invoice.status != "draft":
                raise conflict(
                    "INVOICE_NOT_EDITABLE",
                    "Only a draft invoice can be edited. Reject it back to a draft first, or "
                    "raise a credit note against an issued invoice.",
                    details={"status": invoice.status},
                )
            changes: dict[str, list[Any]] = {}
            for name in ("issue_date", "due_date", "reference", "notes"):
                if name not in fields or getattr(data, name) == getattr(invoice, name):
                    continue
                old, new = getattr(invoice, name), getattr(data, name)
                changes[name] = [_plain(old), _plain(new)]
                setattr(invoice, name, new)
            if invoice.issue_date and invoice.due_date and invoice.due_date < invoice.issue_date:
                raise ValidationError.for_field(
                    "due_date", "The due date is before the issue date."
                )
            if changes:
                await self.session.flush()
                client = await self.refs.get_client(ctx.organization_id, invoice.client_id)
                await record(
                    self.session,
                    ctx,
                    rctx,
                    "invoice.updated",
                    target_type="invoice",
                    target_id=invoice.id,
                    meta={**_meta(invoice, client), "changes": changes},
                )
            detail = await self._detail(ctx, invoice)
        return detail

    async def add_line(
        self, ctx: OrgContext, invoice_id: uuid.UUID, data: LineCreate, rctx: RequestContext
    ) -> InvoiceRead:
        async with unit_of_work(self.session):
            invoice = await self._locked_draft(ctx, invoice_id)
            client = await get_client_or_404(self.refs, ctx.organization_id, invoice.client_id)
            if data.job_id is not None:
                job = await self.refs.get_job(ctx.organization_id, data.job_id)
                if job is None or job.client_id != client.id:
                    raise ValidationError.for_field(
                        "job_id", "The job must be one of the client's jobs."
                    )
            if data.service_id is not None:
                service = await self.refs.get_service(ctx.organization_id, data.service_id)
                if service is None:
                    raise ValidationError.for_field("service_id", "This service does not exist.")
            net = (
                data.net_amount
                if data.net_amount is not None
                else line_net(data.quantity, data.unit_amount)
            )
            rate_id, rate_bp = await VatResolver(self.refs, ctx.organization).resolve(
                client=client,
                line_rate_id=data.vat_rate_id,
                service_id=data.service_id,
                validate_line_rate=True,
            )
            position = data.position or (
                await self.invoices.max_position(ctx.organization_id, invoice.id) + 1
            )
            line = InvoiceLine(
                id=new_id(),
                organization_id=ctx.organization_id,
                invoice_id=invoice.id,
                position=position,
                kind="manual",
                description=data.description,
                job_id=data.job_id,
                service_id=data.service_id,
                quantity=data.quantity,
                unit_amount=data.unit_amount,
                net_amount=net,
                vat_rate_id=rate_id,
                vat_rate_bp=rate_bp,
                vat_amount=vat_on(net, rate_bp),
            )
            await self.invoices.add_lines([line])
            await self._recompute(invoice)
            await record(
                self.session,
                ctx,
                rctx,
                "invoice.line_added",
                target_type="invoice",
                target_id=invoice.id,
                meta={**_meta(invoice, client), "line": _line_meta(line)},
            )
            detail = await self._detail(ctx, invoice)
        return detail

    async def update_line(
        self,
        ctx: OrgContext,
        invoice_id: uuid.UUID,
        line_id: uuid.UUID,
        data: LineUpdate,
        rctx: RequestContext,
    ) -> InvoiceRead:
        fields = data.model_fields_set
        async with unit_of_work(self.session):
            invoice = await self._locked_draft(ctx, invoice_id)
            line = await self._line(ctx, invoice, line_id)
            client = await get_client_or_404(self.refs, ctx.organization_id, invoice.client_id)
            before = _line_meta(line)
            old_net = line.net_amount
            if "description" in fields and data.description:
                line.description = data.description
            if "position" in fields and data.position:
                line.position = data.position
            sizing = False
            if "quantity" in fields and data.quantity is not None:
                line.quantity = data.quantity
                sizing = True
            if "unit_amount" in fields and data.unit_amount is not None:
                line.unit_amount = data.unit_amount
                sizing = True
            if "net_amount" in fields and data.net_amount is not None:
                line.net_amount = data.net_amount
                if "unit_amount" not in fields:
                    line.unit_amount = _unit_for(data.net_amount, line.quantity)
            elif sizing:
                line.net_amount = line_net(line.quantity, line.unit_amount)
            if line.kind != "manual" and line.net_amount < 0:
                raise invalid(
                    "NEGATIVE_WIP_LINE",
                    "net_amount",
                    "A line that bills WIP cannot be negative; add a discount line instead.",
                )
            if "vat_rate_id" in fields:
                line.vat_rate_id, line.vat_rate_bp = await VatResolver(
                    self.refs, ctx.organization
                ).resolve(
                    client=client,
                    line_rate_id=data.vat_rate_id,
                    service_id=line.service_id,
                    validate_line_rate=data.vat_rate_id is not None,
                )
            line.vat_amount = vat_on(line.net_amount, line.vat_rate_bp)
            if line.net_amount != old_net:
                await self._reapportion(ctx, line)
            await self.session.flush()
            await self._recompute(invoice)
            await record(
                self.session,
                ctx,
                rctx,
                "invoice.line_updated",
                target_type="invoice",
                target_id=invoice.id,
                meta={**_meta(invoice, client), "old": before, "new": _line_meta(line)},
            )
            detail = await self._detail(ctx, invoice)
        return detail

    async def delete_line(
        self, ctx: OrgContext, invoice_id: uuid.UUID, line_id: uuid.UUID, rctx: RequestContext
    ) -> InvoiceRead:
        async with unit_of_work(self.session):
            invoice = await self._locked_draft(ctx, invoice_id)
            line = await self._line(ctx, invoice, line_id)
            allocations = await self.wip.allocations_for_line(ctx.organization_id, line.id)
            await self._release(ctx, allocations)
            meta_line = _line_meta(line)
            await self.invoices.delete_line(line)
            await self._recompute(invoice)
            client = await self.refs.get_client(ctx.organization_id, invoice.client_id)
            await record(
                self.session,
                ctx,
                rctx,
                "invoice.line_removed",
                target_type="invoice",
                target_id=invoice.id,
                meta={**_meta(invoice, client), "line": meta_line},
            )
            detail = await self._detail(ctx, invoice)
        return detail

    async def delete_draft(
        self, ctx: OrgContext, invoice_id: uuid.UUID, rctx: RequestContext
    ) -> None:
        async with unit_of_work(self.session):
            invoice = await self._locked_draft(ctx, invoice_id)
            allocations = await self.wip.allocations_for_invoice(ctx.organization_id, invoice.id)
            await self._release(ctx, allocations)
            client = await self.refs.get_client(ctx.organization_id, invoice.client_id)
            meta = _meta(invoice, client, item_count=len(allocations))
            await self.invoices.delete(invoice)
            await record(
                self.session,
                ctx,
                rctx,
                "invoice.draft_deleted",
                target_type="invoice",
                target_id=invoice_id,
                meta=meta,
            )

    async def bulk(
        self, ctx: OrgContext, data: InvoiceBulkAction, rctx: RequestContext
    ) -> BulkChangeResult:
        """Delete up to 200 drafts, all or nothing: one invoice that is not a draft (409
        ``INVOICE_NOT_DRAFT``) stops the whole action, which then deletes nothing."""
        policy.ensure(ctx, "invoice.create")
        org_id = ctx.organization_id
        ids = unique_ids(data.invoice_ids)
        async with unit_of_work(self.session):
            invoices = await self.invoices.lock_many(org_id, ids)
            if len(invoices) != len(ids):
                found = {i.id for i in invoices}
                raise NotFoundError(
                    "Some of the invoices were not found.",
                    details={"invoice_ids": [str(i) for i in ids if i not in found]},
                )
            clients = await self.refs.clients_by_ids(org_id, {i.client_id for i in invoices})
            locked = [i for i in invoices if i.status != "draft"]
            if locked:
                raise conflict(
                    "INVOICE_NOT_DRAFT",
                    f"{len(locked)} of the invoices {'are' if len(locked) != 1 else 'is'} not a "
                    "draft and cannot be deleted. Nothing was deleted.",
                    details=[
                        {
                            "invoice_id": str(i.id),
                            "number": i.number,
                            "status": i.status,
                            "reason": "Only a draft can be deleted: correct an issued invoice "
                            "with a credit note.",
                        }
                        for i in locked
                    ],
                )
            rows: list[AuditRow] = []
            for invoice in invoices:
                allocations = await self.wip.allocations_for_invoice(org_id, invoice.id)
                await self._release(ctx, allocations)
                meta = _meta(
                    invoice,
                    clients.get(invoice.client_id),
                    item_count=len(allocations),
                    bulk=True,
                )
                await self.invoices.delete(invoice)
                rows.append(("invoice.draft_deleted", "invoice", invoice.id, meta))
            await record_audit_many(self.session, ctx, rctx, rows)
        return BulkChangeResult(action=data.action, affected=len(ids), changed=len(rows))

    # --- Approval ----------------------------------------------------------------------

    async def submit(
        self, ctx: OrgContext, invoice_id: uuid.UUID, data: InvoiceSubmit, rctx: RequestContext
    ) -> InvoiceRead:
        async with unit_of_work(self.session):
            invoice = await self._locked_draft(ctx, invoice_id)
            if not await self.invoices.lines(ctx.organization_id, invoice.id):
                raise conflict("INVOICE_EMPTY", "An invoice needs at least one line.")
            await self._recompute(invoice)
            if not approval_needed(ctx.organization, invoice):
                raise conflict(
                    "APPROVAL_NOT_REQUIRED",
                    "This invoice does not need approval: issue it directly.",
                    details={
                        "gross_total": invoice.gross_total,
                        "required": ctx.organization.invoice_approval_required,
                        "threshold": ctx.organization.invoice_approval_threshold,
                    },
                )
            client = await get_client_or_404(self.refs, ctx.organization_id, invoice.client_id)
            invoice.status = "awaiting_approval"
            await self.session.flush()
            step = await self._add_approval(ctx, invoice, "submitted", data.note)
            await record(
                self.session,
                ctx,
                rctx,
                "invoice.submitted",
                target_type="invoice",
                target_id=invoice.id,
                meta=_meta(invoice, client, note=data.note),
            )
            await billing_notifications.invoice_approval_pending(
                self.session,
                organization_id=ctx.organization_id,
                invoice_id=invoice.id,
                client_name=client.name,
                invoice_total=invoice_pdf.money(invoice.gross_total, invoice.currency),
                submitter_id=ctx.user.id,
                submitter_name=ctx.user.full_name,
                creator_id=invoice.created_by,
                submitted_at=step.created_at,
            )
            detail = await self._detail(ctx, invoice)
        return detail

    async def approve(
        self, ctx: OrgContext, invoice_id: uuid.UUID, data: InvoiceApprove, rctx: RequestContext
    ) -> InvoiceRead:
        async with unit_of_work(self.session):
            invoice = await self._get(ctx, invoice_id, lock=True)
            if invoice.status != "awaiting_approval":
                raise conflict(
                    "INVOICE_NOT_AWAITING_APPROVAL",
                    "Only an invoice awaiting approval can be approved.",
                    details={"status": invoice.status},
                )
            approvals = await self.invoices.approvals(ctx.organization_id, invoice.id)
            if not policy.is_owner(ctx) and ctx.user.id in preparers(invoice, approvals):
                raise conflict(
                    "SELF_APPROVAL_NOT_ALLOWED",
                    "Someone else must approve an invoice you prepared or submitted.",
                )
            client = await get_client_or_404(self.refs, ctx.organization_id, invoice.client_id)
            invoice.status = "approved"
            await self.session.flush()
            step = await self._add_approval(ctx, invoice, "approved", data.note)
            await record(
                self.session,
                ctx,
                rctx,
                "invoice.approved",
                target_type="invoice",
                target_id=invoice.id,
                meta=_meta(invoice, client, note=data.note),
            )
            await billing_notifications.invoice_approved(
                self.session,
                organization_id=ctx.organization_id,
                invoice_id=invoice.id,
                recipients=self._preparer_recipients(ctx, invoice, approvals),
                client_name=client.name,
                invoice_total=invoice_pdf.money(invoice.gross_total, invoice.currency),
                decider_name=ctx.user.full_name,
                decided_at=step.created_at,
            )
            detail = await self._detail(ctx, invoice)
        return detail

    async def reject(
        self, ctx: OrgContext, invoice_id: uuid.UUID, data: InvoiceReject, rctx: RequestContext
    ) -> InvoiceRead:
        async with unit_of_work(self.session):
            invoice = await self._get(ctx, invoice_id, lock=True)
            if invoice.status not in ("awaiting_approval", "approved"):
                raise conflict(
                    "INVOICE_NOT_AWAITING_APPROVAL",
                    "Only an invoice awaiting approval, or approved but not yet issued, can be "
                    "rejected.",
                    details={"status": invoice.status},
                )
            approvals = await self.invoices.approvals(ctx.organization_id, invoice.id)
            client = await get_client_or_404(self.refs, ctx.organization_id, invoice.client_id)
            was = invoice.status
            invoice.status = "draft"
            await self.session.flush()
            step = await self._add_approval(ctx, invoice, "rejected", data.reason)
            await record(
                self.session,
                ctx,
                rctx,
                "invoice.rejected",
                target_type="invoice",
                target_id=invoice.id,
                meta=_meta(invoice, client, reason=data.reason, was=was),
            )
            await billing_notifications.invoice_rejected(
                self.session,
                organization_id=ctx.organization_id,
                invoice_id=invoice.id,
                recipients=self._preparer_recipients(ctx, invoice, approvals),
                client_name=client.name,
                invoice_total=invoice_pdf.money(invoice.gross_total, invoice.currency),
                decider_name=ctx.user.full_name,
                reason=data.reason,
                decided_at=step.created_at,
            )
            detail = await self._detail(ctx, invoice)
        return detail

    async def _add_approval(
        self, ctx: OrgContext, invoice: Invoice, action: str, note: str | None
    ) -> InvoiceApproval:
        return await self.invoices.add_approval(
            InvoiceApproval(
                id=new_id(),
                organization_id=ctx.organization_id,
                invoice_id=invoice.id,
                action=action,
                actor_user_id=ctx.user.id,
                note=note,
                gross_total=invoice.gross_total,
            )
        )

    @staticmethod
    def _preparer_recipients(
        ctx: OrgContext, invoice: Invoice, approvals: Sequence[InvoiceApproval]
    ) -> list[uuid.UUID]:
        """Who hears of a decision: the people who prepared and submitted the invoice, never the
        person who decided."""
        return sorted(preparers(invoice, approvals) - {ctx.user.id})

    # --- Issue and void ----------------------------------------------------------------

    async def issue(
        self, ctx: OrgContext, invoice_id: uuid.UUID, data: InvoiceIssue, rctx: RequestContext
    ) -> InvoiceRead:
        organization = ctx.organization
        async with unit_of_work(self.session):
            invoice = await self._get(ctx, invoice_id, lock=True)
            if invoice.status == "awaiting_approval":
                raise self._approval_required(organization, invoice)
            if invoice.status not in ("draft", "approved"):
                raise conflict("INVOICE_NOT_DRAFT", "Only a draft invoice can be changed this way.")
            lines = await self.invoices.lines(ctx.organization_id, invoice.id)
            if not lines:
                raise conflict("INVOICE_EMPTY", "An invoice needs at least one line.")
            await self._recompute(invoice)
            if invoice.gross_total < 0:
                raise conflict("INVOICE_TOTAL_NEGATIVE", "The invoice total cannot be negative.")
            if invoice.status == "draft" and approval_needed(organization, invoice):
                raise self._approval_required(organization, invoice)
            client = await get_client_or_404(self.refs, ctx.organization_id, invoice.client_id)
            issue_date = data.issue_date or invoice.issue_date or today_for(organization)
            terms = (
                client.payment_terms_days
                if client.payment_terms_days is not None
                else organization.payment_terms_days
            )
            due_date = data.due_date or (
                invoice.due_date
                if invoice.due_date and invoice.due_date >= issue_date
                else issue_date + timedelta(days=terms)
            )
            if due_date < issue_date:
                raise ValidationError.for_field(
                    "due_date", "The due date is before the issue date."
                )
            allocations = await self.wip.allocations_for_invoice(ctx.organization_id, invoice.id)
            items = await lock_items(
                self.session,
                ctx.organization_id,
                [a.time_entry_id for a in allocations if a.time_entry_id],
                [a.disbursement_id for a in allocations if a.disbursement_id],
            )
            set_items_status(items, "billed")
            invoice.number = await next_number(self.session, ctx.organization_id, "invoice")
            invoice.status = "issued"
            invoice.issue_date = issue_date
            invoice.due_date = due_date
            invoice.issued_at = now_utc()
            invoice.issued_by = ctx.user.id
            await self.session.flush()
            wip_value = sum(a.wip_value for a in allocations)
            await record(
                self.session,
                ctx,
                rctx,
                "invoice.issued",
                target_type="invoice",
                target_id=invoice.id,
                meta={
                    **_meta(invoice, client, wip_value=wip_value, item_count=items.count),
                    "issue_date": issue_date.isoformat(),
                    "due_date": due_date.isoformat(),
                    "write_up_off": invoice_adjustment(allocations),
                },
            )
            detail = await self._detail(ctx, invoice)
        return detail

    async def void(
        self, ctx: OrgContext, invoice_id: uuid.UUID, data: InvoiceVoid, rctx: RequestContext
    ) -> InvoiceRead:
        async with unit_of_work(self.session):
            invoice = await self._get(ctx, invoice_id, lock=True)
            if invoice.status != "issued":
                raise conflict("INVOICE_NOT_ISSUED", "Only an issued invoice can be voided.")
            if invoice.paid_total > 0:
                raise conflict(
                    "INVOICE_HAS_PAYMENTS",
                    "Remove the payments allocated to this invoice before voiding it.",
                )
            if invoice.credited_total > 0:
                raise conflict(
                    "INVOICE_HAS_CREDITS", "An invoice with credit notes cannot be voided."
                )
            allocations = [
                a
                for a in await self.wip.allocations_for_invoice(ctx.organization_id, invoice.id)
                if a.reversed_at is None
            ]
            items = await lock_items(
                self.session,
                ctx.organization_id,
                [a.time_entry_id for a in allocations if a.time_entry_id],
                [a.disbursement_id for a in allocations if a.disbursement_id],
            )
            set_items_status(items, "unbilled")
            now = now_utc()
            await self.session.flush()
            await self.wip.reverse_allocations(
                ctx.organization_id, [a.id for a in allocations], now
            )
            invoice.status = "void"
            invoice.voided_at = now
            invoice.voided_by = ctx.user.id
            invoice.void_reason = data.reason
            await self.session.flush()
            client = await self.refs.get_client(ctx.organization_id, invoice.client_id)
            await record(
                self.session,
                ctx,
                rctx,
                "invoice.voided",
                target_type="invoice",
                target_id=invoice.id,
                meta={
                    **_meta(invoice, client, item_count=items.count),
                    "reason": data.reason,
                    "wip_reinstated": items.wip_value,
                },
            )
            detail = await self._detail(ctx, invoice)
        return detail

    # --- PDF and email -----------------------------------------------------------------

    async def pdf(
        self, ctx: OrgContext, invoice_id: uuid.UUID, template_id: uuid.UUID | None = None
    ) -> tuple[str, bytes]:
        """The invoice PDF, drawn with the client's invoice template (else the practice
        default); ``template_id`` previews another one."""
        invoice = await self._get(ctx, invoice_id)
        if template_id is not None:
            template = await InvoiceTemplateRepository(self.session).get(
                ctx.organization_id, template_id
            )
            if template is None or (
                template.status != "active" and not policy.has_permission(ctx, "settings.manage")
            ):
                raise NotFoundError("Invoice template not found.")
        content = await render_invoice_pdf(self.session, ctx.organization, invoice, template_id)
        return f"{invoice.number or 'draft-' + str(invoice.id)[:8]}.pdf", content

    async def send(
        self, ctx: OrgContext, invoice_id: uuid.UUID, data: InvoiceSend, rctx: RequestContext
    ) -> InvoiceSent:
        async with unit_of_work(self.session):
            invoice = await self._get(ctx, invoice_id, lock=True)
            if invoice.status != "issued":
                raise conflict("INVOICE_NOT_ISSUED", "Only an issued invoice can be sent.")
            client = await get_client_or_404(self.refs, ctx.organization_id, invoice.client_id)
            to = [str(a) for a in data.to] if data.to else await self._recipients(ctx, client)
            if not to:
                raise invalid(
                    "NO_RECIPIENT",
                    "to",
                    "The client has no billing contact or email: give the addresses to send to.",
                )
            queued = await job_queue.enqueue(
                self.session,
                SEND_JOB,
                {
                    "organization_id": str(ctx.organization_id),
                    "invoice_id": str(invoice.id),
                    "to": to,
                    "requested_at": now_utc().isoformat(),
                    "requested_by": str(ctx.user.id),
                },
                organization_id=ctx.organization_id,
                dedupe_key=f"{SEND_JOB}:{invoice.id}",
            )
            if queued:
                await record(
                    self.session,
                    ctx,
                    rctx,
                    "invoice.send_queued",
                    target_type="invoice",
                    target_id=invoice.id,
                    meta={**_meta(invoice, client), "to": to},
                )
        return InvoiceSent(invoice_id=invoice.id, to=to, queued=queued)

    async def _recipients(self, ctx: OrgContext, client: Client) -> list[str]:
        return await billing_recipients(self.refs, ctx.organization_id, client)

    # --- Internals ---------------------------------------------------------------------

    async def _locked_draft(self, ctx: OrgContext, invoice_id: uuid.UUID) -> Invoice:
        invoice = await self._get(ctx, invoice_id, lock=True)
        if invoice.status != "draft":
            raise conflict("INVOICE_NOT_DRAFT", "Only a draft invoice can be changed this way.")
        return invoice

    @staticmethod
    def _approval_required(organization: Organization, invoice: Invoice) -> ConflictError:
        return conflict(
            "INVOICE_APPROVAL_REQUIRED",
            "This invoice needs approval before it can be issued.",
            details={
                "status": invoice.status,
                "gross_total": invoice.gross_total,
                "threshold": organization.invoice_approval_threshold,
            },
        )

    async def _line(self, ctx: OrgContext, invoice: Invoice, line_id: uuid.UUID) -> InvoiceLine:
        line = await self.invoices.get_line(ctx.organization_id, invoice.id, line_id)
        if line is None:
            raise NotFoundError("Invoice line not found.")
        return line

    async def _plan(
        self, ctx: OrgContext, items: LockedItems, data: WipSelection
    ) -> list[_PlannedLine]:
        jobs = await self.wip.jobs_by_ids(
            ctx.organization_id,
            [e.job_id for e in items.time_entries] + [d.job_id for d in items.disbursements],
        )
        fixed_ids = [j.id for j in jobs.values() if j.billing_basis == "fixed"]
        # Read what is already billed of each fee only once no other draft can be adding
        # a fixed-fee line for the same job (it commits, then this statement sees it).
        await self.wip.lock_fixed_fee_jobs(ctx.organization_id, fixed_ids)
        already = await self.wip.fixed_fee_billed(ctx.organization_id, fixed_ids)
        planned = plan_lines(items, jobs, data.group_by, already)
        apply_overrides(planned, data.overrides)
        return planned

    def _allocations(
        self, ctx: OrgContext, invoice: Invoice, line_id: uuid.UUID, line: _PlannedLine
    ) -> list[WipAllocation]:
        return [
            WipAllocation(
                id=new_id(),
                organization_id=ctx.organization_id,
                time_entry_id=e.id,
                invoice_id=invoice.id,
                invoice_line_id=line_id,
                wip_value=e.charge_value,
                billed_value=0,
            )
            for e in line.time_entries
        ] + [
            WipAllocation(
                id=new_id(),
                organization_id=ctx.organization_id,
                disbursement_id=d.id,
                invoice_id=invoice.id,
                invoice_line_id=line_id,
                wip_value=d.amount,
                billed_value=0,
            )
            for d in line.disbursements
        ]

    async def _write_lines(
        self,
        ctx: OrgContext,
        invoice: Invoice,
        client: Client,
        planned: Sequence[_PlannedLine],
        *,
        start_position: int,
    ) -> None:
        if not planned:
            return
        resolver = VatResolver(self.refs, ctx.organization)
        lines: list[InvoiceLine] = []
        allocations: list[WipAllocation] = []
        for offset, plan in enumerate(planned):
            if plan.net_amount < 0:
                raise invalid(
                    "NEGATIVE_WIP_LINE", "overrides", "A line that bills WIP cannot be negative."
                )
            rate_id, rate_bp = await resolver.resolve(
                client=client,
                line_rate_id=plan.line_rate_id,
                service_id=plan.service_id,
                validate_line_rate=plan.validate_rate,
            )
            line = InvoiceLine(
                id=new_id(),
                organization_id=ctx.organization_id,
                invoice_id=invoice.id,
                position=start_position + offset,
                kind=plan.kind,
                description=plan.description,
                job_id=plan.job_id,
                service_id=plan.service_id,
                quantity=plan.quantity,
                unit_amount=plan.unit_amount,
                net_amount=plan.net_amount,
                vat_rate_id=rate_id,
                vat_rate_bp=rate_bp,
                vat_amount=vat_on(plan.net_amount, rate_bp),
            )
            lines.append(line)
            mine = self._allocations(ctx, invoice, line.id, plan)
            apportion_allocations(mine, plan.net_amount)
            allocations += mine
        await self.invoices.add_lines(lines)
        await self.wip.add_allocations(allocations)

    async def _reapportion(self, ctx: OrgContext, line: InvoiceLine) -> None:
        allocations = await self.wip.allocations_for_line(ctx.organization_id, line.id)
        if allocations:
            apportion_allocations(allocations, line.net_amount)
            await self.session.flush()

    async def _release(self, ctx: OrgContext, allocations: Sequence[WipAllocation]) -> None:
        """Take items off a draft: allocation rows deleted, items back to ``unbilled``."""
        items = await lock_items(
            self.session,
            ctx.organization_id,
            [a.time_entry_id for a in allocations if a.time_entry_id],
            [a.disbursement_id for a in allocations if a.disbursement_id],
        )
        set_items_status(items, "unbilled")
        await self.session.flush()
        await self.wip.delete_allocations(ctx.organization_id, [a.id for a in allocations])

    async def _recompute(self, invoice: Invoice) -> None:
        """Totals are the sums of the lines. Call with the invoice row locked."""
        lines = await self.invoices.lines(invoice.organization_id, invoice.id)
        net = sum(line.net_amount for line in lines)
        vat = sum(line.vat_amount for line in lines)
        if net + vat < 0:
            raise invalid(
                "INVOICE_TOTAL_NEGATIVE", "lines", "The invoice total cannot be negative."
            )
        invoice.net_total, invoice.vat_total, invoice.gross_total = net, vat, net + vat
        await self.session.flush()


def vat_summary(lines: Sequence[InvoiceLine]) -> list[tuple[int, int, int]]:
    """``(rate_bp, net, vat)`` per VAT rate, highest rate first."""
    totals: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for line in lines:
        totals[line.vat_rate_bp][0] += line.net_amount
        totals[line.vat_rate_bp][1] += line.vat_amount
    return [(bp, net, vat) for bp, (net, vat) in sorted(totals.items(), reverse=True)]


def invoice_adjustment(allocations: Sequence[WipAllocation]) -> int:
    return sum(a.billed_value - a.wip_value for a in allocations)


def _plain(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, date | datetime):
        return value.isoformat()
    return value


def _meta(invoice: Invoice, client: Client | None, **extra: Any) -> dict[str, Any]:
    return {
        "number": invoice.number,
        "status": invoice.status,
        "client_id": str(invoice.client_id),
        "client_name": client.name if client else None,
        "net_total": invoice.net_total,
        "vat_total": invoice.vat_total,
        "gross_total": invoice.gross_total,
        **extra,
    }


def _line_meta(line: InvoiceLine) -> dict[str, Any]:
    return {
        "id": str(line.id),
        "kind": line.kind,
        "description": line.description,
        "quantity": str(line.quantity),
        "unit_amount": line.unit_amount,
        "net_amount": line.net_amount,
        "vat_rate_bp": line.vat_rate_bp,
        "vat_amount": line.vat_amount,
    }


async def addressee_for(
    refs: BillingReferenceRepository, organization_id: uuid.UUID, client: Client
) -> str | None:
    contacts = await refs.client_contacts(organization_id, client.id)
    billing = [c for c in contacts if c.is_billing]
    primary = [c for c in contacts if c.is_primary]
    chosen = billing[0] if billing else (primary[0] if primary else None)
    return chosen.name if chosen else None


async def billing_recipients(
    refs: BillingReferenceRepository, organization_id: uuid.UUID, client: Client
) -> list[str]:
    """Where a client's invoices, reminders and statements go: its invoice email, else its
    billing contacts, else its primary contact, else the client's own email."""
    if client.invoice_email:
        return [client.invoice_email]
    contacts = await refs.client_contacts(organization_id, client.id)
    billing = [c.email for c in contacts if c.is_billing and c.email]
    if billing:
        return list(dict.fromkeys(billing))
    primary = [c.email for c in contacts if c.is_primary and c.email]
    if primary:
        return primary[:1]
    return [client.email] if client.email else []


async def render_invoice_pdf(
    session: AsyncSession,
    organization: Organization,
    invoice: Invoice,
    template_id: uuid.UUID | None = None,
) -> bytes:
    """The invoice PDF in the client's invoice template (else the practice default), or in
    ``template_id`` when given (a preview)."""
    refs = BillingReferenceRepository(session)
    client = await refs.get_client(organization.id, invoice.client_id)
    if client is None:
        raise NotFoundError("Client not found.")
    lines = await InvoiceRepository(session).lines(organization.id, invoice.id)
    template = await document_templates.pdf_template(session, organization.id, client, template_id)
    details = None
    names = None
    if template.layout == "detailed":
        details = await document_templates.line_details(session, organization.id, invoice.id)
    elif template.layout == "summary":
        names = await document_templates.job_names(session, organization.id, lines)
    return invoice_pdf.render_invoice(
        organization=organization,
        client=client,
        addressee=await addressee_for(refs, organization.id, client),
        invoice=invoice,
        lines=lines,
        vat_summary=vat_summary(lines),
        template=template,
        details=details,
        job_names=names,
    )


async def deliver_invoice_email(
    session: AsyncSession, settings: Settings, payload: dict[str, Any]
) -> dict[str, Any]:
    """The ``send_invoice_email`` job: render the PDF and email it, then set ``sent_at``.

    Idempotent: a job whose request was already delivered (``sent_at`` at or after the
    request time) sends nothing.
    """
    try:
        organization_id = uuid.UUID(str(payload["organization_id"]))
        invoice_id = uuid.UUID(str(payload["invoice_id"]))
        to = [str(a) for a in payload["to"]]
        requested_at = datetime.fromisoformat(str(payload["requested_at"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise InvoiceEmailError(f"bad payload: {exc}") from exc
    if not to:
        raise InvoiceEmailError("no recipients")
    organization = await BillingReferenceRepository(session).get_organization(organization_id)
    if organization is None:
        raise InvoiceEmailError("organization not found")
    invoices = InvoiceRepository(session)
    # No row lock while talking to SMTP: payments on the invoice must not wait for it.
    invoice = await invoices.get(organization_id, invoice_id)
    if invoice is None or invoice.status != "issued":
        raise InvoiceEmailError("invoice not found or not issued")
    if invoice.sent_at is not None and invoice.sent_at >= requested_at:
        return {**payload, "skipped": "already_sent"}
    refs = BillingReferenceRepository(session)
    client = await refs.get_client(organization_id, invoice.client_id)
    if client is None:
        raise InvoiceEmailError("client not found")
    content = await render_invoice_pdf(session, organization, invoice)
    template = await document_templates.pdf_template(session, organization_id, client)
    amount_due = invoice_pdf.money(invoice.balance, invoice.currency)
    await EmailService(settings).send_template(
        "invoice",
        to=", ".join(to),
        name=await addressee_for(refs, organization_id, client) or "",
        attachments=[Attachment(f"{invoice.number}.pdf", content, "application/pdf")],
        override=await load_override(session, organization_id, "invoice"),
        organization_id=organization_id,
        invoice_number=invoice.number,
        practice_name=organization.name,
        client_name=client.name,
        issue_date=invoice_pdf.long_date(invoice.issue_date),
        due_date=invoice_pdf.long_date(invoice.due_date),
        amount_due=amount_due,
        bank_details=(organization.bank_details or "") if template.show_bank_details else "",
        footer_text=template.footer_text or "",
    )
    locked = await invoices.get(organization_id, invoice_id, for_update=True)
    assert locked is not None
    locked.sent_at = now_utc()
    await session.flush()
    if payload.get("requested_by"):
        await notification_events.invoice_delivery(
            session,
            organization_id=organization_id,
            sender_id=uuid.UUID(str(payload["requested_by"])),
            invoice_id=invoice_id,
            invoice_number=invoice.number or "",
            client_name=client.name,
            amount_due=amount_due,
            sent_to=to,
            request_key=requested_at.isoformat(),
        )
    return {**payload, "sent_at": locked.sent_at.isoformat()}
