"""Work in progress: the WIP ledger per client, and write-offs without an invoice.

WIP is billable time (on an approved timesheet when the practice requires approval), billable
disbursements and approved billable expense lines that are ``unbilled``. ``lock_wip_items`` is
how every billing flow takes items out of WIP: it locks them (time entries, then
disbursements, then expense lines, each in id order) and checks that each one is still
available.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import NotFoundError, ValidationError
from app.db.models import (
    Client,
    Disbursement,
    Expense,
    Organization,
    TimeEntry,
    WipAllocation,
    WipWriteOff,
)
from app.db.session import unit_of_work
from app.repositories.billing_common import BillingReferenceRepository
from app.repositories.wip import (
    WipRepository,
    WipSummaryFilters,
    WipSummaryRow,
    WriteOffFilters,
)
from app.schemas.billing import (
    AgeBuckets,
    ClientRef,
    ClientWipRead,
    JobRef,
    PersonRef,
    WipClientSummary,
    WipItemRead,
    WipTotals,
    WriteOffCreate,
    WriteOffItemRead,
    WriteOffRead,
    hours_of,
)
from app.services import period_locks
from app.services.billing_common import (
    conflict,
    get_client_or_404,
    invalid,
    record,
    today_for,
)
from app.services.policy import OrgContext
from app.utils.pagination import Page, PageParams


@dataclass(slots=True)
class LockedItems:
    time_entries: list[TimeEntry]
    disbursements: list[Disbursement]
    expenses: list[Expense] = field(default_factory=list)

    @property
    def wip_value(self) -> int:
        return (
            sum(t.charge_value for t in self.time_entries)
            + sum(d.amount for d in self.disbursements)
            + sum(e.recharge_amount for e in self.expenses)
        )

    @property
    def count(self) -> int:
        return len(self.time_entries) + len(self.disbursements) + len(self.expenses)


async def lock_items(
    session: AsyncSession,
    organization_id: uuid.UUID,
    time_entry_ids: Sequence[uuid.UUID],
    disbursement_ids: Sequence[uuid.UUID],
    expense_ids: Sequence[uuid.UUID] = (),
) -> LockedItems:
    """Lock items in the global order and 404 any id that is not in the practice."""
    wip = WipRepository(session)
    entries = await wip.lock_time_entries(organization_id, time_entry_ids)
    if len(entries) != len(set(time_entry_ids)):
        raise NotFoundError("Time entry not found.")
    disbursements = await wip.lock_disbursements(organization_id, disbursement_ids)
    if len(disbursements) != len(set(disbursement_ids)):
        raise NotFoundError("Disbursement not found.")
    expenses = await wip.lock_expenses(organization_id, expense_ids)
    if len(expenses) != len(set(expense_ids)):
        raise NotFoundError("Expense not found.")
    return LockedItems(entries, disbursements, expenses)


async def lock_wip_items(
    session: AsyncSession,
    organization: Organization,
    client: Client,
    time_entry_ids: Sequence[uuid.UUID],
    disbursement_ids: Sequence[uuid.UUID],
    expense_ids: Sequence[uuid.UUID] = (),
) -> LockedItems:
    """Lock items and check that every one is the client's and currently WIP."""
    items = await lock_items(
        session, organization.id, time_entry_ids, disbursement_ids, expense_ids
    )
    wrong_client = (
        [str(t.id) for t in items.time_entries if t.client_id != client.id]
        + [str(d.id) for d in items.disbursements if d.client_id != client.id]
        + [str(e.id) for e in items.expenses if e.client_id != client.id]
    )
    if wrong_client:
        raise ValidationError(
            "Every item must belong to the client.",
            code="CLIENT_MISMATCH",
            details={"items": wrong_client},
        )
    wip = WipRepository(session)
    statuses: dict[uuid.UUID, str] = {}
    if organization.require_timesheet_approval:
        statuses = await wip.timesheet_statuses(
            organization.id, {t.timesheet_id for t in items.time_entries}
        )
    claims = await wip.claim_statuses(organization.id, {e.claim_id for e in items.expenses})
    unavailable: list[dict[str, str]] = []
    for entry in items.time_entries:
        reason = None
        if not entry.is_billable:
            reason = "not_billable"
        elif entry.billing_status != "unbilled":
            reason = entry.billing_status
        elif organization.require_timesheet_approval and statuses.get(entry.timesheet_id) != (
            "approved"
        ):
            reason = "timesheet_not_approved"
        if reason:
            unavailable.append({"time_entry_id": str(entry.id), "reason": reason})
    for disbursement in items.disbursements:
        reason = None
        if not disbursement.is_billable:
            reason = "not_billable"
        elif disbursement.billing_status != "unbilled":
            reason = disbursement.billing_status
        if reason:
            unavailable.append({"disbursement_id": str(disbursement.id), "reason": reason})
    for expense in items.expenses:
        reason = None
        if not expense.is_billable:
            reason = "not_billable"
        elif expense.billing_status != "unbilled":
            reason = expense.billing_status
        elif claims.get(expense.claim_id) != "approved":
            reason = "claim_not_approved"
        elif expense.recharge_amount <= 0:
            reason = "nothing_to_recharge"
        if reason:
            unavailable.append({"expense_id": str(expense.id), "reason": reason})
    if unavailable:
        raise conflict(
            "WIP_NOT_AVAILABLE",
            "Some items are not unbilled work in progress any more.",
            details=unavailable,
        )
    return items


