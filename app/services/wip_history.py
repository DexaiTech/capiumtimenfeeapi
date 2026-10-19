"""Historic WIP: the WIP of a closed day, frozen the first time it is reported, and the report
on it.

A closed day (before today in the practice's time zone) is built once, from the item dates and the
allocations dated after it, and stored in ``wip_snapshots`` / ``wip_snapshot_items`` (append-only).
Every later request for that day reads the stored rows, so a later invoice, write-off, re-rate or
move of an item to another job can never change a figure that was reported. Today is computed live
and not stored. The write-ons and write-offs come from the immutable ledger, limited to what was
recorded before the day was frozen.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, cast

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.db.models import Client, Job, WipMovement, WipSnapshot, WipSnapshotItem
from app.db.session import unit_of_work
from app.repositories.billing_common import BillingReferenceRepository
from app.repositories.reports import ReportRepository
from app.repositories.wip import WipRepository
from app.repositories.wip_atoms import WipAtom, WipAtomRepository, WipFilters
from app.repositories.wip_history import WipHistoryRepository
from app.schemas.billing import ClientRef, hours_of
from app.schemas.wip import WipAges
from app.schemas.wip_reports import (
    HistoricGroupBy,
    HistoricWipAmounts,
    HistoricWipFilters,
    HistoricWipItem,
    HistoricWipItemsQuery,
    HistoricWipQuery,
    HistoricWipReport,
    HistoricWipRow,
    SnapshotInfo,
)
from app.services.policy import OrgContext
from app.services.reports import practice_today
from app.services.wip_engine import (
    Group,
    aggregate,
    group_key,
    matches,
    new_group,
    page_of,
    totals_of,
)
from app.utils.ids import new_id
from app.utils.pagination import Page, PageParams
from app.utils.wip_rules import age_days

NOTICE = (
    "History is immutable: the figures of a closed day were stored when it was first reported "
    "and do not change after later invoices, write-offs or rate changes."
)
_BILLING_ORDER = {"hourly": 0, "fixed": 1, "non_billable": 2}
_WRITE_KINDS = ("write_on", "write_off", "credit_write_off")


def atom_from_item(item: WipSnapshotItem) -> WipAtom:
    return WipAtom(
        kind=item.kind,
        id=item.item_id,
        client_id=item.client_id,
        client_code=item.client_code,
        client_name=item.client_name,
        job_id=item.job_id,
        job_number=item.job_number,
        job_name=item.job_name,
        billing_basis=item.billing_basis,
        service_id=item.service_id,
        partner_user_id=item.partner_user_id,
        manager_user_id=item.manager_user_id,
        client_partner_user_id=item.partner_user_id,
        client_manager_user_id=item.manager_user_id,
        item_date=item.item_date,
        component=item.component,
        value=item.value,
        minutes=item.minutes,
        charge_value=item.charge_value,
        description=item.description,
        person_user_id=item.person_user_id,
    )


def item_from_atom(
    snapshot_id: uuid.UUID, organization_id: uuid.UUID, atom: WipAtom
) -> WipSnapshotItem:
    return WipSnapshotItem(
        id=new_id(),
        organization_id=organization_id,
        snapshot_id=snapshot_id,
        kind=atom.kind,
        component=atom.component,
        item_id=atom.id,
        client_id=atom.client_id,
        client_code=atom.client_code,
        client_name=atom.client_name,
        job_id=atom.job_id,
        job_number=atom.job_number,
        job_name=atom.job_name,
        billing_basis=atom.billing_basis,
        partner_user_id=atom.partner_user_id,
        manager_user_id=atom.manager_user_id,
        service_id=atom.service_id,
        person_user_id=atom.person_user_id,
        item_date=atom.item_date,
        minutes=atom.minutes,
        value=atom.value,
        charge_value=atom.charge_value,
        description=atom.description,
    )


def _pseudo_atom(movement: WipMovement, client: Client, job: Job | None) -> WipAtom:
    """A movement as an atom without value: it only carries the client, job and people."""
    return WipAtom(
        kind="adjustment",
        id=movement.id,
        client_id=client.id,
        client_code=client.code,
        client_name=client.name,
        job_id=job.id if job else None,
        job_number=job.number if job else None,
        job_name=job.name if job else None,
        billing_basis=job.billing_basis if job else None,
        service_id=job.service_id if job else None,
        partner_user_id=(job.partner_user_id if job else None) or client.partner_user_id,
        manager_user_id=(job.manager_user_id if job else None) or client.manager_user_id,
        client_partner_user_id=client.partner_user_id,
        client_manager_user_id=client.manager_user_id,
        item_date=movement.effective_date,
        component=movement.component,
        value=0,
    )


def _key_text(group: Group, group_by: str) -> str | None:
    if group_by == "billing_type":
        return group.billing_type
    return str(group.key) if group.key else None


def _ordered(groups: Iterable[Group], group_by: str) -> list[Group]:
    if group_by == "billing_type":
        return sorted(groups, key=lambda g: _BILLING_ORDER.get(g.billing_type or "", 9))
    if group_by == "job":
        return sorted(
            groups, key=lambda g: ((g.client_name or "").lower(), g.code or "", str(g.key))
        )
    return sorted(groups, key=lambda g: (g.name.lower(), str(g.key)))


def _amounts(group: Group) -> HistoricWipAmounts:
    return HistoricWipAmounts(
        minutes=group.minutes,
        hours=hours_of(group.minutes) if group.minutes else Decimal("0.00"),
        time_wip=group.time,
        fixed_fee_wip=group.fixed_fee,
        expense_wip=group.expense,
        write_ons=group.write_ons,
        write_offs=group.write_offs,
        total=group.total,
        ages=WipAges(
            days_0_30=group.ages["0_30"],
            days_31_60=group.ages["31_60"],
            days_61_90=group.ages["61_90"],
            days_90_plus=group.ages["90_plus"],
        ),
        oldest_date=group.oldest,
        item_count=group.count,
    )


@dataclass(slots=True)
class Day:
    """The WIP of one day: its atoms (filtered) and the snapshot they came from, if frozen."""

    as_at: date
    atoms: list[WipAtom]
    snapshot: WipSnapshot | None


class WipHistoryService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.atoms = WipAtomRepository(session)
        self.history = WipHistoryRepository(session)
        self.refs = BillingReferenceRepository(session)
        self.reports = ReportRepository(session)
        self.wip = WipRepository(session)

    # --- The day -----------------------------------------------------------------------

    async def day(self, ctx: OrgContext, as_at: date | None, f: WipFilters) -> Day:
        """The atoms of ``as_at`` (default today): live for today, frozen for a closed day.
        A day after today is 422."""
        org = ctx.organization
        today = practice_today(org)
        day = as_at or today
        if day > today:
            raise ValidationError.for_field(
                "as_at", "The date cannot be in the future.", code="future_date"
            )
        if day == today:
            atoms = await self.atoms.load_atoms(
                org.id,
                as_at=today,
                today=today,
                timezone=org.timezone,
                require_approval=org.require_timesheet_approval,
                fixed_fee_mode=org.fixed_fee_wip_mode,
                filters=f,
            )
            return Day(day, atoms, None)
        snapshot = await self._snapshot(ctx, day, today)
        items = await self.history.snapshot_items(org.id, snapshot.id)
        return Day(day, [a for a in map(atom_from_item, items) if matches(a, f)], snapshot)

    async def _snapshot(self, ctx: OrgContext, as_at: date, today: date) -> WipSnapshot:
        """The stored snapshot of the day; built and stored on first use. Two requests that race
        to freeze the same day both end up reading the winner's rows."""
        org = ctx.organization
        found = await self.history.get_snapshot(org.id, as_at)
        if found is not None:
            return found
        atoms = await self.atoms.load_atoms(
            org.id,
            as_at=as_at,
            today=today,
            timezone=org.timezone,
            require_approval=org.require_timesheet_approval,
            fixed_fee_mode=org.fixed_fee_wip_mode,
            filters=WipFilters(),
        )
        snapshot = WipSnapshot(
            id=new_id(),
            organization_id=org.id,
            as_at=as_at,
            source="request",
            fixed_fee_wip_mode=org.fixed_fee_wip_mode,
            require_timesheet_approval=org.require_timesheet_approval,
            item_count=len(atoms),
            total=sum(a.value for a in atoms),
            taken_by=ctx.user.id,
        )
        items = [item_from_atom(snapshot.id, org.id, a) for a in atoms]
        async with unit_of_work(self.session):
            try:
                async with self.session.begin_nested():
                    await self.history.add_snapshot(snapshot, items)
            except IntegrityError:
                winner = await self.history.get_snapshot(org.id, as_at)
                if winner is None:
                    raise
                return winner
            await self.session.refresh(snapshot)
        return snapshot

    # --- The report --------------------------------------------------------------------

    async def _check_refs(self, ctx: OrgContext, q: HistoricWipQuery) -> None:
        org_id = ctx.organization_id
        if q.client_id is not None and not await self.reports.client_exists(org_id, q.client_id):
            raise NotFoundError("Client not found.")
        if q.job_id is not None and not await self.reports.job_exists(org_id, q.job_id):
            raise NotFoundError("Job not found.")
        for user_id in (q.user_id, q.partner_user_id, q.manager_user_id):
            if user_id is not None and not await self.reports.member_exists(org_id, user_id):
                raise NotFoundError("Member not found.")
        if q.service_id is not None and not await self.reports.service_exists(org_id, q.service_id):
            raise NotFoundError("Service not found.")

    async def report(self, ctx: OrgContext, q: HistoricWipQuery) -> HistoricWipReport:
        group_by: HistoricGroupBy = q.group_by or "job"
        await self._check_refs(ctx, q)
        f = WipFilters(
            client_id=q.client_id,
            job_id=q.job_id,
            partner_user_id=q.partner_user_id,
            manager_user_id=q.manager_user_id,
            service_id=q.service_id,
        )
        day = await self.day(ctx, q.as_at, f)
        atoms = day.atoms
        if q.user_id is not None:
            atoms = [a for a in atoms if a.kind == "time" and a.person_user_id == q.user_id]
        groups = aggregate(atoms, group_by, day.as_at)
        await self._add_movements(ctx, day, group_by, f, groups)
        total = totals_of(atoms, day.as_at)
        total.write_ons = sum(g.write_ons for g in groups.values())
        total.write_offs = sum(g.write_offs for g in groups.values())
        rows = [
            HistoricWipRow(
                **_amounts(g).model_dump(),
                group_by=group_by,
                key=_key_text(g, group_by),
                name=g.name,
                code=g.code,
                client=(
                    ClientRef(id=g.client_id, code=g.client_code or "", name=g.client_name or "")
                    if group_by == "job" and g.client_id
                    else None
                ),
                billing_type=g.billing_type if group_by == "job" else None,
            )
            for g in _ordered(groups.values(), group_by)
        ]
        snapshot = day.snapshot
        return HistoricWipReport(
            rows=rows,
            totals=_amounts(total),
            filters=HistoricWipFilters(
                as_at=day.as_at,
                group_by=group_by,
                client_id=q.client_id,
                job_id=q.job_id,
                user_id=q.user_id,
                partner_user_id=q.partner_user_id,
                manager_user_id=q.manager_user_id,
                service_id=q.service_id,
            ),
            snapshot=SnapshotInfo(
                frozen=snapshot is not None,
                taken_at=snapshot.taken_at if snapshot else None,
                source=snapshot.source if snapshot else None,
            ),
            notice=NOTICE,
        )

    async def _add_movements(
        self,
        ctx: OrgContext,
        day: Day,
        group_by: str,
        f: WipFilters,
        groups: dict[tuple[object, ...], Group],
    ) -> None:
        """Add the write-ons and write-offs up to the day, net of reversals, to their groups. A
        group that has only movements (everything was written off) gets a row of its own."""
        org_id = ctx.organization_id
        movements = await self.history.movements_until(
            org_id, day.as_at, day.snapshot.taken_at if day.snapshot else None
        )
        if not movements:
            return
        originals = {m.id: m for m in movements if m.kind != "reversal"}
        clients = await self.refs.clients_by_ids(org_id, list({m.client_id for m in movements}))
        jobs = await self.wip.jobs_by_ids(org_id, [m.job_id for m in movements])
        for m in movements:
            kind = m.kind
            if kind == "reversal":
                original = originals.get(m.reversal_of_id) if m.reversal_of_id else None
                if original is None:
                    continue
                kind = original.kind
            client = clients.get(m.client_id)
            if kind not in _WRITE_KINDS or client is None:
                continue
            pseudo = _pseudo_atom(m, client, jobs.get(m.job_id) if m.job_id else None)
            if not matches(pseudo, f):
                continue
            key = group_key(pseudo, group_by)
            group = groups.get(key)
            if group is None:
                group = groups[key] = new_group(pseudo, group_by, key)
            if kind == "write_on":
                group.write_ons += m.amount
            else:
                group.write_offs -= m.amount

    # --- Drill-down --------------------------------------------------------------------

    async def items(
        self, ctx: OrgContext, q: HistoricWipItemsQuery, params: PageParams
    ) -> Page[HistoricWipItem]:
        org_id = ctx.organization_id
        if q.client_id is not None and not await self.reports.client_exists(org_id, q.client_id):
            raise NotFoundError("Client not found.")
        if q.job_id is not None and not await self.reports.job_exists(org_id, q.job_id):
            raise NotFoundError("Job not found.")
        f = WipFilters(
            client_id=q.client_id,
            job_id=q.job_id,
            partner_user_id=q.partner_user_id,
            manager_user_id=q.manager_user_id,
            service_id=q.service_id,
            kinds=frozenset(q.kind) if q.kind else None,
        )
        day = await self.day(ctx, q.as_at, f)
        atoms = day.atoms
        if q.billing_type is not None:
            atoms = [a for a in atoms if a.billing_basis == q.billing_type]
        page = page_of(atoms, params)
        return Page(
            items=[_item_read(a, day.as_at) for a in page.items], next_cursor=page.next_cursor
        )


def _item_read(atom: WipAtom, as_at: date) -> HistoricWipItem:
    fields: dict[str, Any] = {
        "kind": atom.kind,
        "component": atom.component,
    }
    return HistoricWipItem(
        id=atom.id,
        date=atom.item_date,
        age_days=age_days(atom.item_date, as_at),
        client=ClientRef(id=atom.client_id, code=atom.client_code, name=atom.client_name),
        job_id=atom.job_id,
        job_number=atom.job_number,
        job_name=atom.job_name,
        billing_type=atom.billing_basis,
        description=atom.description,
        minutes=atom.minutes,
        value=atom.value,
        charge_value=atom.charge_value,
        **cast(Any, fields),
    )
