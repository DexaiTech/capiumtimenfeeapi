"""Credit note queries. Every method is scoped to one organization."""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client, CreditNote, CreditNoteLine, Invoice
from app.repositories.billing_common import paginate_by_date
from app.utils.pagination import Page, PageParams


@dataclass(frozen=True, slots=True)
class CreditNoteFilters:
    client_id: uuid.UUID | None = None
    invoice_id: uuid.UUID | None = None
    date_from: date | None = None
    date_to: date | None = None
    q: str | None = None


class CreditNoteRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, credit_note: CreditNote, lines: Sequence[CreditNoteLine]) -> CreditNote:
        self.session.add(credit_note)
        await self.session.flush()
        self.session.add_all(lines)
        await self.session.flush()
        await self.session.refresh(credit_note)
        return credit_note

    async def get(self, organization_id: uuid.UUID, credit_note_id: uuid.UUID) -> CreditNote | None:
        stmt = select(CreditNote).where(
            CreditNote.organization_id == organization_id, CreditNote.id == credit_note_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def lines(
        self, organization_id: uuid.UUID, credit_note_id: uuid.UUID
    ) -> list[CreditNoteLine]:
        stmt = (
            select(CreditNoteLine)
            .where(
                CreditNoteLine.organization_id == organization_id,
                CreditNoteLine.credit_note_id == credit_note_id,
            )
            .order_by(CreditNoteLine.position, CreditNoteLine.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def invoice_numbers(
        self, organization_id: uuid.UUID, invoice_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, str | None]:
        """The numbers of the given invoices, in one query (for lists)."""
        wanted = set(invoice_ids)
        if not wanted:
            return {}
        stmt = select(Invoice.id, Invoice.number).where(
            Invoice.organization_id == organization_id, Invoice.id.in_(wanted)
        )
        return {row[0]: row[1] for row in (await self.session.execute(stmt)).all()}

    async def list_page(
        self, organization_id: uuid.UUID, filters: CreditNoteFilters, params: PageParams
    ) -> Page[CreditNote]:
        stmt = select(CreditNote).where(CreditNote.organization_id == organization_id)
        if filters.client_id is not None:
            stmt = stmt.where(CreditNote.client_id == filters.client_id)
        if filters.invoice_id is not None:
            stmt = stmt.where(CreditNote.invoice_id == filters.invoice_id)
        if filters.date_from is not None:
            stmt = stmt.where(CreditNote.issue_date >= filters.date_from)
        if filters.date_to is not None:
            stmt = stmt.where(CreditNote.issue_date <= filters.date_to)
        if filters.q:
            client_match = (
                select(Client.id)
                .where(
                    Client.organization_id == CreditNote.organization_id,
                    Client.id == CreditNote.client_id,
                    Client.name.icontains(filters.q, autoescape=True),
                )
                .exists()
            )
            stmt = stmt.where(
                or_(CreditNote.number.icontains(filters.q, autoescape=True), client_match)
            )
        return await paginate_by_date(
            self.session,
            stmt,
            sort_column=CreditNote.issue_date,
            id_column=CreditNote.id,
            params=params,
        )
