"""Grade queries. Every method is scoped to one organization."""

import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Grade, OrganizationMember, RateRule
from app.db.models.organization import GONE_STATUSES
from app.repositories.time_paging import keyset_finish, keyset_query
from app.utils.pagination import Page, PageParams

# sort key -> (sort expression, cursor value parser, key of a loaded grade)
_SORTS: dict[str, tuple[Any, Callable[[Any], Any], Callable[[Grade], Any]]] = {
    "sort_order": (Grade.sort_order, int, lambda g: g.sort_order),
    "name": (func.lower(Grade.name), str, lambda g: g.name.lower()),
    "code": (Grade.code, str, lambda g: g.code),
}
GRADE_SORTS = tuple(_SORTS)


@dataclass(frozen=True, slots=True)
class GradeFilters:
    q: str | None = None
    # None = every grade; True = active only; False = inactive only.
    is_active: bool | None = None


class GradeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(
        self, organization_id: uuid.UUID, grade_id: uuid.UUID, *, lock: bool = False
    ) -> Grade | None:
        stmt = select(Grade).where(Grade.organization_id == organization_id, Grade.id == grade_id)
        if lock:
            stmt = stmt.with_for_update()
        stmt = stmt.execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_page(
        self,
        organization_id: uuid.UUID,
        filters: GradeFilters,
        *,
        sort: str,
        descending: bool,
        params: PageParams,
    ) -> Page[Grade]:
        sort_expr, parse, key = _SORTS.get(sort, _SORTS["sort_order"])
        stmt = select(Grade).where(Grade.organization_id == organization_id)
        if filters.is_active is not None:
            stmt = stmt.where(Grade.is_active.is_(filters.is_active))
        if filters.q:
            stmt = stmt.where(
                or_(
                    Grade.name.icontains(filters.q, autoescape=True),
                    Grade.code.icontains(filters.q, autoescape=True),
                    Grade.description.icontains(filters.q, autoescape=True),
                )
            )
        stmt = keyset_query(
            stmt,
            sort_expr=sort_expr,
            id_column=Grade.id,
            params=params,
            descending=descending,
            parse=parse,
        ).execution_options(populate_existing=True)
        rows = list((await self.session.execute(stmt)).scalars())
        items, next_cursor = keyset_finish(rows, params, lambda g: (key(g), g.id))
        return Page(items=items, next_cursor=next_cursor)

    async def staff_counts(
        self, organization_id: uuid.UUID, grade_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """Staff (not removed or left) holding each grade."""
        if not grade_ids:
            return {}
        stmt = (
            select(OrganizationMember.grade_id, func.count())
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.grade_id.in_(list(grade_ids)),
                OrganizationMember.status.not_in(GONE_STATUSES),
            )
            .group_by(OrganizationMember.grade_id)
        )
        return {
            row[0]: int(row[1])
            for row in (await self.session.execute(stmt)).all()
            if row[0] is not None
        }

    async def is_used(self, organization_id: uuid.UUID, grade_id: uuid.UUID) -> bool:
        """Held by any staff member (whatever their status) or named by a rate rule."""
        checks = [
            exists().where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.grade_id == grade_id,
            ),
            exists().where(
                RateRule.organization_id == organization_id, RateRule.grade_id == grade_id
            ),
        ]
        return bool(await self.session.scalar(select(or_(*checks))))

    async def used_ids(
        self, organization_id: uuid.UUID, grade_ids: Collection[uuid.UUID]
    ) -> set[uuid.UUID]:
        """Which of these grades are held by staff (whatever their status) or named by a rule."""
        if not grade_ids:
            return set()
        ids = list(grade_ids)
        by_staff = select(OrganizationMember.grade_id).where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.grade_id.in_(ids),
        )
        by_rule = select(RateRule.grade_id).where(
            RateRule.organization_id == organization_id, RateRule.grade_id.in_(ids)
        )
        rows = (await self.session.execute(by_staff.union(by_rule))).scalars()
        return {grade_id for grade_id in rows if grade_id is not None}

    async def name_taken(
        self, organization_id: uuid.UUID, name: str, exclude_id: uuid.UUID | None = None
    ) -> bool:
        stmt = select(Grade.id).where(
            Grade.organization_id == organization_id, func.lower(Grade.name) == name.lower()
        )
        if exclude_id is not None:
            stmt = stmt.where(Grade.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def code_taken(
        self, organization_id: uuid.UUID, code: str, exclude_id: uuid.UUID | None = None
    ) -> bool:
        stmt = select(Grade.id).where(
            Grade.organization_id == organization_id, func.upper(Grade.code) == code.upper()
        )
        if exclude_id is not None:
            stmt = stmt.where(Grade.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def add(self, grade: Grade) -> Grade:
        self.session.add(grade)
        await self.session.flush()
        await self.session.refresh(grade)
        return grade

    async def save(self, grade: Grade) -> Grade:
        await self.session.flush()
        await self.session.refresh(grade)
        return grade

    async def delete(self, organization_id: uuid.UUID, grade_id: uuid.UUID) -> None:
        await self.session.execute(
            delete(Grade).where(Grade.organization_id == organization_id, Grade.id == grade_id)
        )
