"""Recurring invoice schedules: a template of lines that raises a draft invoice on a date.

A schedule runs on ``start_date`` and then every week, month, quarter or year from it (dates
are counted from the start, so 31 January goes to 28 February and back to 31 March). The daily
job (``run_due``) drafts one invoice per due date and moves ``next_run_date`` on; it never
issues anything: a person reviews and issues the draft. A run is one row per (schedule, date):
a delivered-twice job cannot raise two invoices, and a run that failed (an inactive client, a
deleted VAT rate) stays ``failed`` until someone retries it. A schedule that was paused or
down for a while does not flood: ``resume`` skips to the next date after today, while the job
catches up at most ``MAX_CATCH_UP`` missed dates per day.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

from dateutil.relativedelta import relativedelta
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.db.models import Client, Organization, RecurringLine, RecurringRun, RecurringSchedule
from app.db.session import unit_of_work
from app.repositories.billing_common import BillingReferenceRepository
from app.repositories.invoices import InvoiceRepository
from app.repositories.recurring import RecurringRepository, ScheduleFilters
from app.schemas.estimates import LineInput
from app.schemas.recurring import (
    RunRead,
    ScheduleCreate,
    ScheduleLineRead,
    ScheduleRead,
    ScheduleSummaryRead,
    ScheduleUpdate,
)
from app.services import audit, billing_drafts
from app.services.billing_common import conflict, get_client_or_404, record, today_for
from app.services.policy import OrgContext
from app.utils.ids import new_id
from app.utils.pagination import PageParams

log = get_logger(__name__)

MAX_CATCH_UP = 12
# (weeks, months) of one step.
STEP: dict[str, tuple[int, int]] = {
    "weekly": (1, 0),
    "monthly": (0, 1),
    "quarterly": (0, 3),
    "annually": (0, 12),
}


def occurrence(start: date, frequency: str, n: int) -> date:
    """The n-th date (0 = ``start``) of a schedule, counted from the start."""
    weeks, months = STEP[frequency]
    result: date = start + relativedelta(weeks=weeks * n, months=months * n)
    return result


def first_after(start: date, frequency: str, after: date) -> date:
    """The first date of the schedule strictly after ``after`` (``start`` itself if later)."""
    n = 0
    while (candidate := occurrence(start, frequency, n)) <= after:
        n += 1
    return candidate


def first_on_or_after(start: date, frequency: str, day: date) -> date:
    return first_after(start, frequency, day - timedelta(days=1))


def _meta(schedule: RecurringSchedule, client: Client | None, **extra: object) -> dict[str, object]:
    return {
        "name": schedule.name,
        "client_id": str(schedule.client_id),
        "client": client.name if client else None,
        "frequency": schedule.frequency,
        **extra,
    }


def _rows(
    organization_id: uuid.UUID, schedule_id: uuid.UUID, lines: Sequence[LineInput]
) -> list[RecurringLine]:
    return [
        RecurringLine(
            id=new_id(),
            organization_id=organization_id,
            schedule_id=schedule_id,
            position=index + 1,
            description=line.description,
            job_id=line.job_id,
            service_id=line.service_id,
            quantity=line.quantity,
            unit_amount=line.unit_amount,
            vat_rate_id=line.vat_rate_id,
        )
        for index, line in enumerate(lines)
    ]


def _advance(schedule: RecurringSchedule, run_date: date) -> None:
    """After ``run_date`` ran: remember it and move to the next date (or end the schedule)."""
    schedule.last_run_on = run_date
    following = first_after(schedule.start_date, schedule.frequency, run_date)
    schedule.next_run_date = following
    if schedule.end_date is not None and following > schedule.end_date:
        schedule.status = "ended"


class RecurringService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.recurring = RecurringRepository(session)
        self.refs = BillingReferenceRepository(session)

    # --- Reads -------------------------------------------------------------------------

    async def list_schedules(
        self, ctx: OrgContext, filters: ScheduleFilters, params: PageParams
    ) -> tuple[list[ScheduleSummaryRead], str | None]:
        org_id = ctx.organization_id
        page = await self.recurring.list_page(org_id, filters, params)
        ids = [s.id for s in page.items]
        clients = await self.refs.clients_by_ids(org_id, [s.client_id for s in page.items])
        nets = await self.recurring.net_estimates(org_id, ids)
        failed = await self.recurring.failed_counts(org_id, ids)
        items = [
            ScheduleSummaryRead.build(
                s,
                clients.get(s.client_id),
                net_estimate=nets.get(s.id, 0),
                failed_runs=failed.get(s.id, 0),
            )
            for s in page.items
        ]
        return items, page.next_cursor

    async def get(self, ctx: OrgContext, schedule_id: uuid.UUID) -> ScheduleRead:
        return await self._detail(ctx, await self._get(ctx, schedule_id))

    async def _get(
        self, ctx: OrgContext, schedule_id: uuid.UUID, lock: bool = False
    ) -> RecurringSchedule:
        schedule = await self.recurring.get(ctx.organization_id, schedule_id, for_update=lock)
        if schedule is None:
            raise NotFoundError("Recurring schedule not found.")
        return schedule

    async def _detail(self, ctx: OrgContext, schedule: RecurringSchedule) -> ScheduleRead:
        org_id = ctx.organization_id
        client = await self.refs.get_client(org_id, schedule.client_id)
        lines = await self.recurring.lines(org_id, schedule.id)
        failed = await self.recurring.failed_counts(org_id, [schedule.id])
        summary = ScheduleSummaryRead.build(
            schedule,
            client,
            net_estimate=(await self.recurring.net_estimates(org_id, [schedule.id])).get(
                schedule.id, 0
            ),
            failed_runs=failed.get(schedule.id, 0),
        )
        return ScheduleRead(
            **summary.model_dump(),
            notes=schedule.notes,
            lines=[ScheduleLineRead.from_model(line) for line in lines],
        )

    async def list_runs(
        self, ctx: OrgContext, schedule_id: uuid.UUID, params: PageParams
    ) -> tuple[list[RunRead], str | None]:
        await self._get(ctx, schedule_id)
        page = await self.recurring.runs_page(ctx.organization_id, schedule_id, params)
        numbers = await self.recurring.invoice_numbers(
            ctx.organization_id, [r.invoice_id for r in page.items if r.invoice_id]
        )
        items = [
            RunRead.build(r, numbers.get(r.invoice_id) if r.invoice_id else None)
            for r in page.items
        ]
        return items, page.next_cursor

    # --- Writes ------------------------------------------------------------------------

    async def create(
        self, ctx: OrgContext, data: ScheduleCreate, rctx: RequestContext
    ) -> ScheduleRead:
        async with unit_of_work(self.session):
            client = await get_client_or_404(self.refs, ctx.organization_id, data.client_id)
            # Priced once now so a bad job, service or rate is refused at once.
            await billing_drafts.price_lines(self.refs, ctx.organization, client, data.lines)
            schedule = await self.recurring.add(
                RecurringSchedule(
                    organization_id=ctx.organization_id,
                    client_id=client.id,
                    name=data.name,
                    frequency=data.frequency,
                    start_date=data.start_date,
                    end_date=data.end_date,
                    next_run_date=data.start_date,
                    status="active",
                    reference=data.reference,
                    notes=data.notes,
                    created_by=ctx.user.id,
                )
            )
            await self.recurring.replace_lines(
                ctx.organization_id,
                schedule.id,
                _rows(ctx.organization_id, schedule.id, data.lines),
            )
            await record(
                self.session,
                ctx,
                rctx,
                "recurring.created",
                target_type="recurring_schedule",
                target_id=schedule.id,
                meta=_meta(schedule, client),
            )
            detail = await self._detail(ctx, schedule)
        return detail

    async def update(
        self,
        ctx: OrgContext,
        schedule_id: uuid.UUID,
        data: ScheduleUpdate,
        rctx: RequestContext,
    ) -> ScheduleRead:
        async with unit_of_work(self.session):
            schedule = await self._get(ctx, schedule_id, lock=True)
            if schedule.status == "ended":
                raise conflict("SCHEDULE_ENDED", "An ended schedule cannot be changed.")
            client = await get_client_or_404(self.refs, ctx.organization_id, schedule.client_id)
            changed = data.model_fields_set
            today = today_for(ctx.organization)
            if "name" in changed and data.name is not None:
                schedule.name = data.name
            if "reference" in changed:
                schedule.reference = data.reference
            if "notes" in changed:
                schedule.notes = data.notes
            if "end_date" in changed:
                if data.end_date is not None and data.end_date < schedule.start_date:
                    raise conflict("SCHEDULE_DATES", "The end date is before the start date.")
                schedule.end_date = data.end_date
            if "frequency" in changed and data.frequency and data.frequency != schedule.frequency:
                schedule.frequency = data.frequency
                schedule.next_run_date = first_on_or_after(
                    schedule.start_date, data.frequency, max(today, schedule.start_date)
                )
            if data.lines is not None:
                await billing_drafts.price_lines(self.refs, ctx.organization, client, data.lines)
                await self.recurring.replace_lines(
                    ctx.organization_id,
                    schedule.id,
                    _rows(ctx.organization_id, schedule.id, data.lines),
                )
            if schedule.end_date is not None and schedule.next_run_date > schedule.end_date:
                schedule.status = "ended"
            await self.session.flush()
            await record(
                self.session,
                ctx,
                rctx,
                "recurring.updated",
                target_type="recurring_schedule",
                target_id=schedule.id,
                meta=_meta(schedule, client, fields=sorted(changed)),
            )
            detail = await self._detail(ctx, schedule)
        return detail

    async def pause(
        self, ctx: OrgContext, schedule_id: uuid.UUID, rctx: RequestContext
    ) -> ScheduleRead:
        async with unit_of_work(self.session):
            schedule = await self._get(ctx, schedule_id, lock=True)
            if schedule.status != "active":
                raise conflict("SCHEDULE_NOT_ACTIVE", "Only an active schedule can be paused.")
            schedule.status = "paused"
            await self.session.flush()
            await self._audit(ctx, rctx, "recurring.paused", schedule)
            detail = await self._detail(ctx, schedule)
        return detail

    async def resume(
        self, ctx: OrgContext, schedule_id: uuid.UUID, rctx: RequestContext
    ) -> ScheduleRead:
        """Dates missed while paused are skipped: the next run is the first date from today."""
        async with unit_of_work(self.session):
            schedule = await self._get(ctx, schedule_id, lock=True)
            if schedule.status != "paused":
                raise conflict("SCHEDULE_NOT_PAUSED", "Only a paused schedule can be resumed.")
            today = today_for(ctx.organization)
            if schedule.next_run_date < today and not await self._has_failed_run(schedule):
                schedule.next_run_date = first_on_or_after(
                    schedule.start_date, schedule.frequency, today
                )
            schedule.status = "active"
            if schedule.end_date is not None and schedule.next_run_date > schedule.end_date:
                schedule.status = "ended"
            await self.session.flush()
            await self._audit(ctx, rctx, "recurring.resumed", schedule)
            detail = await self._detail(ctx, schedule)
        return detail

    async def delete(self, ctx: OrgContext, schedule_id: uuid.UUID, rctx: RequestContext) -> None:
        """A schedule that has run stays as history: end or pause it instead (409)."""
        async with unit_of_work(self.session):
            schedule = await self._get(ctx, schedule_id, lock=True)
            if await self.recurring.has_runs(schedule.id):
                raise conflict(
                    "SCHEDULE_HAS_RUNS",
                    "This schedule has already run. Pause it or set an end date instead.",
                )
            await self._audit(ctx, rctx, "recurring.deleted", schedule)
            await self.recurring.delete(schedule)

    async def retry_run(self, ctx: OrgContext, run_id: uuid.UUID, rctx: RequestContext) -> RunRead:
        """Try a failed run again. A run that fails again stays ``failed`` with its new error
        (the response says so); one that works drafts the invoice and moves the schedule on."""
        async with unit_of_work(self.session):
            run = await self.recurring.get_run(ctx.organization_id, run_id)
            if run is None:
                raise NotFoundError("Run not found.")
            if run.status != "failed":
                raise conflict("RUN_NOT_FAILED", "Only a failed run can be retried.")
            schedule = await self._get(ctx, run.schedule_id, lock=True)
            invoice_id, error = await _draft_for(
                self.session, ctx.organization, schedule, run.run_date
            )
            run.attempts += 1
            run.ran_at = datetime.now(UTC)
            if invoice_id is None:
                run.error = error
            else:
                run.status, run.invoice_id, run.error = "succeeded", invoice_id, None
                if schedule.next_run_date == run.run_date and schedule.status != "ended":
                    _advance(schedule, run.run_date)
                else:
                    schedule.last_run_on = max(schedule.last_run_on or run.run_date, run.run_date)
            await self.session.flush()
            await _audit_run(self.session, schedule, run, invoice_id, rctx, ctx.user.id)
            numbers = await self.recurring.invoice_numbers(
                ctx.organization_id, [run.invoice_id] if run.invoice_id else []
            )
            read = RunRead.build(run, numbers.get(run.invoice_id) if run.invoice_id else None)
        return read

    async def _has_failed_run(self, schedule: RecurringSchedule) -> bool:
        """The date the schedule waits on already has a (failed) run: keep it for the retry."""
        return await self.recurring.run_exists(schedule.id, schedule.next_run_date)

    async def _audit(
        self, ctx: OrgContext, rctx: RequestContext, action: str, schedule: RecurringSchedule
    ) -> None:
        client = await self.refs.get_client(ctx.organization_id, schedule.client_id)
        await record(
            self.session,
            ctx,
            rctx,
            action,
            target_type="recurring_schedule",
            target_id=schedule.id,
            meta=_meta(schedule, client),
        )


# --- Raising the invoice ------------------------------------------------------------------


def _reason(exc: Exception) -> str:
    """Why a run failed, in words for the run list: the first field message of a validation
    error, else the error's own message."""
    details = getattr(exc, "details", None)
    if isinstance(details, list) and details and isinstance(details[0], dict):
        message = details[0].get("message")
        if message:
            return str(message)[:500]
    return (getattr(exc, "message", None) or str(exc) or type(exc).__name__)[:500]


