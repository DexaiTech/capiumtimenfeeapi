"""Estimates: write, send (numbered), accept or decline, convert to a draft invoice.

A draft changes freely; its lines are replaced as a whole and priced when written. Sending
takes the next number of the ``estimate`` sequence and fixes the dates. An estimate is
``expired`` when sent and past its expiry date (derived, never stored); an expired estimate
can still be accepted, since the practice may honour it. Converting copies the lines into a
draft invoice (409 ``ESTIMATE_ALREADY_CONVERTED`` the second time).
"""

import uuid
from collections.abc import Sequence
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import NotFoundError, ValidationError
from app.db.models import Client, Estimate, EstimateLine
from app.db.session import unit_of_work
from app.repositories.billing_common import BillingReferenceRepository
from app.repositories.estimates import EstimateFilters, EstimateRepository
from app.repositories.invoices import InvoiceRepository
from app.schemas.estimates import (
    EstimateConverted,
    EstimateCreate,
    EstimateDisplayStatus,
    EstimateLineRead,
    EstimateListSummary,
    EstimateRead,
    EstimateSend,
    EstimateStatusCounts,
    EstimateSummaryRead,
    EstimateUpdate,
)
from app.services import billing_drafts
from app.services.billing_common import (
    conflict,
    get_client_or_404,
    now_utc,
    record,
    today_for,
)
from app.services.billing_drafts import PricedLine, price_lines, totals
from app.services.policy import OrgContext
from app.services.sequences import next_number
from app.utils.ids import new_id
from app.utils.pagination import PageParams


def display_status(estimate: Estimate, today: date) -> EstimateDisplayStatus:
    if (
        estimate.status == "sent"
        and estimate.expiry_date is not None
        and estimate.expiry_date < today
    ):
        return "expired"
    return estimate.status  # type: ignore[return-value]


def _meta(estimate: Estimate, client: Client | None, **extra: object) -> dict[str, object]:
    return {
        "number": estimate.number,
        "client_id": str(estimate.client_id),
        "client": client.name if client else None,
        "gross_total": estimate.gross_total,
        **extra,
    }


