"""Branch queries. Every method is scoped to one organization."""

import uuid
from collections.abc import Collection

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Branch, Client


class BranchRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(
        self, organization_id: uuid.UUID, branch_id: uuid.UUID, *, lock: bool = False
    ) -> Branch | None:
        stmt = select(Branch).where(
            Branch.organization_id == organization_id, Branch.id == branch_id
        )
        if lock:
            stmt = stmt.with_for_update()
        return (
            await self.session.execute(stmt.execution_options(populate_existing=True))
        ).scalar_one_or_none()

    async def list_all(
        self, organization_id: uuid.UUID, *, is_active: bool | None = None, q: str | None = None
    ) -> list[Branch]:
        stmt = select(Branch).where(Branch.organization_id == organization_id)
        if is_active is not None:
            stmt = stmt.where(Branch.is_active.is_(is_active))
        if q:
            stmt = stmt.where(Branch.name.icontains(q, autoescape=True))
        stmt = stmt.order_by(func.lower(Branch.name), Branch.id)
        return list(
            (await self.session.execute(stmt.execution_options(populate_existing=True))).scalars()
        )

    async def names(
        self, organization_id: uuid.UUID, branch_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        if not branch_ids:
            return {}
        stmt = select(Branch.id, Branch.name).where(
            Branch.organization_id == organization_id, Branch.id.in_(list(branch_ids))
        )
        return {row[0]: row[1] for row in (await self.session.execute(stmt)).all()}

    async def by_names(
        self, organization_id: uuid.UUID, names: Collection[str]
    ) -> dict[str, Branch]:
        """The branches named (ignoring case and surrounding blanks), keyed by lower-cased name."""
        keys = {n.strip().lower() for n in names}
        if not keys:
            return {}
        stmt = select(Branch).where(
            Branch.organization_id == organization_id, func.lower(Branch.name).in_(keys)
        )
        return {b.name.lower(): b for b in (await self.session.execute(stmt)).scalars()}

    async def name_taken(
        self, organization_id: uuid.UUID, name: str, exclude_id: uuid.UUID | None = None
    ) -> bool:
        stmt = select(Branch.id).where(
            Branch.organization_id == organization_id,
            func.lower(Branch.name) == name.strip().lower(),
        )
        if exclude_id is not None:
            stmt = stmt.where(Branch.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def client_counts(
        self, organization_id: uuid.UUID, branch_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """Live clients per branch."""
        if not branch_ids:
            return {}
        stmt = (
            select(Client.branch_id, func.count())
            .where(
                Client.organization_id == organization_id,
                Client.deleted_at.is_(None),
                Client.branch_id.in_(list(branch_ids)),
            )
            .group_by(Client.branch_id)
        )
        rows = (await self.session.execute(stmt)).all()
        return {row[0]: int(row[1]) for row in rows if row[0] is not None}

    async def any_client(self, organization_id: uuid.UUID, branch_id: uuid.UUID) -> bool:
        """Whether any client, deleted ones included, has the branch (the foreign key would
        refuse to delete it)."""
        stmt = select(Client.id).where(
            Client.organization_id == organization_id, Client.branch_id == branch_id
        )
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def add(self, branch: Branch) -> Branch:
        self.session.add(branch)
        await self.session.flush()
        await self.session.refresh(branch)
        return branch

    async def save(self, branch: Branch) -> Branch:
        await self.session.flush()
        await self.session.refresh(branch)
        return branch

    async def delete(self, organization_id: uuid.UUID, branch_id: uuid.UUID) -> None:
        await self.session.execute(
            delete(Branch).where(Branch.organization_id == organization_id, Branch.id == branch_id)
        )
