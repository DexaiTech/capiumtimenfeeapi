"""Disbursement queries. Every method is scoped to one organization."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Disbursement
from app.repositories.billing_common import paginate_by_date
from app.utils.pagination import Page, PageParams


@dataclass(frozen=True, slots=True)
class DisbursementFilters:
    client_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    billing_status: Sequence[str] | None = None
    created_by: uuid.UUID | None = None
    date_from: date | None = None
    date_to: date | None = None
    is_billable: bool | None = None


class DisbursementRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, disbursement: Disbursement) -> Disbursement:
        self.session.add(disbursement)
        await self.session.flush()
        await self.session.refresh(disbursement)
        return disbursement

    async def get(
        self, organization_id: uuid.UUID, disbursement_id: uuid.UUID, *, for_update: bool = False
    ) -> Disbursement | None:
        stmt = (
            select(Disbursement)
            .where(
                Disbursement.organization_id == organization_id,
                Disbursement.id == disbursement_id,
            )
            .execution_options(populate_existing=True)
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_page(
        self, organization_id: uuid.UUID, filters: DisbursementFilters, params: PageParams
    ) -> Page[Disbursement]:
        """Newest first by ``(incurred_on, id)``."""
        stmt = select(Disbursement).where(Disbursement.organization_id == organization_id)
        if filters.client_id is not None:
            stmt = stmt.where(Disbursement.client_id == filters.client_id)
        if filters.job_id is not None:
            stmt = stmt.where(Disbursement.job_id == filters.job_id)
        if filters.billing_status:
            stmt = stmt.where(Disbursement.billing_status.in_(filters.billing_status))
        if filters.created_by is not None:
            stmt = stmt.where(Disbursement.created_by == filters.created_by)
        if filters.date_from is not None:
            stmt = stmt.where(Disbursement.incurred_on >= filters.date_from)
        if filters.date_to is not None:
            stmt = stmt.where(Disbursement.incurred_on <= filters.date_to)
        if filters.is_billable is not None:
            stmt = stmt.where(Disbursement.is_billable.is_(filters.is_billable))
        return await paginate_by_date(
            self.session,
            stmt,
            sort_column=Disbursement.incurred_on,
            id_column=Disbursement.id,
            params=params,
        )

    async def delete(self, disbursement: Disbursement) -> None:
        await self.session.delete(disbursement)
        await self.session.flush()
