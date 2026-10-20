"""The WIP atoms: every row that counts in the WIP of a practice as at a day.

An atom is a time entry, a disbursement, an approved expense line, the recognised unbilled part of
a fixed fee, or an adjustment that is still in force. ``load_atoms`` builds them for any day up to
today; for today it reads the live state, for an earlier day it rebuilds the day from the item
dates and from the allocations dated after it (an item billed or written off *after* the day was
still WIP at the end of it). Everything is scoped to one organization.
"""

import uuid
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, and_, exists, false, func, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models import (
    Client,
    Disbursement,
    Expense,
    ExpenseClaim,
    Invoice,
    InvoiceLine,
    Job,
    Service,
    TimeEntry,
    WipAllocation,
    WipMovement,
    WipWriteOff,
)
from app.repositories.expense_lines import wip_expense_conditions
from app.repositories.wip import approved_timesheet
from app.utils.money import percentage_bp
from app.utils.practice_dates import end_of_day_utc
from app.utils.wip_rules import recognised_fixed_fee

KINDS = ("time", "fixed_fee", "expense", "disbursement", "adjustment")


@dataclass(frozen=True, slots=True)
class WipFilters:
    """What narrows the atoms. ``partner_user_id`` / ``manager_user_id`` match the responsible
    person (the job's, else the client's), or the client's own with ``client_responsible``."""

    client_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    partner_user_id: uuid.UUID | None = None
    manager_user_id: uuid.UUID | None = None
    service_id: uuid.UUID | None = None
    kinds: frozenset[str] | None = None
    client_responsible: bool = False


@dataclass(frozen=True, slots=True)
class FixedFeeInfo:
    fee: int
    recognised: int
    billed: int
    mode: str
    progress_bp: int | None
    time_entry_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class WipAtom:
    kind: str
    id: uuid.UUID
    client_id: uuid.UUID
    client_code: str
    client_name: str
    job_id: uuid.UUID | None
    job_number: str | None
    job_name: str | None
    billing_basis: str | None
    service_id: uuid.UUID | None
    # The responsible people: the job's, else the client's.
    partner_user_id: uuid.UUID | None
    manager_user_id: uuid.UUID | None
    client_partner_user_id: uuid.UUID | None
    client_manager_user_id: uuid.UUID | None
    item_date: date
    component: str
    # What counts in the totals, and what the time is worth at its charge-out rate.
    value: int
    minutes: int = 0
    charge_value: int | None = None
    rate: int | None = None
    description: str | None = None
    person_user_id: uuid.UUID | None = None
    supplier: str | None = None
    activity_code_id: uuid.UUID | None = None
    claim_id: uuid.UUID | None = None
    fixed_fee: FixedFeeInfo | None = None
    # Adjustments: the ledger kind (write_on, write_off, transfer_in, ...).
    movement_kind: str | None = None


@dataclass(slots=True)
class _Dims:
    """Client and job columns joined to an item, as the atoms need them."""

    client_code: str
    client_name: str
    client_partner: uuid.UUID | None
    client_manager: uuid.UUID | None
    job_number: str | None = None
    job_name: str | None = None
    billing_basis: str | None = None
    service_id: uuid.UUID | None = None
    job_partner: uuid.UUID | None = None
    job_manager: uuid.UUID | None = None


@dataclass(slots=True)
class FixedFeeJob:
    job: Job
    dims: _Dims
    recognised: int | None = None
    billed: int = 0
    logged_minutes: int = 0
    recognised_on: date | None = None
    time_entry_ids: list[uuid.UUID] = field(default_factory=list)


def _atom(
    kind: str,
    item_id: uuid.UUID,
    client_id: uuid.UUID,
    job_id: uuid.UUID | None,
    dims: _Dims,
    item_date: date,
    component: str,
    value: int,
    **extra: Any,
) -> WipAtom:
    return WipAtom(
        kind=kind,
        id=item_id,
        client_id=client_id,
        client_code=dims.client_code,
        client_name=dims.client_name,
        job_id=job_id,
        job_number=dims.job_number,
        job_name=dims.job_name,
        billing_basis=dims.billing_basis,
        service_id=dims.service_id,
        partner_user_id=dims.job_partner or dims.client_partner,
        manager_user_id=dims.job_manager or dims.client_manager,
        client_partner_user_id=dims.client_partner,
        client_manager_user_id=dims.client_manager,
        item_date=item_date,
        component=component,
        value=value,
        **extra,
    )


