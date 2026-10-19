"""Reference lookups shared by the billing repositories, and keyset pagination on dates.

Billing reads clients, jobs, VAT rates, services and members that other modules own. The
queries here are read-only and, like every query, filtered by ``organization_id``.
"""

import uuid
from collections.abc import Iterable
from datetime import date
from typing import Any

from sqlalchemy import Select, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.core.exceptions import ValidationError
from app.db.models import (
    Client,
    ClientContact,
    Job,
    Organization,
    OrganizationMember,
    Service,
    User,
    VatRate,
)
from app.utils.pagination import Page, PageParams, decode_cursor, encode_cursor


class BillingReferenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_organization(self, organization_id: uuid.UUID) -> Organization | None:
        stmt = select(Organization).where(
            Organization.id == organization_id, Organization.deleted_at.is_(None)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_client(self, organization_id: uuid.UUID, client_id: uuid.UUID) -> Client | None:
        stmt = select(Client).where(
            Client.organization_id == organization_id,
            Client.id == client_id,
            Client.deleted_at.is_(None),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def clients_by_ids(
        self, organization_id: uuid.UUID, ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, Client]:
        wanted = set(ids)
        if not wanted:
            return {}
        stmt = select(Client).where(
            Client.organization_id == organization_id, Client.id.in_(wanted)
        )
        return {c.id: c for c in (await self.session.execute(stmt)).scalars()}

    async def get_job(self, organization_id: uuid.UUID, job_id: uuid.UUID) -> Job | None:
        stmt = select(Job).where(
            Job.organization_id == organization_id, Job.id == job_id, Job.deleted_at.is_(None)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def jobs_by_ids(
        self, organization_id: uuid.UUID, ids: Iterable[uuid.UUID | None]
    ) -> dict[uuid.UUID, Job]:
        wanted = {i for i in ids if i is not None}
        if not wanted:
            return {}
        stmt = select(Job).where(Job.organization_id == organization_id, Job.id.in_(wanted))
        return {j.id: j for j in (await self.session.execute(stmt)).scalars()}

    async def get_vat_rate(self, organization_id: uuid.UUID, rate_id: uuid.UUID) -> VatRate | None:
        stmt = select(VatRate).where(
            VatRate.organization_id == organization_id, VatRate.id == rate_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def default_vat_rate(self, organization_id: uuid.UUID) -> VatRate | None:
        stmt = select(VatRate).where(
            VatRate.organization_id == organization_id,
            VatRate.is_default.is_(True),
            VatRate.archived_at.is_(None),
        )
        return (await self.session.execute(stmt.limit(1))).scalar_one_or_none()

    async def get_service(
        self, organization_id: uuid.UUID, service_id: uuid.UUID
    ) -> Service | None:
        stmt = select(Service).where(
            Service.organization_id == organization_id, Service.id == service_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def member_names(
        self, organization_id: uuid.UUID, user_ids: Iterable[uuid.UUID | None]
    ) -> dict[uuid.UUID, str]:
        wanted = {i for i in user_ids if i is not None}
        if not wanted:
            return {}
        stmt = (
            select(OrganizationMember.user_id, User.full_name)
            .join(User, User.id == OrganizationMember.user_id)
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id.in_(wanted),
            )
        )
        return {row[0]: row[1] for row in (await self.session.execute(stmt)).all()}

    async def client_contacts(
        self, organization_id: uuid.UUID, client_id: uuid.UUID
    ) -> list[ClientContact]:
        stmt = (
            select(ClientContact)
            .where(
                ClientContact.organization_id == organization_id,
                ClientContact.client_id == client_id,
            )
            .order_by(ClientContact.is_primary.desc(), ClientContact.name, ClientContact.id)
        )
        return list((await self.session.execute(stmt)).scalars())


async def paginate_by_date[T](
    session: AsyncSession,
    stmt: Select[T],
    *,
    sort_column: InstrumentedAttribute[date],
    id_column: InstrumentedAttribute[uuid.UUID],
    params: PageParams,
    descending: bool = True,
) -> Page[T]:
    """Like ``app.utils.pagination.paginate`` for a non-null ``date`` sort column."""
    if params.cursor:
        raw, row_id = decode_cursor(params.cursor)
        try:
            value = date.fromisoformat(str(raw))
        except ValueError as exc:
            raise ValidationError.for_field("cursor", "Invalid cursor.") from exc
        key = tuple_(sort_column, id_column)
        stmt = stmt.where(key < (value, row_id) if descending else key > (value, row_id))
    order: tuple[Any, ...] = (
        (sort_column.desc(), id_column.desc()) if descending else (sort_column, id_column)
    )
    rows = list((await session.execute(stmt.order_by(*order).limit(params.limit + 1))).scalars())
    next_cursor = None
    if len(rows) > params.limit:
        rows = rows[: params.limit]
        last = rows[-1]
        next_cursor = encode_cursor(
            getattr(last, sort_column.key).isoformat(), getattr(last, id_column.key)
        )
    return Page(items=rows, next_cursor=next_cursor)