async def _draft_for(
    session: AsyncSession, organization: Organization, schedule: RecurringSchedule, run_date: date
) -> tuple[uuid.UUID | None, str | None]:
    """Draft the invoice of one run in a savepoint: ``(invoice_id, None)`` on success,
    ``(None, reason)`` when it could not be raised (the savepoint is rolled back)."""
    repo = RecurringRepository(session)
    refs = BillingReferenceRepository(session)
    try:
        async with session.begin_nested():
            client = await refs.get_client(organization.id, schedule.client_id)
            if client is None:
                raise ValueError("The client no longer exists.")
            lines = await repo.lines(organization.id, schedule.id)
            priced = await billing_drafts.price_lines(refs, organization, client, lines)
            invoice = await billing_drafts.create_invoice_draft(
                InvoiceRepository(session),
                organization,
                client,
                priced,
                created_by=schedule.created_by,
                reference=schedule.reference,
                notes=schedule.notes,
            )
            await audit.record(
                session,
                "invoice.draft_created",
                actor_user_id=schedule.created_by,
                organization_id=organization.id,
                target_type="invoice",
                target_id=invoice.id,
                meta={
                    "client_id": str(client.id),
                    "client": client.name,
                    "gross_total": invoice.gross_total,
                    "recurring_schedule": schedule.name,
                    "run_date": run_date.isoformat(),
                },
            )
            return invoice.id, None
    except Exception as exc:
        log.warning(
            "recurring_run_failed",
            schedule_id=str(schedule.id),
            run_date=run_date.isoformat(),
            error=str(exc),
        )
        return None, _reason(exc)