def set_items_status(items: LockedItems, status: str) -> None:
    for entry in items.time_entries:
        entry.billing_status = status
    for disbursement in items.disbursements:
        disbursement.billing_status = status
    for expense in items.expenses:
        expense.billing_status = status


async def lock_allocation_items(
    session: AsyncSession, organization_id: uuid.UUID, allocations: Sequence[WipAllocation]
) -> LockedItems:
    """Lock every item the allocations point at: time entries, disbursements, expense lines."""
    return await lock_items(
        session,
        organization_id,
        [a.time_entry_id for a in allocations if a.time_entry_id],
        [a.disbursement_id for a in allocations if a.disbursement_id],
        [a.expense_id for a in allocations if a.expense_id],
    )


def summary_read(row: WipSummaryRow) -> WipClientSummary:
    return WipClientSummary(
        client=ClientRef(id=row.client_id, code=row.client_code, name=row.client_name),
        partner_user_id=row.partner_user_id,
        manager_user_id=row.manager_user_id,
        minutes=row.minutes,
        hours=hours_of(row.minutes),
        time_value=row.time_value,
        disbursement_value=row.disbursement_value,
        total=row.time_value + row.disbursement_value,
        oldest_date=row.oldest_date,
        ages=AgeBuckets(
            days_0_30=row.age_0_30,
            days_31_60=row.age_31_60,
            days_61_90=row.age_61_90,
            days_91_plus=row.age_91_plus,
        ),
        item_count=row.item_count,
    )


