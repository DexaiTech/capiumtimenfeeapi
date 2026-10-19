"""Work-in-progress queries: the WIP ledger, item locks, allocations and write-offs.

WIP is billable time (on an approved timesheet when the practice requires approval), billable
disbursements whose ``billing_status`` is ``unbilled`` and approved billable expense lines
(``app/repositories/expense_lines.py``). Every method is scoped to one organization.
"""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import (
    ColumnElement,
    Date,
    Integer,
    case,
    delete,
    func,
    literal,
    select,
    tuple_,
    union_all,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Client,
    Disbursement,
    Expense,
    ExpenseClaim,
    Invoice,
    InvoiceLine,
    Job,
    TimeEntry,
    Timesheet,
    WipAllocation,
    WipWriteOff,
)
from app.repositories.billing_common import paginate_by_date
from app.repositories.expense_lines import wip_expense_conditions
from app.utils.pagination import Page, PageParams, decode_cursor, encode_cursor


@dataclass(frozen=True, slots=True)
class WipSummaryFilters:
    client_id: uuid.UUID | None = None
    partner_user_id: uuid.UUID | None = None
    manager_user_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class WipSummaryRow:
    client_id: uuid.UUID
    client_code: str
    client_name: str
    partner_user_id: uuid.UUID | None
    manager_user_id: uuid.UUID | None
    minutes: int
    time_value: int
    disbursement_value: int
    oldest_date: date | None
    age_0_30: int
    age_31_60: int
    age_61_90: int
    age_91_plus: int
    item_count: int


@dataclass(frozen=True, slots=True)
class WriteOffFilters:
    client_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    date_from: date | None = None
    date_to: date | None = None


def _approved_timesheet() -> ColumnElement[bool]:
    return (
        select(Timesheet.id)
        .where(
            Timesheet.organization_id == TimeEntry.organization_id,
            Timesheet.id == TimeEntry.timesheet_id,
            Timesheet.status == "approved",
        )
        .exists()
    )


def approved_timesheet() -> ColumnElement[bool]:
    """The time entry is on an approved timesheet (for queries that select ``TimeEntry``)."""
    return _approved_timesheet()


def eligible_time_conditions(
    organization_id: uuid.UUID, require_approval: bool
) -> list[ColumnElement[bool]]:
    """The WIP ledger's time: billable, unbilled, with a client, approved when required."""
    conditions: list[ColumnElement[bool]] = [
        TimeEntry.organization_id == organization_id,
        TimeEntry.is_billable.is_(True),
        TimeEntry.billing_status == "unbilled",
        TimeEntry.client_id.is_not(None),
    ]
    if require_approval:
        conditions.append(_approved_timesheet())
    return conditions


def eligible_disbursement_conditions(organization_id: uuid.UUID) -> list[ColumnElement[bool]]:
    return [
        Disbursement.organization_id == organization_id,
        Disbursement.is_billable.is_(True),
        Disbursement.billing_status == "unbilled",
    ]


class WipRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- The ledger --------------------------------------------------------------------

    async def summary_page(
        self,
        organization_id: uuid.UUID,
        *,
        require_approval: bool,
        filters: WipSummaryFilters,
        today: date,
        params: PageParams,
    ) -> Page[WipSummaryRow]:
        """One row per client with WIP, ordered by ``(lower(name), id)``."""
        time_items = select(
            TimeEntry.client_id.label("client_id"),
            TimeEntry.entry_date.label("item_date"),
            TimeEntry.minutes.label("minutes"),
            TimeEntry.charge_value.label("time_value"),
            literal(0, Integer).label("disbursement_value"),
        ).where(*eligible_time_conditions(organization_id, require_approval))
        disbursement_items = select(
            Disbursement.client_id.label("client_id"),
            Disbursement.incurred_on.label("item_date"),
            literal(0, Integer).label("minutes"),
            literal(0, Integer).label("time_value"),
            Disbursement.amount.label("disbursement_value"),
        ).where(*eligible_disbursement_conditions(organization_id))
        if filters.job_id is not None:
            time_items = time_items.where(TimeEntry.job_id == filters.job_id)
            disbursement_items = disbursement_items.where(Disbursement.job_id == filters.job_id)
        if filters.client_id is not None:
            time_items = time_items.where(TimeEntry.client_id == filters.client_id)
            disbursement_items = disbursement_items.where(
                Disbursement.client_id == filters.client_id
            )
        items = union_all(time_items, disbursement_items).subquery("items")

        value = items.c.time_value + items.c.disbursement_value
        d30, d60, d90 = (literal(today - timedelta(days=n), Date) for n in (30, 60, 90))

        def bucket(condition: ColumnElement[bool]) -> ColumnElement[int]:
            return func.coalesce(func.sum(case((condition, value), else_=0)), 0)

        grouped = (
            select(
                items.c.client_id,
                func.coalesce(func.sum(items.c.minutes), 0).label("minutes"),
                func.coalesce(func.sum(items.c.time_value), 0).label("time_value"),
                func.coalesce(func.sum(items.c.disbursement_value), 0).label("disbursement_value"),
                func.min(items.c.item_date).label("oldest_date"),
                bucket(items.c.item_date >= d30).label("age_0_30"),
                bucket((items.c.item_date < d30) & (items.c.item_date >= d60)).label("age_31_60"),
                bucket((items.c.item_date < d60) & (items.c.item_date >= d90)).label("age_61_90"),
                bucket(items.c.item_date < d90).label("age_91_plus"),
                func.count().label("item_count"),
            )
            .group_by(items.c.client_id)
            .subquery("grouped")
        )
        sort_key = func.lower(Client.name)
        stmt = (
            select(Client, grouped, sort_key.label("sort_key"))
            .join(grouped, grouped.c.client_id == Client.id)
            .where(Client.organization_id == organization_id)
        )
        if filters.partner_user_id is not None:
            stmt = stmt.where(Client.partner_user_id == filters.partner_user_id)
        if filters.manager_user_id is not None:
            stmt = stmt.where(Client.manager_user_id == filters.manager_user_id)
        if params.cursor:
            value_key, last_id = decode_cursor(params.cursor)
            stmt = stmt.where(tuple_(sort_key, Client.id) > (str(value_key), last_id))
        stmt = stmt.order_by(sort_key, Client.id).limit(params.limit + 1)
        rows = (await self.session.execute(stmt)).all()
        next_cursor = None
        if len(rows) > params.limit:
            rows = rows[: params.limit]
            next_cursor = encode_cursor(rows[-1].sort_key, rows[-1][0].id)
        result = []
        for row in rows:
            client: Client = row[0]
            result.append(
                WipSummaryRow(
                    client_id=client.id,
                    client_code=client.code,
                    client_name=client.name,
                    partner_user_id=client.partner_user_id,
                    manager_user_id=client.manager_user_id,
                    minutes=int(row.minutes),
                    time_value=int(row.time_value),
                    disbursement_value=int(row.disbursement_value),
                    oldest_date=row.oldest_date,
                    age_0_30=int(row.age_0_30),
                    age_31_60=int(row.age_31_60),
                    age_61_90=int(row.age_61_90),
                    age_91_plus=int(row.age_91_plus),
                    item_count=int(row.item_count),
                )
            )
        return Page(items=result, next_cursor=next_cursor)

    async def client_time(
        self,
        organization_id: uuid.UUID,
        client_id: uuid.UUID,
        *,
        require_approval: bool,
        job_id: uuid.UUID | None = None,
    ) -> list[TimeEntry]:
        stmt = select(TimeEntry).where(
            *eligible_time_conditions(organization_id, require_approval),
            TimeEntry.client_id == client_id,
        )
        if job_id is not None:
            stmt = stmt.where(TimeEntry.job_id == job_id)
        stmt = stmt.order_by(TimeEntry.entry_date, TimeEntry.created_at, TimeEntry.id)
        return list((await self.session.execute(stmt)).scalars())

    async def client_disbursements(
        self, organization_id: uuid.UUID, client_id: uuid.UUID, *, job_id: uuid.UUID | None = None
    ) -> list[Disbursement]:
        stmt = select(Disbursement).where(
            *eligible_disbursement_conditions(organization_id),
            Disbursement.client_id == client_id,
        )
        if job_id is not None:
            stmt = stmt.where(Disbursement.job_id == job_id)
        stmt = stmt.order_by(Disbursement.incurred_on, Disbursement.created_at, Disbursement.id)
        return list((await self.session.execute(stmt)).scalars())

    # --- Item locks (always time entries, then disbursements, each in id order) ----------

    async def fixed_fee_time_value(
        self, organization_id: uuid.UUID, client_id: uuid.UUID, *, require_approval: bool
    ) -> int:
        """The charge value of the client's WIP time that sits on fixed-fee jobs: the part of
        the time WIP that the fixed fee clears (a client's "fixed fee WIP")."""
        stmt = (
            select(func.coalesce(func.sum(TimeEntry.charge_value), 0))
            .join(
                Job,
                (Job.organization_id == TimeEntry.organization_id) & (Job.id == TimeEntry.job_id),
            )
            .where(
                *eligible_time_conditions(organization_id, require_approval),
                TimeEntry.client_id == client_id,
                Job.billing_basis == "fixed",
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())

    # --- Item locks (always time entries, then disbursements, each in id order) ----------

    async def lock_time_entries(
        self, organization_id: uuid.UUID, ids: Iterable[uuid.UUID]
    ) -> list[TimeEntry]:
        wanted = sorted(set(ids))
        if not wanted:
            return []
        stmt = (
            select(TimeEntry)
            .where(TimeEntry.organization_id == organization_id, TimeEntry.id.in_(wanted))
            .order_by(TimeEntry.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def lock_disbursements(
        self, organization_id: uuid.UUID, ids: Iterable[uuid.UUID]
    ) -> list[Disbursement]:
        wanted = sorted(set(ids))
        if not wanted:
            return []
        stmt = (
            select(Disbursement)
            .where(Disbursement.organization_id == organization_id, Disbursement.id.in_(wanted))
            .order_by(Disbursement.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def lock_expenses(
        self, organization_id: uuid.UUID, ids: Iterable[uuid.UUID]
    ) -> list[Expense]:
        """Expense claim lines, locked in id order (after the time entries and disbursements)."""
        wanted = sorted(set(ids))
        if not wanted:
            return []
        stmt = (
            select(Expense)
            .where(Expense.organization_id == organization_id, Expense.id.in_(wanted))
            .order_by(Expense.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def claim_statuses(
        self, organization_id: uuid.UUID, ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        wanted = set(ids)
        if not wanted:
            return {}
        stmt = select(ExpenseClaim.id, ExpenseClaim.status).where(
            ExpenseClaim.organization_id == organization_id, ExpenseClaim.id.in_(wanted)
        )
        return {row[0]: row[1] for row in (await self.session.execute(stmt)).all()}

    async def timesheet_statuses(
        self, organization_id: uuid.UUID, ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        wanted = set(ids)
        if not wanted:
            return {}
        stmt = select(Timesheet.id, Timesheet.status).where(
            Timesheet.organization_id == organization_id, Timesheet.id.in_(wanted)
        )
        return {row[0]: row[1] for row in (await self.session.execute(stmt)).all()}

    async def job_item_ids(
        self, organization_id: uuid.UUID, job_id: uuid.UUID, *, require_approval: bool
    ) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
        """Ids of the job's current WIP: (time entry ids, disbursement ids)."""
        time_stmt = select(TimeEntry.id).where(
            *eligible_time_conditions(organization_id, require_approval),
            TimeEntry.job_id == job_id,
        )
        disbursement_stmt = select(Disbursement.id).where(
            *eligible_disbursement_conditions(organization_id), Disbursement.job_id == job_id
        )
        time_ids = list((await self.session.execute(time_stmt)).scalars())
        disbursement_ids = list((await self.session.execute(disbursement_stmt)).scalars())
        return time_ids, disbursement_ids

    async def job_expense_ids(
        self, organization_id: uuid.UUID, job_id: uuid.UUID
    ) -> list[uuid.UUID]:
        """Ids of the job's approved billable expense lines that are WIP."""
        stmt = select(Expense.id).where(
            *wip_expense_conditions(organization_id), Expense.job_id == job_id
        )
        return list((await self.session.execute(stmt)).scalars())

    async def jobs_by_ids(
        self, organization_id: uuid.UUID, ids: Iterable[uuid.UUID | None]
    ) -> dict[uuid.UUID, Job]:
        wanted = {i for i in ids if i is not None}
        if not wanted:
            return {}
        stmt = select(Job).where(Job.organization_id == organization_id, Job.id.in_(wanted))
        return {j.id: j for j in (await self.session.execute(stmt)).scalars()}

    # --- Allocations -------------------------------------------------------------------

    async def add_allocations(self, allocations: Sequence[WipAllocation]) -> None:
        self.session.add_all(allocations)
        await self.session.flush()

    async def allocations_for_invoice(
        self, organization_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> list[WipAllocation]:
        stmt = (
            select(WipAllocation)
            .where(
                WipAllocation.organization_id == organization_id,
                WipAllocation.invoice_id == invoice_id,
            )
            .order_by(WipAllocation.created_at, WipAllocation.id)
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def allocations_for_line(
        self, organization_id: uuid.UUID, line_id: uuid.UUID
    ) -> list[WipAllocation]:
        stmt = (
            select(WipAllocation)
            .where(
                WipAllocation.organization_id == organization_id,
                WipAllocation.invoice_line_id == line_id,
            )
            .order_by(WipAllocation.id)
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def allocations_for_write_off(
        self, organization_id: uuid.UUID, write_off_id: uuid.UUID
    ) -> list[WipAllocation]:
        stmt = (
            select(WipAllocation)
            .where(
                WipAllocation.organization_id == organization_id,
                WipAllocation.write_off_id == write_off_id,
            )
            .order_by(WipAllocation.id)
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def delete_allocations(
        self, organization_id: uuid.UUID, ids: Iterable[uuid.UUID]
    ) -> None:
        wanted = list(set(ids))
        if not wanted:
            return
        await self.session.execute(
            delete(WipAllocation).where(
                WipAllocation.organization_id == organization_id, WipAllocation.id.in_(wanted)
            )
        )

    async def reverse_allocations(
        self, organization_id: uuid.UUID, ids: Iterable[uuid.UUID], at: datetime
    ) -> None:
        wanted = list(set(ids))
        if not wanted:
            return
        await self.session.execute(
            update(WipAllocation)
            .where(
                WipAllocation.organization_id == organization_id,
                WipAllocation.id.in_(wanted),
                WipAllocation.reversed_at.is_(None),
            )
            .values(reversed_at=at)
        )

    async def lock_fixed_fee_jobs(
        self, organization_id: uuid.UUID, job_ids: Iterable[uuid.UUID]
    ) -> None:
        """Serialise fixed-fee billing per job: a transaction-scoped advisory lock per job,
        in id order, taken after the WIP item locks. Two drafts billing different time of
        one fixed-fee job then see each other's fixed-fee line instead of both billing the
        whole fee. Only billing takes these locks, so they cannot deadlock with row locks."""
        for job_id in sorted(set(job_ids)):
            await self.session.execute(
                select(
                    func.pg_advisory_xact_lock(
                        func.hashtextextended(f"fixed_fee:{organization_id}:{job_id}", 0)
                    )
                )
            )

    async def fixed_fee_billed(
        self, organization_id: uuid.UUID, job_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """Net already put on fixed-fee lines of draft or issued invoices, per job. Call
        with ``lock_fixed_fee_jobs`` held for those jobs."""
        wanted = set(job_ids)
        if not wanted:
            return {}
        stmt = (
            select(InvoiceLine.job_id, func.coalesce(func.sum(InvoiceLine.net_amount), 0))
            .join(
                Invoice,
                (Invoice.organization_id == InvoiceLine.organization_id)
                & (Invoice.id == InvoiceLine.invoice_id),
            )
            .where(
                InvoiceLine.organization_id == organization_id,
                InvoiceLine.kind == "fixed_fee",
                InvoiceLine.job_id.in_(wanted),
                Invoice.status != "void",
            )
            .group_by(InvoiceLine.job_id)
        )
        return {row[0]: int(row[1]) for row in (await self.session.execute(stmt)).all() if row[0]}

    # --- Write-offs --------------------------------------------------------------------

    async def add_write_off(self, write_off: WipWriteOff) -> WipWriteOff:
        self.session.add(write_off)
        await self.session.flush()
        await self.session.refresh(write_off)
        return write_off

    async def get_write_off(
        self, organization_id: uuid.UUID, write_off_id: uuid.UUID, *, for_update: bool = False
    ) -> WipWriteOff | None:
        stmt = (
            select(WipWriteOff)
            .where(WipWriteOff.organization_id == organization_id, WipWriteOff.id == write_off_id)
            .execution_options(populate_existing=True)
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_write_offs(
        self, organization_id: uuid.UUID, filters: WriteOffFilters, params: PageParams
    ) -> Page[WipWriteOff]:
        stmt = select(WipWriteOff).where(WipWriteOff.organization_id == organization_id)
        if filters.client_id is not None:
            stmt = stmt.where(WipWriteOff.client_id == filters.client_id)
        if filters.job_id is not None:
            stmt = stmt.where(WipWriteOff.job_id == filters.job_id)
        if filters.date_from is not None:
            stmt = stmt.where(WipWriteOff.written_off_on >= filters.date_from)
        if filters.date_to is not None:
            stmt = stmt.where(WipWriteOff.written_off_on <= filters.date_to)
        return await paginate_by_date(
            self.session,
            stmt,
            sort_column=WipWriteOff.written_off_on,
            id_column=WipWriteOff.id,
            params=params,
        )

    async def write_off_item_counts(
        self, organization_id: uuid.UUID, ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        wanted = set(ids)
        if not wanted:
            return {}
        stmt = (
            select(WipAllocation.write_off_id, func.count())
            .where(
                WipAllocation.organization_id == organization_id,
                WipAllocation.write_off_id.in_(wanted),
            )
            .group_by(WipAllocation.write_off_id)
        )
        return {row[0]: int(row[1]) for row in (await self.session.execute(stmt)).all() if row[0]}

    async def delete_write_off(self, write_off: WipWriteOff) -> None:
        await self.session.delete(write_off)
        await self.session.flush()
