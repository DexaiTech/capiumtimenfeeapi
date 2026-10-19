"""Expense type queries. Every method is scoped to one organization."""

import uuid
from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Expense, ExpenseType
from app.repositories.expense_paging import SortExpr, keyset_page
from app.utils.pagination import Page, PageParams


@dataclass(frozen=True, slots=True)
class TypeFilters:
    q: str | None = None
    # None = every type; True = active only; False = inactive only.
    is_active: bool | None = None
    is_mileage: bool | None = None


class ExpenseTypeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(
        self, organization_id: uuid.UUID, type_id: uuid.UUID, *, lock: bool = False
    ) -> ExpenseType | None:
        stmt = select(ExpenseType).where(
            ExpenseType.organization_id == organization_id, ExpenseType.id == type_id
        )
        if lock:
            stmt = stmt.with_for_update()
        return (
            await self.session.execute(stmt.execution_options(populate_existing=True))
        ).scalar_one_or_none()

    async def list_page(
        self,
        organization_id: uuid.UUID,
        filters: TypeFilters,
        *,
        sort: str,
        descending: bool,
        params: PageParams,
    ) -> Page[ExpenseType]:
        stmt = select(ExpenseType).where(ExpenseType.organization_id == organization_id)
        if filters.is_active is not None:
            stmt = stmt.where(ExpenseType.is_active.is_(filters.is_active))
        if filters.is_mileage is not None:
            stmt = stmt.where(ExpenseType.is_mileage.is_(filters.is_mileage))
        if filters.q:
            stmt = stmt.where(
                or_(
                    ExpenseType.name.icontains(filters.q, autoescape=True),
                    ExpenseType.code.icontains(filters.q, autoescape=True),
                )
            )
        key: SortExpr = ExpenseType.code if sort == "code" else func.lower(ExpenseType.name)
        rows, next_cursor = await keyset_page(
            self.session,
            stmt.execution_options(populate_existing=True),
            key=key,
            id_column=ExpenseType.id,
            sort=sort,
            kind="str",
            descending=descending,
            params=params,
        )
        return Page(items=[row[0] for row in rows], next_cursor=next_cursor)

    async def used_ids(
        self, organization_id: uuid.UUID, type_ids: Collection[uuid.UUID]
    ) -> set[uuid.UUID]:
        """Which of these types an expense line uses."""
        if not type_ids:
            return set()
        stmt = (
            select(Expense.expense_type_id)
            .where(
                Expense.organization_id == organization_id,
                Expense.expense_type_id.in_(list(type_ids)),
            )
            .distinct()
        )
        return set((await self.session.execute(stmt)).scalars())

    async def is_used(self, organization_id: uuid.UUID, type_id: uuid.UUID) -> bool:
        stmt = select(
            exists().where(
                Expense.organization_id == organization_id, Expense.expense_type_id == type_id
            )
        )
        return bool(await self.session.scalar(stmt))

    async def name_taken(
        self, organization_id: uuid.UUID, name: str, *, exclude_id: uuid.UUID | None = None
    ) -> bool:
        """Another *active* type has this name (ignoring case)."""
        stmt = select(ExpenseType.id).where(
            ExpenseType.organization_id == organization_id,
            ExpenseType.is_active.is_(True),
            func.lower(ExpenseType.name) == name.lower(),
        )
        if exclude_id is not None:
            stmt = stmt.where(ExpenseType.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def code_taken(
        self, organization_id: uuid.UUID, code: str, *, exclude_id: uuid.UUID | None = None
    ) -> bool:
        stmt = select(ExpenseType.id).where(
            ExpenseType.organization_id == organization_id, ExpenseType.code == code.upper()
        )
        if exclude_id is not None:
            stmt = stmt.where(ExpenseType.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def by_ids(
        self, organization_id: uuid.UUID, type_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, ExpenseType]:
        if not type_ids:
            return {}
        stmt = select(ExpenseType).where(
            ExpenseType.organization_id == organization_id, ExpenseType.id.in_(list(type_ids))
        )
        return {t.id: t for t in (await self.session.execute(stmt)).scalars()}

    async def add(self, row: ExpenseType) -> ExpenseType:
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        return row

    async def save(self, row: ExpenseType) -> ExpenseType:
        await self.session.flush()
        await self.session.refresh(row)
        return row

    async def delete(self, organization_id: uuid.UUID, type_id: uuid.UUID) -> None:
        await self.session.execute(
            delete(ExpenseType).where(
                ExpenseType.organization_id == organization_id, ExpenseType.id == type_id
            )
        )