class WipService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.wip = WipRepository(session)
        self.refs = BillingReferenceRepository(session)

    async def summary(
        self, ctx: OrgContext, filters: WipSummaryFilters, params: PageParams
    ) -> Page[WipSummaryRow]:
        return await self.wip.summary_page(
            ctx.organization_id,
            require_approval=ctx.organization.require_timesheet_approval,
            filters=filters,
            today=today_for(ctx.organization),
            params=params,
        )

    async def client_wip(
        self, ctx: OrgContext, client_id: uuid.UUID, job_id: uuid.UUID | None
    ) -> ClientWipRead:
        client = await get_client_or_404(self.refs, ctx.organization_id, client_id)
        entries = await self.wip.client_time(
            ctx.organization_id,
            client.id,
            require_approval=ctx.organization.require_timesheet_approval,
            job_id=job_id,
        )
        disbursements = await self.wip.client_disbursements(
            ctx.organization_id, client.id, job_id=job_id
        )
        jobs = await self.wip.jobs_by_ids(
            ctx.organization_id, [e.job_id for e in entries] + [d.job_id for d in disbursements]
        )
        names = await self.refs.member_names(
            ctx.organization_id,
            [e.user_id for e in entries] + [d.created_by for d in disbursements],
        )

        def job_ref(job_id: uuid.UUID | None) -> JobRef | None:
            job = jobs.get(job_id) if job_id else None
            return JobRef.from_model(job) if job else None

        def person(user_id: uuid.UUID | None) -> PersonRef | None:
            return PersonRef(user_id=user_id, full_name=names.get(user_id)) if user_id else None

        items = [
            WipItemRead(
                kind="time",
                id=e.id,
                date=e.entry_date,
                job=job_ref(e.job_id),
                person=person(e.user_id),
                description=e.description,
                minutes=e.minutes,
                hours=hours_of(e.minutes),
                rate=e.charge_rate,
                value=e.charge_value,
                activity_code_id=e.activity_code_id,
            )
            for e in entries
        ] + [
            WipItemRead(
                kind="disbursement",
                id=d.id,
                date=d.incurred_on,
                job=job_ref(d.job_id),
                person=person(d.created_by),
                description=d.description,
                minutes=None,
                hours=None,
                rate=None,
                value=d.amount,
                supplier=d.supplier,
            )
            for d in disbursements
        ]
        items.sort(key=lambda i: (i.date, i.kind != "time"))
        minutes = sum(e.minutes for e in entries)
        time_value = sum(e.charge_value for e in entries)
        disbursement_value = sum(d.amount for d in disbursements)
        return ClientWipRead(
            client=ClientRef.from_model(client),
            items=items,
            totals=WipTotals(
                minutes=minutes,
                hours=hours_of(minutes),
                time_value=time_value,
                disbursement_value=disbursement_value,
                total=time_value + disbursement_value,
            ),
        )

    # --- Write-offs --------------------------------------------------------------------

    async def create_write_off(
        self, ctx: OrgContext, data: WriteOffCreate, rctx: RequestContext
    ) -> WriteOffRead:
        organization = ctx.organization
        async with unit_of_work(self.session):
            client = await get_client_or_404(self.refs, ctx.organization_id, data.client_id)
            if data.job_id is not None:
                job = await self.refs.get_job(ctx.organization_id, data.job_id)
                if job is None or job.client_id != client.id:
                    raise ValidationError.for_field(
                        "job_id", "The job must be one of the client's jobs."
                    )
                time_ids, disbursement_ids = await self.wip.job_item_ids(
                    ctx.organization_id,
                    job.id,
                    require_approval=organization.require_timesheet_approval,
                )
                if not time_ids and not disbursement_ids:
                    raise conflict("WIP_NOT_AVAILABLE", "This job has no work in progress.")
            else:
                assert data.items is not None
                time_ids = [i.time_entry_id for i in data.items if i.time_entry_id]
                disbursement_ids = [i.disbursement_id for i in data.items if i.disbursement_id]
                if len(set(time_ids)) + len(set(disbursement_ids)) != len(data.items):
                    raise invalid("DUPLICATE_ITEM", "items", "Each item may appear only once.")
            items = await lock_wip_items(
                self.session, organization, client, time_ids, disbursement_ids
            )
            written_off_on = data.written_off_on or today_for(organization)
            await period_locks.assert_open(self.session, ctx.organization_id, written_off_on, "wip")
            write_off = await self.wip.add_write_off(
                WipWriteOff(
                    organization_id=ctx.organization_id,
                    client_id=client.id,
                    job_id=data.job_id,
                    written_off_on=written_off_on,
                    reason=data.reason,
                    total_wip_value=items.wip_value,
                    created_by=ctx.user.id,
                )
            )
            allocations = [
                WipAllocation(
                    organization_id=ctx.organization_id,
                    time_entry_id=e.id,
                    write_off_id=write_off.id,
                    wip_value=e.charge_value,
                    billed_value=0,
                )
                for e in items.time_entries
            ] + [
                WipAllocation(
                    organization_id=ctx.organization_id,
                    disbursement_id=d.id,
                    write_off_id=write_off.id,
                    wip_value=d.amount,
                    billed_value=0,
                )
                for d in items.disbursements
            ]
            await self.wip.add_allocations(allocations)
            set_items_status(items, "written_off")
            await self.session.flush()
            await record(
                self.session,
                ctx,
                rctx,
                "wip.written_off",
                target_type="wip_write_off",
                target_id=write_off.id,
                meta={
                    "client_id": str(client.id),
                    "client_name": client.name,
                    "job_id": str(data.job_id) if data.job_id else None,
                    "reason": data.reason,
                    "written_off_on": written_off_on.isoformat(),
                    "total_wip_value": items.wip_value,
                    "item_count": items.count,
                },
            )
        return WriteOffRead.from_model(
            write_off,
            client=client,
            item_count=items.count,
            items=_write_off_items(allocations),
        )

    async def list_write_offs(
        self, ctx: OrgContext, filters: WriteOffFilters, params: PageParams
    ) -> tuple[Page[WipWriteOff], dict[uuid.UUID, Client], dict[uuid.UUID, int]]:
        page = await self.wip.list_write_offs(ctx.organization_id, filters, params)
        clients = await self.refs.clients_by_ids(
            ctx.organization_id, [w.client_id for w in page.items]
        )
        counts = await self.wip.write_off_item_counts(
            ctx.organization_id, [w.id for w in page.items]
        )
        return page, clients, counts

    async def get_write_off(self, ctx: OrgContext, write_off_id: uuid.UUID) -> WriteOffRead:
        write_off = await self.wip.get_write_off(ctx.organization_id, write_off_id)
        if write_off is None:
            raise NotFoundError("Write-off not found.")
        allocations = await self.wip.allocations_for_write_off(ctx.organization_id, write_off.id)
        client = await self.refs.get_client(ctx.organization_id, write_off.client_id)
        return WriteOffRead.from_model(
            write_off,
            client=client,
            item_count=len(allocations),
            items=_write_off_items(allocations),
        )

    async def reverse_write_off(
        self, ctx: OrgContext, write_off_id: uuid.UUID, rctx: RequestContext
    ) -> None:
        async with unit_of_work(self.session):
            write_off = await self.wip.get_write_off(
                ctx.organization_id, write_off_id, for_update=True
            )
            if write_off is None:
                raise NotFoundError("Write-off not found.")
            await period_locks.assert_open(
                self.session, ctx.organization_id, write_off.written_off_on, "wip"
            )
            allocations = await self.wip.allocations_for_write_off(
                ctx.organization_id, write_off.id
            )
            items = await lock_items(
                self.session,
                ctx.organization_id,
                [a.time_entry_id for a in allocations if a.time_entry_id],
                [a.disbursement_id for a in allocations if a.disbursement_id],
            )
            moved_on = (
                any(a.reversed_at is not None for a in allocations)
                or any(t.billing_status != "written_off" for t in items.time_entries)
                or any(d.billing_status != "written_off" for d in items.disbursements)
            )
            if moved_on:
                raise conflict(
                    "WRITE_OFF_BILLED",
                    "Some of these items have been billed since: the write-off cannot be reversed.",
                )
            await self.wip.delete_allocations(ctx.organization_id, [a.id for a in allocations])
            set_items_status(items, "unbilled")
            client = await self.refs.get_client(ctx.organization_id, write_off.client_id)
            meta = {
                "client_id": str(write_off.client_id),
                "client_name": client.name if client else None,
                "reason": write_off.reason,
                "written_off_on": write_off.written_off_on.isoformat(),
                "total_wip_value": write_off.total_wip_value,
                "item_count": len(allocations),
            }
            await self.session.flush()
            await self.wip.delete_write_off(write_off)
            await record(
                self.session,
                ctx,
                rctx,
                "wip.write_off_reversed",
                target_type="wip_write_off",
                target_id=write_off_id,
                meta=meta,
            )


def _write_off_items(allocations: list[WipAllocation]) -> list[WriteOffItemRead]:
    return [
        WriteOffItemRead(
            kind="time" if a.time_entry_id else "disbursement",
            id=a.time_entry_id or a.disbursement_id,
            wip_value=a.wip_value,
        )
        for a in allocations
    ]