def _filter_conditions(f: WipFilters, client_col: Any, job_col: Any) -> list[ColumnElement[bool]]:
    """Filters on a query that has ``Client`` joined and ``Job`` outer-joined."""
    conditions: list[ColumnElement[bool]] = []
    if f.client_id is not None:
        conditions.append(client_col == f.client_id)
    if f.job_id is not None:
        conditions.append(job_col == f.job_id)
    if f.service_id is not None:
        conditions.append(Job.service_id == f.service_id)
    if f.partner_user_id is not None:
        conditions.append(
            Client.partner_user_id == f.partner_user_id
            if f.client_responsible
            else func.coalesce(Job.partner_user_id, Client.partner_user_id) == f.partner_user_id
        )
    if f.manager_user_id is not None:
        conditions.append(
            Client.manager_user_id == f.manager_user_id
            if f.client_responsible
            else func.coalesce(Job.manager_user_id, Client.manager_user_id) == f.manager_user_id
        )
    return conditions


def _cleared_after(
    organization_id: uuid.UUID, as_at: date, item_fk: str, item_id: Any
) -> ColumnElement[bool]:
    """A live allocation cleared the item after ``as_at``: an issued invoice or a write-off dated
    later. Such an item was still WIP at the end of ``as_at``."""
    alloc = aliased(WipAllocation)
    inv = aliased(Invoice)
    wo = aliased(WipWriteOff)
    return (
        select(alloc.id)
        .outerjoin(
            inv, and_(inv.organization_id == alloc.organization_id, inv.id == alloc.invoice_id)
        )
        .outerjoin(
            wo, and_(wo.organization_id == alloc.organization_id, wo.id == alloc.write_off_id)
        )
        .where(
            alloc.organization_id == organization_id,
            getattr(alloc, item_fk) == item_id,
            alloc.reversed_at.is_(None),
            or_(and_(inv.status == "issued", inv.issue_date > as_at), wo.written_off_on > as_at),
        )
        .exists()
    )


def _status_condition(
    organization_id: uuid.UUID,
    as_at: date,
    today: date,
    status_col: Any,
    item_fk: str,
    item_id: Any,
) -> ColumnElement[bool]:
    if as_at >= today:
        return cast(ColumnElement[bool], status_col == "unbilled")
    return or_(
        status_col == "unbilled",
        and_(
            status_col.in_(("billed", "written_off")),
            _cleared_after(organization_id, as_at, item_fk, item_id),
        ),
    )


class WipAtomRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def service_names(
        self, organization_id: uuid.UUID, ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        if not ids:
            return {}
        stmt = select(Service.id, Service.name).where(
            Service.organization_id == organization_id, Service.id.in_(list(ids))
        )
        return {row[0]: row[1] for row in (await self.session.execute(stmt)).all()}

    # --- The atoms ---------------------------------------------------------------------

    async def load_atoms(
        self,
        organization_id: uuid.UUID,
        *,
        as_at: date,
        today: date,
        timezone: str,
        require_approval: bool,
        fixed_fee_mode: str,
        filters: WipFilters,
    ) -> list[WipAtom]:
        """Every atom of the practice as at ``as_at`` (not after ``today``), oldest first."""
        wanted = filters.kinds
        fixed = await self._fixed_fee_jobs(
            organization_id, as_at, today, timezone, fixed_fee_mode, filters
        )
        atoms: list[WipAtom] = []
        atoms += await self._time(organization_id, as_at, today, require_approval, filters, fixed)
        atoms += await self._disbursements(organization_id, as_at, today, filters)
        atoms += await self._expenses(organization_id, as_at, today, timezone, filters)
        atoms += self._fixed_fee_atoms(fixed, fixed_fee_mode, as_at)
        atoms += await self.adjustment_atoms(organization_id, as_at, today, filters)
        if wanted is not None:
            atoms = [a for a in atoms if a.kind in wanted]
        atoms.sort(key=lambda a: (a.item_date, a.kind != "time", str(a.id)))
        return atoms

    async def _time(
        self,
        organization_id: uuid.UUID,
        as_at: date,
        today: date,
        require_approval: bool,
        f: WipFilters,
        fixed: dict[uuid.UUID, FixedFeeJob],
    ) -> list[WipAtom]:
        if f.kinds is not None and not {"time", "fixed_fee"} & f.kinds:
            return []
        stmt = (
            select(
                TimeEntry.id,
                TimeEntry.client_id,
                TimeEntry.job_id,
                TimeEntry.user_id,
                TimeEntry.entry_date,
                TimeEntry.minutes,
                TimeEntry.charge_value,
                TimeEntry.charge_rate,
                TimeEntry.description,
                TimeEntry.activity_code_id,
                Client.code,
                Client.name,
                Client.partner_user_id,
                Client.manager_user_id,
                Job.number,
                Job.name,
                Job.billing_basis,
                Job.service_id,
                Job.partner_user_id,
                Job.manager_user_id,
            )
            .select_from(TimeEntry)
            .join(
                Client,
                and_(
                    Client.organization_id == TimeEntry.organization_id,
                    Client.id == TimeEntry.client_id,
                ),
            )
            .outerjoin(
                Job,
                and_(Job.organization_id == TimeEntry.organization_id, Job.id == TimeEntry.job_id),
            )
            .where(
                TimeEntry.organization_id == organization_id,
                TimeEntry.is_billable.is_(True),
                TimeEntry.client_id.is_not(None),
                TimeEntry.entry_date <= as_at,
                _status_condition(
                    organization_id,
                    as_at,
                    today,
                    TimeEntry.billing_status,
                    "time_entry_id",
                    TimeEntry.id,
                ),
                *_filter_conditions(f, TimeEntry.client_id, TimeEntry.job_id),
            )
        )
        if require_approval:
            stmt = stmt.where(approved_timesheet())
        atoms: list[WipAtom] = []
        for row in (await self.session.execute(stmt)).all():
            dims = _Dims(
                row[10],
                row[11],
                row[12],
                row[13],
                row[14],
                row[15],
                row[16],
                row[17],
                row[18],
                row[19],
            )
            client_id: uuid.UUID | None = row[1]
            job_id: uuid.UUID | None = row[2]
            assert client_id is not None
            component, value = "time", int(row[6])
            if dims.billing_basis == "fixed" and job_id is not None:
                component = "fixed_fee"
                job = fixed.get(job_id)
                if job is not None:
                    job.time_entry_ids.append(row[0])
                    if job.recognised is not None:
                        # The fee replaces the time: it counts as the fixed fee item.
                        value = 0
            atoms.append(
                _atom(
                    "time",
                    row[0],
                    client_id,
                    job_id,
                    dims,
                    row[4],
                    component,
                    value,
                    minutes=int(row[5]),
                    charge_value=int(row[6]),
                    rate=int(row[7]),
                    description=row[8],
                    person_user_id=row[3],
                    activity_code_id=row[9],
                )
            )
        return atoms

    async def _disbursements(
        self, organization_id: uuid.UUID, as_at: date, today: date, f: WipFilters
    ) -> list[WipAtom]:
        if f.kinds is not None and "disbursement" not in f.kinds:
            return []
        stmt = (
            select(
                Disbursement.id,
                Disbursement.client_id,
                Disbursement.job_id,
                Disbursement.created_by,
                Disbursement.incurred_on,
                Disbursement.amount,
                Disbursement.description,
                Disbursement.supplier,
                Client.code,
                Client.name,
                Client.partner_user_id,
                Client.manager_user_id,
                Job.number,
                Job.name,
                Job.billing_basis,
                Job.service_id,
                Job.partner_user_id,
                Job.manager_user_id,
            )
            .select_from(Disbursement)
            .join(
                Client,
                and_(
                    Client.organization_id == Disbursement.organization_id,
                    Client.id == Disbursement.client_id,
                ),
            )
            .outerjoin(
                Job,
                and_(
                    Job.organization_id == Disbursement.organization_id,
                    Job.id == Disbursement.job_id,
                ),
            )
            .where(
                Disbursement.organization_id == organization_id,
                Disbursement.is_billable.is_(True),
                Disbursement.incurred_on <= as_at,
                _status_condition(
                    organization_id,
                    as_at,
                    today,
                    Disbursement.billing_status,
                    "disbursement_id",
                    Disbursement.id,
                ),
                *_filter_conditions(f, Disbursement.client_id, Disbursement.job_id),
            )
        )
        return [
            _atom(
                "disbursement",
                row[0],
                row[1],
                row[2],
                _Dims(
                    row[8],
                    row[9],
                    row[10],
                    row[11],
                    row[12],
                    row[13],
                    row[14],
                    row[15],
                    row[16],
                    row[17],
                ),
                row[4],
                "expense",
                int(row[5]),
                description=row[6],
                supplier=row[7],
                person_user_id=row[3],
            )
            for row in (await self.session.execute(stmt)).all()
        ]

    async def _expenses(
        self,
        organization_id: uuid.UUID,
        as_at: date,
        today: date,
        timezone: str,
        f: WipFilters,
    ) -> list[WipAtom]:
        if f.kinds is not None and "expense" not in f.kinds:
            return []
        claim = aliased(ExpenseClaim)
        if as_at >= today:
            # Today: the one rule expenses publish (docs/modules/expenses.md, "Work in progress").
            conditions = wip_expense_conditions(organization_id)
        else:
            conditions = [
                Expense.organization_id == organization_id,
                Expense.is_billable.is_(True),
                Expense.client_id.is_not(None),
                Expense.recharge_amount > 0,
                Expense.incurred_on <= as_at,
                _status_condition(
                    organization_id,
                    as_at,
                    today,
                    Expense.billing_status,
                    "expense_id",
                    Expense.id,
                ),
                exists().where(
                    claim.organization_id == Expense.organization_id,
                    claim.id == Expense.claim_id,
                    claim.status == "approved",
                    claim.decided_at < end_of_day_utc(as_at, timezone),
                ),
            ]
        stmt = (
            select(
                Expense.id,
                Expense.client_id,
                Expense.job_id,
                Expense.claim_id,
                Expense.incurred_on,
                Expense.recharge_amount,
                Expense.description,
                Expense.supplier,
                ExpenseClaim.user_id,
                Client.code,
                Client.name,
                Client.partner_user_id,
                Client.manager_user_id,
                Job.number,
                Job.name,
                Job.billing_basis,
                Job.service_id,
                Job.partner_user_id,
                Job.manager_user_id,
            )
            .select_from(Expense)
            .join(
                ExpenseClaim,
                and_(
                    ExpenseClaim.organization_id == Expense.organization_id,
                    ExpenseClaim.id == Expense.claim_id,
                ),
            )
            .join(
                Client,
                and_(
                    Client.organization_id == Expense.organization_id,
                    Client.id == Expense.client_id,
                ),
            )
            .outerjoin(
                Job,
                and_(Job.organization_id == Expense.organization_id, Job.id == Expense.job_id),
            )
            .where(*conditions, *_filter_conditions(f, Expense.client_id, Expense.job_id))
        )
        return [
            _atom(
                "expense",
                row[0],
                cast(uuid.UUID, row[1]),
                row[2],
                _Dims(
                    row[9],
                    row[10],
                    row[11],
                    row[12],
                    row[13],
                    row[14],
                    row[15],
                    row[16],
                    row[17],
                    row[18],
                ),
                row[4],
                "expense",
                int(row[5]),
                description=row[6],
                supplier=row[7],
                person_user_id=row[8],
                claim_id=row[3],
            )
            for row in (await self.session.execute(stmt)).all()
        ]

    # --- Fixed fees --------------------------------------------------------------------

    async def _fixed_fee_jobs(
        self,
        organization_id: uuid.UUID,
        as_at: date,
        today: date,
        timezone: str,
        mode: str,
        f: WipFilters,
    ) -> dict[uuid.UUID, FixedFeeJob]:
        """The live fixed-fee jobs with their recognised fee, billed so far and logged time."""
        stmt = (
            select(
                Job,
                Client.code,
                Client.name,
                Client.partner_user_id,
                Client.manager_user_id,
            )
            .join(
                Client,
                and_(Client.organization_id == Job.organization_id, Client.id == Job.client_id),
            )
            .where(
                Job.organization_id == organization_id,
                Job.billing_basis == "fixed",
                Job.fixed_fee.is_not(None),
                Job.fixed_fee > 0,
                Job.deleted_at.is_(None),
                *_filter_conditions(f, Job.client_id, Job.id),
            )
        )
        found: dict[uuid.UUID, FixedFeeJob] = {}
        for row in (await self.session.execute(stmt)).all():
            job: Job = row[0]
            dims = _Dims(
                row[1],
                row[2],
                row[3],
                row[4],
                job.number,
                job.name,
                job.billing_basis,
                job.service_id,
                job.partner_user_id,
                job.manager_user_id,
            )
            found[job.id] = FixedFeeJob(job=job, dims=dims)
        if not found:
            return found
        logged = (
            select(
                TimeEntry.job_id,
                func.coalesce(func.sum(TimeEntry.minutes), 0),
                func.max(TimeEntry.entry_date),
            )
            .where(
                TimeEntry.organization_id == organization_id,
                TimeEntry.job_id.in_(list(found)),
                TimeEntry.entry_date <= as_at,
            )
            .group_by(TimeEntry.job_id)
        )
        latest: dict[uuid.UUID, date] = {}
        for job_id, minutes, last in (await self.session.execute(logged)).all():
            assert job_id is not None
            found[job_id].logged_minutes = int(minutes)
            latest[job_id] = last
        billed = await self.fixed_fee_billed(organization_id, list(found), as_at, today)
        zone = ZoneInfo(timezone)
        for job_id, item in found.items():
            job = item.job
            item.billed = billed.get(job_id, 0)
            assert job.fixed_fee is not None
            item.recognised = recognised_fixed_fee(
                mode,
                fee=job.fixed_fee,
                status=job.status,
                budget_minutes=job.budget_minutes,
                logged_minutes=item.logged_minutes,
            )
            if item.recognised is None:
                continue
            if mode == "by_budget_progress":
                item.recognised_on = latest.get(job_id) or as_at
            else:
                stamp = job.completed_at or job.closed_at or job.updated_at
                item.recognised_on = stamp.astimezone(zone).date()
                if item.recognised_on > as_at:
                    # Completed after the day: the fee was not recognised yet.
                    item.recognised = None
        return found

    async def fixed_fee_billed(
        self, organization_id: uuid.UUID, job_ids: Collection[uuid.UUID], as_at: date, today: date
    ) -> dict[uuid.UUID, int]:
        """Net on the jobs' fixed-fee invoice lines: issued by ``as_at`` and, today, drafts too."""
        wanted = set(job_ids)
        if not wanted:
            return {}
        if as_at >= today:
            live: ColumnElement[bool] = Invoice.status.in_(("issued", "draft"))
        else:
            live = and_(Invoice.status == "issued", Invoice.issue_date <= as_at)
        stmt = (
            select(InvoiceLine.job_id, func.coalesce(func.sum(InvoiceLine.net_amount), 0))
            .join(
                Invoice,
                and_(
                    Invoice.organization_id == InvoiceLine.organization_id,
                    Invoice.id == InvoiceLine.invoice_id,
                ),
            )
            .where(
                InvoiceLine.organization_id == organization_id,
                InvoiceLine.kind == "fixed_fee",
                InvoiceLine.job_id.in_(wanted),
                live,
            )
            .group_by(InvoiceLine.job_id)
        )
        return {row[0]: int(row[1]) for row in (await self.session.execute(stmt)).all() if row[0]}

    @staticmethod
    def _fixed_fee_atoms(
        fixed: dict[uuid.UUID, FixedFeeJob], mode: str, as_at: date
    ) -> list[WipAtom]:
        atoms: list[WipAtom] = []
        for job_id, item in fixed.items():
            if item.recognised is None:
                continue
            value = max(0, item.recognised - item.billed)
            if value == 0:
                continue
            fee = item.job.fixed_fee or 0
            progress = None
            if mode == "by_budget_progress" and item.job.budget_minutes:
                progress = percentage_bp(
                    min(item.logged_minutes, item.job.budget_minutes), item.job.budget_minutes
                )
            atoms.append(
                _atom(
                    "fixed_fee",
                    job_id,
                    item.job.client_id,
                    job_id,
                    item.dims,
                    item.recognised_on or as_at,
                    "fixed_fee",
                    value,
                    description=f"{item.job.name} (fixed fee)",
                    fixed_fee=FixedFeeInfo(
                        fee=fee,
                        recognised=item.recognised,
                        billed=item.billed,
                        mode=mode,
                        progress_bp=progress,
                        time_entry_ids=tuple(item.time_entry_ids),
                    ),
                )
            )
        return atoms

    # --- Adjustments -------------------------------------------------------------------

    async def adjustment_atoms(
        self, organization_id: uuid.UUID, as_at: date, today: date, f: WipFilters
    ) -> list[WipAtom]:
        """The amount adjustments in force: not reversed and not settled by a later billing."""
        if f.kinds is not None and "adjustment" not in f.kinds:
            return []
        stmt = select(WipMovement).where(
            WipMovement.organization_id == organization_id,
            WipMovement.scope == "amount",
            WipMovement.effective_date <= as_at,
        )
        movements = list((await self.session.execute(stmt)).scalars())
        if not movements:
            return []
        roots = {m.id: m for m in movements if m.kind != "reversal"}
        reversed_ids = {m.reversal_of_id for m in movements if m.kind == "reversal"}
        live = [m for m in roots.values() if m.id not in reversed_ids]
        if not live:
            return []
        last_billed = await self._last_allocation(organization_id, as_at, today)
        live = [m for m in live if not self._settled(m, last_billed)]
        if not live:
            return []
        client_ids = {m.client_id for m in live}
        job_ids = {m.job_id for m in live if m.job_id}
        clients = {
            c.id: c
            for c in (
                await self.session.execute(
                    select(Client).where(
                        Client.organization_id == organization_id, Client.id.in_(client_ids)
                    )
                )
            ).scalars()
        }
        jobs: dict[uuid.UUID, Job] = {}
        if job_ids:
            jobs = {
                j.id: j
                for j in (
                    await self.session.execute(
                        select(Job).where(
                            Job.organization_id == organization_id, Job.id.in_(job_ids)
                        )
                    )
                ).scalars()
            }
        atoms: list[WipAtom] = []
        for m in live:
            client = clients.get(m.client_id)
            if client is None:
                continue
            job = jobs.get(m.job_id) if m.job_id else None
            dims = _Dims(
                client.code,
                client.name,
                client.partner_user_id,
                client.manager_user_id,
                job.number if job else None,
                job.name if job else None,
                job.billing_basis if job else None,
                job.service_id if job else None,
                job.partner_user_id if job else None,
                job.manager_user_id if job else None,
            )
            if not self._matches(f, dims, m.client_id, m.job_id):
                continue
            atoms.append(
                _atom(
                    "adjustment",
                    m.id,
                    m.client_id,
                    m.job_id,
                    dims,
                    m.effective_date,
                    m.component,
                    m.amount,
                    description=m.reason,
                    person_user_id=m.actor_user_id,
                    movement_kind=m.kind,
                )
            )
        return atoms

    @staticmethod
    def _matches(
        f: WipFilters, dims: _Dims, client_id: uuid.UUID, job_id: uuid.UUID | None
    ) -> bool:
        if f.client_id is not None and client_id != f.client_id:
            return False
        if f.job_id is not None and job_id != f.job_id:
            return False
        if f.service_id is not None and dims.service_id != f.service_id:
            return False
        partner = (
            dims.client_partner if f.client_responsible else dims.job_partner or dims.client_partner
        )
        manager = (
            dims.client_manager if f.client_responsible else dims.job_manager or dims.client_manager
        )
        if f.partner_user_id is not None and partner != f.partner_user_id:
            return False
        return not (f.manager_user_id is not None and manager != f.manager_user_id)

    @staticmethod
    def _settled(
        movement: WipMovement, last_billed: dict[tuple[uuid.UUID, uuid.UUID | None], datetime]
    ) -> bool:
        latest = last_billed.get((movement.client_id, movement.job_id))
        return latest is not None and latest > movement.created_at

    async def _last_allocation(
        self, organization_id: uuid.UUID, as_at: date, today: date
    ) -> dict[tuple[uuid.UUID, uuid.UUID | None], datetime]:
        """When the job (or the client's job-less items) was last billed or written off by
        ``as_at``: the newest live allocation of an issued invoice (a draft too today) or of a
        write-off dated by then."""
        draft = Invoice.status == "draft" if as_at >= today else false()
        dated: ColumnElement[bool] = or_(
            and_(Invoice.status == "issued", Invoice.issue_date <= as_at),
            draft,
            WipWriteOff.written_off_on <= as_at,
        )
        selects = []
        for model, fk in (
            (TimeEntry, WipAllocation.time_entry_id),
            (Disbursement, WipAllocation.disbursement_id),
            (Expense, WipAllocation.expense_id),
        ):
            selects.append(
                select(model.client_id, model.job_id, WipAllocation.created_at)
                .select_from(WipAllocation)
                .join(
                    model,
                    and_(model.organization_id == WipAllocation.organization_id, model.id == fk),
                )
                .outerjoin(
                    Invoice,
                    and_(
                        Invoice.organization_id == WipAllocation.organization_id,
                        Invoice.id == WipAllocation.invoice_id,
                    ),
                )
                .outerjoin(
                    WipWriteOff,
                    and_(
                        WipWriteOff.organization_id == WipAllocation.organization_id,
                        WipWriteOff.id == WipAllocation.write_off_id,
                    ),
                )
                .where(
                    WipAllocation.organization_id == organization_id,
                    WipAllocation.reversed_at.is_(None),
                    dated,
                )
            )
        events = union_all(*selects).subquery("events")
        grouped = select(
            events.c.client_id, events.c.job_id, func.max(events.c.created_at)
        ).group_by(events.c.client_id, events.c.job_id)
        return {(row[0], row[1]): row[2] for row in (await self.session.execute(grouped)).all()}
