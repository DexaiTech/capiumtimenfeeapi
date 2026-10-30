"""Lead queries. Every method is scoped to one organization."""

import uuid
from collections.abc import Collection

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client, Lead
from app.schemas.leads import LeadStatus, LeadTotals
from app.utils.pagination import Page, PageParams, paginate

STATUSES: tuple[LeadStatus, ...] = ("open", "converted", "archived")


def _has_status(status: LeadStatus) -> ColumnElement[bool]:
    """The derived status in SQL; the same rule as ``app.schemas.leads.status_of``."""
    if status == "archived":
        return Lead.archived_at.is_not(None)
    converted = (
        Lead.converted_client_id.is_not(None)
        if status == "converted"
        else Lead.converted_client_id.is_(None)
    )
    return and_(Lead.archived_at.is_(None), converted)


class LeadRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(
        self, organization_id: uuid.UUID, lead_id: uuid.UUID, *, lock: bool = False
    ) -> Lead | None:
        stmt = select(Lead).where(Lead.organization_id == organization_id, Lead.id == lead_id)
        if lock:
            stmt = stmt.with_for_update()
        return (
            await self.session.execute(stmt.execution_options(populate_existing=True))
        ).scalar_one_or_none()

    async def list_page(
        self,
        organization_id: uuid.UUID,
        params: PageParams,
        *,
        status: LeadStatus = "open",
        q: str | None = None,
    ) -> Page[Lead]:
        """Newest first."""
        stmt = select(Lead).where(Lead.organization_id == organization_id, _has_status(status))
        if q:
            stmt = stmt.where(
                or_(
                    Lead.name.icontains(q, autoescape=True),
                    Lead.contact_name.icontains(q, autoescape=True),
                    Lead.email.icontains(q, autoescape=True),
                )
            )
        return await paginate(
            self.session,
            stmt,
            sort_column=Lead.created_at,
            id_column=Lead.id,
            params=params,
            descending=True,
        )

    async def totals(self, organization_id: uuid.UUID) -> dict[LeadStatus, LeadTotals]:
        """Count and costs per status; every status is present."""
        archived = Lead.archived_at.is_not(None)
        converted = Lead.converted_client_id.is_not(None)
        stmt = (
            select(
                archived,
                converted,
                func.count(),
                func.coalesce(func.sum(Lead.acquisition_cost), 0),
                func.coalesce(func.sum(Lead.onboarding_cost), 0),
            )
            .where(Lead.organization_id == organization_id)
            .group_by(archived, converted)
        )
        sums: dict[LeadStatus, list[int]] = {s: [0, 0, 0] for s in STATUSES}
        for is_archived, is_converted, count, acquisition, onboarding in (
            await self.session.execute(stmt)
        ).all():
            status: LeadStatus = (
                "archived" if is_archived else "converted" if is_converted else "open"
            )
            sums[status][0] += int(count)
            sums[status][1] += int(acquisition)
            sums[status][2] += int(onboarding)
        return {
            s: LeadTotals(
                count=count,
                acquisition_cost=acquisition,
                onboarding_cost=onboarding,
                total_cost=acquisition + onboarding,
            )
            for s, (count, acquisition, onboarding) in sums.items()
        }

    async def live_clients(
        self, organization_id: uuid.UUID, client_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, Client]:
        """The clients (not deleted) that leads were converted into."""
        if not client_ids:
            return {}
        stmt = select(Client).where(
            Client.organization_id == organization_id,
            Client.id.in_(list(client_ids)),
            Client.deleted_at.is_(None),
        )
        return {c.id: c for c in (await self.session.execute(stmt)).scalars()}

    async def add(self, lead: Lead) -> Lead:
        self.session.add(lead)
        await self.session.flush()
        await self.session.refresh(lead)
        return lead

    async def save(self, lead: Lead) -> Lead:
        await self.session.flush()
        await self.session.refresh(lead)
        return lead