class EstimateService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.estimates = EstimateRepository(session)
        self.invoices = InvoiceRepository(session)
        self.refs = BillingReferenceRepository(session)

    # --- Reads -------------------------------------------------------------------------

    async def list_estimates(
        self, ctx: OrgContext, filters: EstimateFilters, params: PageParams
    ) -> tuple[list[EstimateSummaryRead], str | None]:
        today = today_for(ctx.organization)
        page = await self.estimates.list_page(
            ctx.organization_id, filters, today=today, params=params
        )
        clients = await self.refs.clients_by_ids(
            ctx.organization_id, [e.client_id for e in page.items]
        )
        items = [
            EstimateSummaryRead.build(e, clients.get(e.client_id), display_status(e, today))
            for e in page.items
        ]
        return items, page.next_cursor

    async def list_summary(self, ctx: OrgContext, filters: EstimateFilters) -> EstimateListSummary:
        figures = await self.estimates.summary(
            ctx.organization_id, filters, today=today_for(ctx.organization)
        )
        by = figures.by_status
        return EstimateListSummary(
            counts=EstimateStatusCounts(
                draft=by["draft"],
                sent=by["sent"],
                accepted=by["accepted"],
                declined=by["declined"],
                expired=by["expired"],
                total=figures.total,
            ),
            open_gross=figures.open_gross,
        )

    async def get(self, ctx: OrgContext, estimate_id: uuid.UUID) -> EstimateRead:
        return await self._detail(ctx, await self._get(ctx, estimate_id))

    async def _get(self, ctx: OrgContext, estimate_id: uuid.UUID, lock: bool = False) -> Estimate:
        estimate = await self.estimates.get(ctx.organization_id, estimate_id, for_update=lock)
        if estimate is None:
            raise NotFoundError("Estimate not found.")
        return estimate

    async def _detail(self, ctx: OrgContext, estimate: Estimate) -> EstimateRead:
        client = await self.refs.get_client(ctx.organization_id, estimate.client_id)
        lines = await self.estimates.lines(ctx.organization_id, estimate.id)
        summary = EstimateSummaryRead.build(
            estimate, client, display_status(estimate, today_for(ctx.organization))
        )
        return EstimateRead(
            **summary.model_dump(),
            notes=estimate.notes,
            lines=[EstimateLineRead.from_model(line) for line in lines],
        )

    # --- Writes ------------------------------------------------------------------------

    async def create(
        self, ctx: OrgContext, data: EstimateCreate, rctx: RequestContext
    ) -> EstimateRead:
        organization = ctx.organization
        async with unit_of_work(self.session):
            client = await get_client_or_404(self.refs, ctx.organization_id, data.client_id)
            await self._check_job(ctx, client, data.job_id)
            priced = await price_lines(self.refs, organization, client, data.lines)
            net, vat, gross = totals(priced)
            self._check_total(gross)
            estimate = await self.estimates.add(
                Estimate(
                    organization_id=ctx.organization_id,
                    client_id=client.id,
                    job_id=data.job_id,
                    status="draft",
                    expiry_date=data.expiry_date,
                    reference=data.reference,
                    notes=data.notes,
                    currency=organization.currency,
                    net_total=net,
                    vat_total=vat,
                    gross_total=gross,
                    created_by=ctx.user.id,
                )
            )
            await self.estimates.replace_lines(
                ctx.organization_id, estimate.id, self._rows(ctx, estimate, priced)
            )
            await record(
                self.session,
                ctx,
                rctx,
                "estimate.created",
                target_type="estimate",
                target_id=estimate.id,
                meta=_meta(estimate, client),
            )
            detail = await self._detail(ctx, estimate)
        return detail

    async def update(
        self,
        ctx: OrgContext,
        estimate_id: uuid.UUID,
        data: EstimateUpdate,
        rctx: RequestContext,
    ) -> EstimateRead:
        async with unit_of_work(self.session):
            estimate = await self._draft(ctx, estimate_id)
            client = await get_client_or_404(self.refs, ctx.organization_id, estimate.client_id)
            changed = data.model_fields_set
            if "job_id" in changed:
                await self._check_job(ctx, client, data.job_id)
                estimate.job_id = data.job_id
            if "expiry_date" in changed:
                estimate.expiry_date = data.expiry_date
            if "reference" in changed:
                estimate.reference = data.reference
            if "notes" in changed:
                estimate.notes = data.notes
            if data.lines is not None:
                priced = await price_lines(self.refs, ctx.organization, client, data.lines)
                net, vat, gross = totals(priced)
                self._check_total(gross)
                await self.estimates.replace_lines(
                    ctx.organization_id, estimate.id, self._rows(ctx, estimate, priced)
                )
                estimate.net_total, estimate.vat_total, estimate.gross_total = net, vat, gross
            await self.session.flush()
            await record(
                self.session,
                ctx,
                rctx,
                "estimate.updated",
                target_type="estimate",
                target_id=estimate.id,
                meta=_meta(estimate, client, fields=sorted(changed)),
            )
            detail = await self._detail(ctx, estimate)
        return detail

    async def send(
        self,
        ctx: OrgContext,
        estimate_id: uuid.UUID,
        data: EstimateSend,
        rctx: RequestContext,
    ) -> EstimateRead:
        """Number the draft and mark it sent. (Delivery by email is not wired yet: the number
        and the ``sent`` status are what the practice shares.)"""
        async with unit_of_work(self.session):
            estimate = await self._draft(ctx, estimate_id)
            lines = await self.estimates.lines(ctx.organization_id, estimate.id)
            if not lines:
                raise conflict("ESTIMATE_EMPTY", "An estimate needs at least one line.")
            issue_date = data.issue_date or today_for(ctx.organization)
            expiry_date = data.expiry_date or estimate.expiry_date
            if expiry_date is not None and expiry_date < issue_date:
                raise ValidationError.for_field(
                    "expiry_date", "The expiry date is before the issue date."
                )
            client = await get_client_or_404(self.refs, ctx.organization_id, estimate.client_id)
            estimate.number = await next_number(self.session, ctx.organization_id, "estimate")
            estimate.status = "sent"
            estimate.issue_date = issue_date
            estimate.expiry_date = expiry_date
            estimate.sent_at = now_utc()
            await self.session.flush()
            await record(
                self.session,
                ctx,
                rctx,
                "estimate.sent",
                target_type="estimate",
                target_id=estimate.id,
                meta=_meta(estimate, client),
            )
            detail = await self._detail(ctx, estimate)
        return detail

    async def respond(
        self,
        ctx: OrgContext,
        estimate_id: uuid.UUID,
        accepted: bool,
        rctx: RequestContext,
    ) -> EstimateRead:
        """Record the client's answer to a sent estimate."""
        async with unit_of_work(self.session):
            estimate = await self._get(ctx, estimate_id, lock=True)
            if estimate.status != "sent":
                raise conflict(
                    "ESTIMATE_NOT_SENT", "Only an estimate that was sent can be answered."
                )
            client = await self.refs.get_client(ctx.organization_id, estimate.client_id)
            estimate.status = "accepted" if accepted else "declined"
            estimate.responded_at = now_utc()
            await self.session.flush()
            await record(
                self.session,
                ctx,
                rctx,
                "estimate.accepted" if accepted else "estimate.declined",
                target_type="estimate",
                target_id=estimate.id,
                meta=_meta(estimate, client),
            )
            detail = await self._detail(ctx, estimate)
        return detail

    async def convert(
        self, ctx: OrgContext, estimate_id: uuid.UUID, rctx: RequestContext
    ) -> EstimateConverted:
        """Raise a draft invoice from the estimate's lines. Needs ``invoice.create`` too."""
        async with unit_of_work(self.session):
            estimate = await self._get(ctx, estimate_id, lock=True)
            if estimate.converted_invoice_id is not None:
                raise conflict(
                    "ESTIMATE_ALREADY_CONVERTED", "This estimate already became an invoice."
                )
            if estimate.status not in ("sent", "accepted"):
                raise conflict(
                    "ESTIMATE_NOT_CONVERTIBLE",
                    "Only a sent or accepted estimate can become an invoice.",
                )
            client = await get_client_or_404(self.refs, ctx.organization_id, estimate.client_id)
            lines = await self.estimates.lines(ctx.organization_id, estimate.id)
            priced = [
                PricedLine(
                    position=line.position,
                    description=line.description,
                    job_id=line.job_id or estimate.job_id,
                    service_id=line.service_id,
                    quantity=line.quantity,
                    unit_amount=line.unit_amount,
                    net_amount=line.net_amount,
                    vat_rate_id=line.vat_rate_id,
                    vat_rate_bp=line.vat_rate_bp,
                    vat_amount=line.vat_amount,
                )
                for line in lines
            ]
            invoice = await billing_drafts.create_invoice_draft(
                self.invoices,
                ctx.organization,
                client,
                priced,
                created_by=ctx.user.id,
                reference=estimate.reference or estimate.number,
                notes=estimate.notes,
            )
            estimate.converted_invoice_id = invoice.id
            await self.session.flush()
            await record(
                self.session,
                ctx,
                rctx,
                "estimate.converted",
                target_type="estimate",
                target_id=estimate.id,
                meta=_meta(estimate, client, invoice_id=str(invoice.id)),
            )
            await record(
                self.session,
                ctx,
                rctx,
                "invoice.draft_created",
                target_type="invoice",
                target_id=invoice.id,
                meta={
                    "client_id": str(client.id),
                    "client": client.name,
                    "gross_total": invoice.gross_total,
                    "from_estimate": estimate.number,
                },
            )
            detail = await self._detail(ctx, estimate)
        return EstimateConverted(estimate=detail, invoice_id=invoice.id)

    async def delete(self, ctx: OrgContext, estimate_id: uuid.UUID, rctx: RequestContext) -> None:
        """Only a draft goes: a numbered estimate stays on record."""
        async with unit_of_work(self.session):
            estimate = await self._draft(ctx, estimate_id)
            client = await self.refs.get_client(ctx.organization_id, estimate.client_id)
            await record(
                self.session,
                ctx,
                rctx,
                "estimate.deleted",
                target_type="estimate",
                target_id=estimate.id,
                meta=_meta(estimate, client),
            )
            await self.estimates.delete(estimate)

    # --- Helpers -----------------------------------------------------------------------

    async def _draft(self, ctx: OrgContext, estimate_id: uuid.UUID) -> Estimate:
        estimate = await self._get(ctx, estimate_id, lock=True)
        if estimate.status != "draft":
            raise conflict("ESTIMATE_NOT_DRAFT", "Only a draft estimate can be changed.")
        return estimate

    async def _check_job(self, ctx: OrgContext, client: Client, job_id: uuid.UUID | None) -> None:
        if job_id is None:
            return
        job = await self.refs.get_job(ctx.organization_id, job_id)
        if job is None or job.client_id != client.id:
            raise ValidationError.for_field("job_id", "The job must be one of the client's jobs.")

    @staticmethod
    def _check_total(gross: int) -> None:
        if gross < 0:
            raise ValidationError.for_field("lines", "The estimate total cannot be negative.")

    @staticmethod
    def _rows(
        ctx: OrgContext, estimate: Estimate, priced: Sequence[PricedLine]
    ) -> list[EstimateLine]:
        return [
            EstimateLine(
                id=new_id(),
                organization_id=ctx.organization_id,
                estimate_id=estimate.id,
                position=line.position,
                description=line.description,
                job_id=line.job_id,
                service_id=line.service_id,
                quantity=line.quantity,
                unit_amount=line.unit_amount,
                net_amount=line.net_amount,
                vat_rate_id=line.vat_rate_id,
                vat_rate_bp=line.vat_rate_bp,
                vat_amount=line.vat_amount,
            )
            for line in priced
        ]
