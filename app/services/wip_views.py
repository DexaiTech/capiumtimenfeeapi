"""The WIP screens: the summary by client, job, partner, manager or service, the item list, the
ageing as at a day, and the original per-client views (`GET /wip`, `GET /clients/{id}/wip`).

Every figure comes from the WIP atoms (``app/repositories/wip_atoms.py``), so the summary, the
details, the ageing, the client view and the historic report agree to the penny.
"""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.db.models import Organization
from app.repositories.billing_common import BillingReferenceRepository
from app.repositories.wip import WipSummaryFilters
from app.repositories.wip_atoms import WipAtom, WipAtomRepository, WipFilters
from app.schemas.billing import (
    AgeBuckets,
    ClientRef,
    ClientWipRead,
    FixedFeeRead,
    JobRef,
    PersonRef,
    WipClientSummary,
    WipComposition,
    WipItemRead,
    WipTotals,
    hours_of,
)
from app.schemas.wip import WipAges, WipGroupBy, WipGroupRow, WipGroupTotals, WipSummaryRead
from app.services.billing_common import get_client_or_404
from app.services.policy import OrgContext
from app.services.reports import practice_today
from app.services.wip_engine import (
    Group,
    SortSpec,
    aggregate,
    page_of,
    sort_groups,
    sum_groups,
)
from app.services.wip_history import WipHistoryService
from app.utils.pagination import Page, PageParams
from app.utils.wip_rules import age_days

ITEM_SORTS = ("date", "value", "client")


def composition_of(group: Group) -> WipComposition:
    return WipComposition(
        time=group.time, fixed_fee=group.fixed_fee, expense=group.expense, total=group.total
    )


def _totals(group: Group) -> WipGroupTotals:
    return WipGroupTotals(
        minutes=group.minutes,
        hours=hours_of(group.minutes) if group.minutes else Decimal("0.00"),
        time=group.time,
        fixed_fee=group.fixed_fee,
        expense=group.expense,
        total=group.total,
        ages=WipAges(
            days_0_30=group.ages["0_30"],
            days_31_60=group.ages["31_60"],
            days_61_90=group.ages["61_90"],
            days_90_plus=group.ages["90_plus"],
        ),
        oldest_date=group.oldest,
        item_count=group.count,
        over_90_bp=group.over_90_bp,
    )


def _job_ref(atom: WipAtom) -> JobRef | None:
    if atom.job_id is None or atom.job_number is None:
        return None
    return JobRef(
        id=atom.job_id,
        number=atom.job_number,
        name=atom.job_name or "",
        billing_basis=atom.billing_basis or "hourly",
    )


def item_read(atom: WipAtom, as_at: date, names: dict[uuid.UUID, str]) -> WipItemRead:
    is_time = atom.kind == "time"
    person = (
        PersonRef(user_id=atom.person_user_id, full_name=names.get(atom.person_user_id))
        if atom.person_user_id
        else None
    )
    info = atom.fixed_fee
    return WipItemRead(
        kind=atom.kind,  # type: ignore[arg-type]
        id=atom.id,
        date=atom.item_date,
        job=_job_ref(atom),
        person=person,
        description=atom.description,
        minutes=atom.minutes if is_time else None,
        hours=hours_of(atom.minutes) if is_time else None,
        rate=atom.rate,
        value=atom.value,
        activity_code_id=atom.activity_code_id,
        supplier=atom.supplier,
        client=ClientRef(id=atom.client_id, code=atom.client_code, name=atom.client_name),
        age_days=age_days(atom.item_date, as_at),
        component=atom.component,  # type: ignore[arg-type]
        charge_value=atom.charge_value,
        selectable=atom.kind in ("time", "disbursement", "expense"),
        claim_id=atom.claim_id,
        fixed_fee=FixedFeeRead(
            fee=info.fee,
            recognised=info.recognised,
            billed=info.billed,
            mode=info.mode,
            progress_bp=info.progress_bp,
            time_entry_ids=list(info.time_entry_ids),
        )
        if info
        else None,
        movement_kind=atom.movement_kind,
    )