async def _audit_run(
    session: AsyncSession,
    schedule: RecurringSchedule,
    run: RecurringRun,
    invoice_id: uuid.UUID | None,
    rctx: RequestContext | None,
    actor: uuid.UUID | None,
) -> None:
    await audit.record(
        session,
        "recurring.run" if invoice_id else "recurring.run_failed",
        ctx=rctx,
        actor_user_id=actor,
        organization_id=schedule.organization_id,
        target_type="recurring_schedule",
        target_id=schedule.id,
        meta={
            "name": schedule.name,
            "run_date": run.run_date.isoformat(),
            "invoice_id": str(invoice_id) if invoice_id else None,
            "error": run.error,
        },
    )


# --- The daily job ------------------------------------------------------------------------


async def run_due(session: AsyncSession) -> dict[str, int]:
    """Draft the invoice of every active schedule that is due, for every practice. A practice
    or schedule that fails is logged and skipped; the others still run. Idempotent: one run
    row per (schedule, date), so a repeat finds nothing left to do."""
    repo = RecurringRepository(session)
    horizon = datetime.now(UTC).date() + timedelta(days=1)
    totals = {"practices": 0, "drafted": 0, "failed": 0}
    for organization_id in await repo.due_organizations(horizon):
        try:
            drafted, failed = await _run_practice(session, organization_id)
        except Exception:
            log.exception("recurring_practice_failed", organization_id=str(organization_id))
            totals["failed"] += 1
            continue
        totals["practices"] += 1
        totals["drafted"] += drafted
        totals["failed"] += failed
    return totals


