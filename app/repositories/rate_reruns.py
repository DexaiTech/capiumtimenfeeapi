"""Queries of re-rating: the entries in scope, run records and their items. Scoped to one
organization."""

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import ColumnElement, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Client,
    Job,
    OrganizationMember,
    RateRerun,
    RateRerunItem,
    TimeEntry,
    User,
)
from app.repositories.time_paging import keyset_finish, keyset_query, parse_date
from app.utils.pagination import Page, PageParams


@dataclass(frozen=True, slots=True)
class ScopeFilters:
    client_ids: tuple[uuid.UUID, ...] = ()
    job_ids: tuple[uuid.UUID, ...] = ()
    user_ids: tuple[uuid.UUID, ...] = ()
    date_from: date | None = None
    date_to: date | None = None


@dataclass(frozen=True, slots=True)
class ItemView:
    item: RateRerunItem
    staff_code: str | None
    user_name: str | None
    client_code: str | None
    client_name: str | None
    job_number: str | None
    job_name: str | None


def _scope_conditions(organization_id: uuid.UUID, scope: ScopeFilters) -> list[ColumnElement[bool]]:
    conditions = [TimeEntry.organization_id == organization_id]
    if scope.client_ids:
        conditions.append(TimeEntry.client_id.in_(list(scope.client_ids)))
    if scope.job_ids:
        conditions.append(TimeEntry.job_id.in_(list(scope.job_ids)))
    if scope.user_ids:
        conditions.append(TimeEntry.user_id.in_(list(scope.user_ids)))
    if scope.date_from is not None:
        conditions.append(TimeEntry.entry_date >= scope.date_from)
    if scope.date_to is not None:
        conditions.append(TimeEntry.entry_date <= scope.date_to)
    return conditions


class RerateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def count_unbilled(self, organization_id: uuid.UUID, scope: ScopeFilters) -> int:
        stmt = (
            select(func.count())
            .select_from(TimeEntry)
            .where(
                *_scope_conditions(organization_id, scope), TimeEntry.billing_status == "unbilled"
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def unbilled_entries(
        self, organization_id: uuid.UUID, scope: ScopeFilters, *, lock: bool
    ) -> list[TimeEntry]:
        """The unbilled entries in scope in id order; ``lock`` takes them ``FOR UPDATE``."""
        stmt = (
            select(TimeEntry)
            .where(
                *_scope_conditions(organization_id, scope), TimeEntry.billing_status == "unbilled"
            )
            .order_by(TimeEntry.id)
            .execution_options(populate_existing=True)
        )
        if lock:
            stmt = stmt.with_for_update()
        return list((await self.session.execute(stmt)).scalars())

    async def other_status_counts(
        self, organization_id: uuid.UUID, scope: ScopeFilters
    ) -> dict[str, int]:
        """Entries in scope that are not unbilled, by billing status."""
        stmt = (
            select(TimeEntry.billing_status, func.count())
            .where(
                *_scope_conditions(organization_id, scope), TimeEntry.billing_status != "unbilled"
            )
            .group_by(TimeEntry.billing_status)
        )
        return {row[0]: int(row[1]) for row in (await self.session.execute(stmt)).all()}

    async def add_run(self, run: RateRerun, items: list[RateRerunItem]) -> None:
        self.session.add(run)
        await self.session.flush()
        self.session.add_all(items)
        await self.session.flush()

    async def get_run(
        self, organization_id: uuid.UUID, run_id: uuid.UUID
    ) -> tuple[RateRerun, str | None] | None:
        stmt = (
            select(RateRerun, User.full_name)
            .outerjoin(User, User.id == RateRerun.requested_by)
            .where(RateRerun.organization_id == organization_id, RateRerun.id == run_id)
        )
        row = (await self.session.execute(stmt)).first()
        return (row[0], row[1]) if row else None

    async def list_runs(
        self, organization_id: uuid.UUID, params: PageParams
    ) -> Page[tuple[RateRerun, str | None]]:
        """Newest first by ``(created_at, id)``."""
        stmt = (
            select(RateRerun, User.full_name)
            .outerjoin(User, User.id == RateRerun.requested_by)
            .where(RateRerun.organization_id == organization_id)
        )
        stmt = keyset_query(
            stmt,
            sort_expr=RateRerun.created_at,
            id_column=RateRerun.id,
            params=params,
            descending=True,
            parse=lambda raw: datetime.fromisoformat(str(raw)),
        )
        rows: list[tuple[RateRerun, str | None]] = [
            (row[0], row[1]) for row in (await self.session.execute(stmt)).all()
        ]
        items, next_cursor = keyset_finish(rows, params, lambda r: (r[0].created_at, r[0].id))
        return Page(items=items, next_cursor=next_cursor)

    async def list_items(
        self,
        organization_id: uuid.UUID,
        run_id: uuid.UUID,
        *,
        outcome: str | None,
        params: PageParams,
    ) -> Page[ItemView]:
        """The run's entries by ``(entry_date, id)``."""
        stmt = (
            select(
                RateRerunItem,
                OrganizationMember.staff_code,
                User.full_name,
                Client.code,
                Client.name,
                Job.number,
                Job.name,
            )
            .outerjoin(User, User.id == RateRerunItem.user_id)
            .outerjoin(
                OrganizationMember,
                and_(
                    OrganizationMember.organization_id == RateRerunItem.organization_id,
                    OrganizationMember.user_id == RateRerunItem.user_id,
                ),
            )
            .outerjoin(
                Client,
                and_(
                    Client.organization_id == RateRerunItem.organization_id,
                    Client.id == RateRerunItem.client_id,
                ),
            )
            .outerjoin(
                Job,
                and_(
                    Job.organization_id == RateRerunItem.organization_id,
                    Job.id == RateRerunItem.job_id,
                ),
            )
            .where(RateRerunItem.organization_id == organization_id, RateRerunItem.run_id == run_id)
        )
        if outcome is not None:
            stmt = stmt.where(RateRerunItem.outcome == outcome)
        stmt = keyset_query(
            stmt,
            sort_expr=RateRerunItem.entry_date,
            id_column=RateRerunItem.id,
            params=params,
            descending=False,
            parse=parse_date,
        )
        rows = [ItemView(*row) for row in (await self.session.execute(stmt)).all()]
        items, next_cursor = keyset_finish(rows, params, lambda v: (v.item.entry_date, v.item.id))
        return Page(items=items, next_cursor=next_cursor)

    async def names(
        self,
        organization_id: uuid.UUID,
        user_ids: Collection[uuid.UUID],
        client_ids: Collection[uuid.UUID],
        job_ids: Collection[uuid.UUID],
    ) -> tuple[
        dict[uuid.UUID, tuple[str | None, str]],
        dict[uuid.UUID, tuple[str, str]],
        dict[uuid.UUID, tuple[str, str]],
    ]:
        """Names for a preview's items: staff ``(code, name)``, clients ``(code, name)`` and
        jobs ``(number, name)``, in three queries."""
        staff: dict[uuid.UUID, tuple[str | None, str]] = {}
        if user_ids:
            stmt = (
                select(User.id, OrganizationMember.staff_code, User.full_name)
                .join(
                    OrganizationMember,
                    and_(
                        OrganizationMember.user_id == User.id,
                        OrganizationMember.organization_id == organization_id,
                    ),
                )
                .where(User.id.in_(list(user_ids)))
            )
            staff = {row[0]: (row[1], row[2]) for row in (await self.session.execute(stmt)).all()}
        clients: dict[uuid.UUID, tuple[str, str]] = {}
        if client_ids:
            cstmt = select(Client.id, Client.code, Client.name).where(
                Client.organization_id == organization_id, Client.id.in_(list(client_ids))
            )
            clients = {
                row[0]: (row[1], row[2]) for row in (await self.session.execute(cstmt)).all()
            }
        jobs: dict[uuid.UUID, tuple[str, str]] = {}
        if job_ids:
            jstmt = select(Job.id, Job.number, Job.name).where(
                Job.organization_id == organization_id, Job.id.in_(list(job_ids))
            )
            jobs = {row[0]: (row[1], row[2]) for row in (await self.session.execute(jstmt)).all()}
        return staff, clients, jobs