@dataclass(frozen=True, slots=True)
class SummaryQuery:
    group_by: WipGroupBy = "client"
    filters: WipFilters = WipFilters()
    q: str | None = None
    sort: str = "name"
    descending: bool = False
    as_at: date | None = None


class WipViewService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.atoms = WipAtomRepository(session)
        self.history = WipHistoryService(session)
        self.refs = BillingReferenceRepository(session)

    async def _live(
        self, organization: Organization, filters: WipFilters
    ) -> tuple[list[WipAtom], date]:
        today = practice_today(organization)
        atoms = await self.atoms.load_atoms(
            organization.id,
            as_at=today,
            today=today,
            timezone=organization.timezone,
            require_approval=organization.require_timesheet_approval,
            fixed_fee_mode=organization.fixed_fee_wip_mode,
            filters=filters,
        )
        return atoms, today

    # --- The original views ------------------------------------------------------------

    async def legacy_summary(
        self, ctx: OrgContext, f: WipSummaryFilters, params: PageParams
    ) -> Page[WipClientSummary]:
        """``GET /wip``: one row per client with WIP, by client name; the partner and manager
        filters are the client's own."""
        filters = WipFilters(
            client_id=f.client_id,
            job_id=f.job_id,
            partner_user_id=f.partner_user_id,
            manager_user_id=f.manager_user_id,
            client_responsible=True,
        )
        atoms, today = await self._live(ctx.organization, filters)
        groups = aggregate(atoms, "client", today, client_responsible=True)
        rows = sort_groups(list(groups.values()), SortSpec("name", False))
        page = page_of(rows, params)
        return Page(
            items=[
                WipClientSummary(
                    client=ClientRef(
                        id=g.client_id,  # type: ignore[arg-type]
                        code=g.client_code or "",
                        name=g.client_name or "",
                    ),
                    partner_user_id=g.partner_user_id,
                    manager_user_id=g.manager_user_id,
                    minutes=g.minutes,
                    hours=hours_of(g.minutes),
                    time_value=g.time_value,
                    disbursement_value=g.disbursement_value,
                    total=g.total,
                    oldest_date=g.oldest,
                    ages=AgeBuckets(
                        days_0_30=g.ages["0_30"],
                        days_31_60=g.ages["31_60"],
                        days_61_90=g.ages["61_90"],
                        days_91_plus=g.ages["90_plus"],
                    ),
                    item_count=g.count,
                    expense_value=g.expense_value,
                    composition=composition_of(g),
                )
                for g in page.items
            ],
            next_cursor=page.next_cursor,
        )

    async def client_wip(
        self, ctx: OrgContext, client_id: uuid.UUID, job_id: uuid.UUID | None
    ) -> ClientWipRead:
        """``GET /clients/{id}/wip``: every WIP item of the client, oldest first, and the totals."""
        client = await get_client_or_404(self.refs, ctx.organization_id, client_id)
        atoms, today = await self._live(
            ctx.organization, WipFilters(client_id=client.id, job_id=job_id)
        )
        names = await self.refs.member_names(
            ctx.organization_id, [a.person_user_id for a in atoms]
        )
        atoms.sort(key=lambda a: (a.item_date, a.kind != "time"))
        total = sum_groups(list(aggregate(atoms, "client", today).values()))
        return ClientWipRead(
            client=ClientRef.from_model(client),
            items=[item_read(a, today, names) for a in atoms],
            totals=WipTotals(
                minutes=total.minutes,
                hours=hours_of(total.minutes),
                time_value=total.time_value,
                disbursement_value=total.disbursement_value,
                total=total.total,
            ),
            composition=composition_of(total),
        )

    async def _check_scope(self, ctx: OrgContext, f: WipFilters) -> None:
        """A client or job the filter names must be the practice's: another practice's is 404."""
        if f.client_id is not None:
            await get_client_or_404(self.refs, ctx.organization_id, f.client_id)
        if f.job_id is not None and await self.refs.get_job(ctx.organization_id, f.job_id) is None:
            raise NotFoundError("Job not found.")

    # --- The summary and the ageing ----------------------------------------------------

    async def summary(
        self, ctx: OrgContext, q: SummaryQuery, params: PageParams
    ) -> tuple[WipSummaryRead, str | None]:
        """Rows grouped by ``q.group_by`` as at ``q.as_at`` (today by default; a closed day is
        read from its frozen snapshot), with the totals of every row that matches."""
        await self._check_scope(ctx, q.filters)
        day = await self.history.day(ctx, q.as_at, q.filters)
        groups = list(aggregate(day.atoms, q.group_by, day.as_at).values())
        await self._name_groups(ctx, groups, q.group_by)
        if q.q:
            needle = q.q.lower()
            groups = [
                g
                for g in groups
                if needle in g.name.lower()
                or needle in (g.code or "").lower()
                or needle in (g.client_name or "").lower()
            ]
        ordered = sort_groups(groups, SortSpec(q.sort, q.descending))
        page = page_of(ordered, params)
        return (
            WipSummaryRead(
                group_by=q.group_by,
                as_at=day.as_at,
                rows=[self._row(g, q.group_by) for g in page.items],
                totals=_totals(sum_groups(groups)),
            ),
            page.next_cursor,
        )

    @staticmethod
    def _row(group: Group, group_by: WipGroupBy) -> WipGroupRow:
        client = (
            ClientRef(id=group.client_id, code=group.client_code or "", name=group.client_name or "")
            if group_by == "job" and group.client_id
            else None
        )
        return WipGroupRow(
            **_totals(group).model_dump(),
            group_by=group_by,
            key=group.key,
            name=group.name,
            code=group.code,
            client=client,
        )

    async def _name_groups(self, ctx: OrgContext, groups: list[Group], group_by: str) -> None:
        """Names for the groups that are people or services (the atoms only carry ids)."""
        if group_by in ("partner", "manager"):
            names = await self.refs.member_names(ctx.organization_id, [g.key for g in groups])
            fallback = "Unassigned"
        elif group_by == "service":
            names = await self.atoms.service_names(
                ctx.organization_id, {g.key for g in groups if g.key}
            )
            fallback = "No service"
        else:
            return
        for group in groups:
            group.name = names.get(group.key, fallback) if group.key else fallback

    # --- Items -------------------------------------------------------------------------

    async def items(
        self,
        ctx: OrgContext,
        f: WipFilters,
        *,
        min_age_days: int | None,
        q: str | None,
        sort: str,
        descending: bool,
        params: PageParams,
    ) -> Page[WipItemRead]:
        await self._check_scope(ctx, f)
        atoms, today = await self._live(ctx.organization, f)
        if min_age_days is not None:
            atoms = [a for a in atoms if age_days(a.item_date, today) >= min_age_days]
        if q:
            needle = q.lower()
            atoms = [
                a
                for a in atoms
                if needle in (a.description or "").lower()
                or needle in a.client_name.lower()
                or needle in (a.job_name or "").lower()
            ]
        if sort == "value":
            atoms.sort(key=lambda a: a.value, reverse=descending)
        elif sort == "client":
            atoms.sort(key=lambda a: (a.client_name.lower(), a.item_date), reverse=descending)
        else:
            atoms.sort(key=lambda a: (a.item_date, str(a.id)), reverse=descending)
        page = page_of(atoms, params)
        names = await self.refs.member_names(
            ctx.organization_id, [a.person_user_id for a in page.items]
        )
        return Page(
            items=[item_read(a, today, names) for a in page.items], next_cursor=page.next_cursor
        )
