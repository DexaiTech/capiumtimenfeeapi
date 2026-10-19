"""Invoice template queries. Every method is scoped to one organization."""

import uuid
from collections.abc import Collection

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client, InvoiceTemplate


class InvoiceTemplateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(
        self, organization_id: uuid.UUID, *, include_inactive: bool
    ) -> list[InvoiceTemplate]:
        """Live templates: the default first, then by name."""
        stmt = select(InvoiceTemplate).where(
            InvoiceTemplate.organization_id == organization_id,
            InvoiceTemplate.deleted_at.is_(None),
        )
        if not include_inactive:
            stmt = stmt.where(InvoiceTemplate.status == "active")
        stmt = stmt.order_by(
            InvoiceTemplate.is_default.desc(), func.lower(InvoiceTemplate.name), InvoiceTemplate.id
        )
        return list((await self.session.execute(stmt)).scalars())

    async def get(
        self, organization_id: uuid.UUID, template_id: uuid.UUID, *, lock: bool = False
    ) -> InvoiceTemplate | None:
        stmt = select(InvoiceTemplate).where(
            InvoiceTemplate.organization_id == organization_id,
            InvoiceTemplate.id == template_id,
            InvoiceTemplate.deleted_at.is_(None),
        )
        if lock:
            stmt = stmt.with_for_update()
        stmt = stmt.execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_default(self, organization_id: uuid.UUID) -> InvoiceTemplate | None:
        stmt = select(InvoiceTemplate).where(
            InvoiceTemplate.organization_id == organization_id,
            InvoiceTemplate.is_default.is_(True),
            InvoiceTemplate.deleted_at.is_(None),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def lock_all(self, organization_id: uuid.UUID) -> None:
        """Lock every live template of the practice, in id order, to serialise default changes."""
        await self.session.execute(
            select(InvoiceTemplate.id)
            .where(
                InvoiceTemplate.organization_id == organization_id,
                InvoiceTemplate.deleted_at.is_(None),
            )
            .order_by(InvoiceTemplate.id)
            .with_for_update()
        )

    async def name_taken(
        self, organization_id: uuid.UUID, name: str, exclude_id: uuid.UUID | None = None
    ) -> bool:
        stmt = select(InvoiceTemplate.id).where(
            InvoiceTemplate.organization_id == organization_id,
            func.lower(InvoiceTemplate.name) == name.lower(),
            InvoiceTemplate.deleted_at.is_(None),
        )
        if exclude_id is not None:
            stmt = stmt.where(InvoiceTemplate.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def clear_default(self, organization_id: uuid.UUID, keep_id: uuid.UUID | None) -> None:
        """Unset the default flag on every template but ``keep_id`` (a fresh statement, so it
        also sees a default committed by a transaction this one waited for)."""
        stmt = update(InvoiceTemplate).where(
            InvoiceTemplate.organization_id == organization_id,
            InvoiceTemplate.is_default.is_(True),
            InvoiceTemplate.deleted_at.is_(None),
        )
        if keep_id is not None:
            stmt = stmt.where(InvoiceTemplate.id != keep_id)
        await self.session.execute(
            stmt.values(is_default=False).execution_options(synchronize_session=False)
        )

    async def client_counts(
        self, organization_id: uuid.UUID, template_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """Live clients that use each template."""
        if not template_ids:
            return {}
        stmt = (
            select(Client.invoice_template_id, func.count())
            .where(
                Client.organization_id == organization_id,
                Client.deleted_at.is_(None),
                Client.invoice_template_id.in_(list(template_ids)),
            )
            .group_by(Client.invoice_template_id)
        )
        return {
            row[0]: int(row[1])
            for row in (await self.session.execute(stmt)).all()
            if row[0] is not None
        }

    async def add(self, template: InvoiceTemplate) -> InvoiceTemplate:
        self.session.add(template)
        await self.session.flush()
        await self.session.refresh(template)
        return template

    async def save(self, template: InvoiceTemplate) -> InvoiceTemplate:
        await self.session.flush()
        await self.session.refresh(template)
        return template