async def _run_practice(session: AsyncSession, organization_id: uuid.UUID) -> tuple[int, int]:
    refs = BillingReferenceRepository(session)
    organization = await refs.get_organization(organization_id)
    if organization is None:
        return 0, 0
    today = today_for(organization)
    drafted = failed = 0
    for schedule_id in await RecurringRepository(session).due_schedule_ids(organization_id, today):
        for _ in range(MAX_CATCH_UP):
            outcome = await _run_one(session, organization, schedule_id, today)
            if outcome == "drafted":
                drafted += 1
                continue
            if outcome == "failed":
                failed += 1
            break
    return drafted, failed


async def _run_one(
    session: AsyncSession, organization: Organization, schedule_id: uuid.UUID, today: date
) -> str:
    """One run in its own transaction. ``drafted``, ``failed`` or ``skipped`` (not due, being
    run elsewhere, or already run)."""
    repo = RecurringRepository(session)
    async with unit_of_work(session):
        schedule = await repo.get(organization.id, schedule_id, for_update=True, skip_locked=True)
        if (
            schedule is None
            or schedule.status != "active"
            or schedule.next_run_date > today
            or await repo.run_exists(schedule.id, schedule.next_run_date)
        ):
            return "skipped"
        run_date = schedule.next_run_date
        if schedule.end_date is not None and run_date > schedule.end_date:
            schedule.status = "ended"
            return "skipped"
        invoice_id, error = await _draft_for(session, organization, schedule, run_date)
        run = await repo.add_run(
            RecurringRun(
                id=new_id(),
                organization_id=organization.id,
                schedule_id=schedule.id,
                run_date=run_date,
                status="succeeded" if invoice_id else "failed",
                invoice_id=invoice_id,
                error=error,
                attempts=1,
            )
        )
        if invoice_id is not None:
            _advance(schedule, run_date)
        await session.flush()
        await _audit_run(session, schedule, run, invoice_id, None, None)
    return "drafted" if invoice_id is not None else "failed"
