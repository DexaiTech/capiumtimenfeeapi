"""Queries of the WIP ledger, adjustments, transfers and the WIP of credit notes. Every method is
scoped to one organization."""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    CreditNote,
    CreditNoteWip,
    Invoice,
    InvoiceLine,
    WipAdjustment,
    WipAllocation,
    WipMovement,
    WipTransfer,
)
from app.utils.pagination import Page, PageParams, paginate


@dataclass(frozen=True, slots=True)
class MovementFilters:
    client_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    kinds: Sequence[str] = ()
    scope: str | None = None
    source_type: str | None = None
    date_from: date | None = None
    date_to: date | None = None


@dataclass(frozen=True, slots=True)
class AdjustmentFilters:
    statuses: Sequence[str] = ()
    kind: str | None = None
    client_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    date_from: date | None = None
    date_to: date | None = None


@dataclass(frozen=True, slots=True)
class TransferFilters:
    status: str | None = None
    client_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    date_from: date | None = None
    date_to: date | None = None


class WipLedgerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- Movements (the immutable ledger) ----------------------------------------------

    async def add_movement(self, movement: WipMovement) -> WipMovement:
        self.session.add(movement)
        await self.session.flush()
        await self.session.refresh(movement)
        return movement

    async def add_movements(self, movements: Sequence[WipMovement]) -> None:
        self.session.add_all(movements)
        await self.session.flush()
        for movement in movements:
            await self.session.refresh(movement)

    async def get_movement(
        self, organization_id: uuid.UUID, movement_id: uuid.UUID
    ) -> WipMovement | None:
        stmt = select(WipMovement).where(
            WipMovement.organization_id == organization_id, WipMovement.id == movement_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def movements_of_source(
        self, organization_id: uuid.UUID, source_type: str, source_id: uuid.UUID
    ) -> list[WipMovement]:
        stmt = (
            select(WipMovement)
            .where(
                WipMovement.organization_id == organization_id,
                WipMovement.source_type == source_type,
                WipMovement.source_id == source_id,
                WipMovement.kind != "reversal",
            )
            .order_by(WipMovement.created_at, WipMovement.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def reversed_ids(
        self, organization_id: uuid.UUID, movement_ids: Collection[uuid.UUID]
    ) -> set[uuid.UUID]:
        """The movements among ``movement_ids`` that a later reversal row cancels."""
        if not movement_ids:
            return set()
        stmt = select(WipMovement.reversal_of_id).where(
            WipMovement.organization_id == organization_id,
            WipMovement.reversal_of_id.in_(list(movement_ids)),
        )
        return {row[0] for row in (await self.session.execute(stmt)).all()}

    async def list_movements(
        self, organization_id: uuid.UUID, f: MovementFilters, params: PageParams
    ) -> Page[WipMovement]:
        """Newest first by ``(created_at, id)``."""
        stmt = select(WipMovement).where(WipMovement.organization_id == organization_id)
        if f.client_id is not None:
            stmt = stmt.where(WipMovement.client_id == f.client_id)
        if f.job_id is not None:
            stmt = stmt.where(WipMovement.job_id == f.job_id)
        if f.kinds:
            stmt = stmt.where(WipMovement.kind.in_(list(f.kinds)))
        if f.scope is not None:
            stmt = stmt.where(WipMovement.scope == f.scope)
        if f.source_type is not None:
            stmt = stmt.where(WipMovement.source_type == f.source_type)
        if f.date_from is not None:
            stmt = stmt.where(WipMovement.effective_date >= f.date_from)
        if f.date_to is not None:
            stmt = stmt.where(WipMovement.effective_date <= f.date_to)
        return await paginate(
            self.session,
            stmt,
            sort_column=WipMovement.created_at,
            id_column=WipMovement.id,
            params=params,
            descending=True,
        )

    # --- Adjustments -------------------------------------------------------------------

    async def add_adjustment(self, adjustment: WipAdjustment) -> WipAdjustment:
        self.session.add(adjustment)
        await self.session.flush()
        await self.session.refresh(adjustment)
        return adjustment

    async def get_adjustment(
        self, organization_id: uuid.UUID, adjustment_id: uuid.UUID, *, for_update: bool = False
    ) -> WipAdjustment | None:
        stmt = (
            select(WipAdjustment)
            .where(
                WipAdjustment.organization_id == organization_id,
                WipAdjustment.id == adjustment_id,
            )
            .execution_options(populate_existing=True)
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_adjustments(
        self, organization_id: uuid.UUID, f: AdjustmentFilters, params: PageParams
    ) -> Page[WipAdjustment]:
        """Newest first by ``(created_at, id)``."""
        stmt = select(WipAdjustment).where(WipAdjustment.organization_id == organization_id)
        if f.statuses:
            stmt = stmt.where(WipAdjustment.status.in_(list(f.statuses)))
        if f.kind is not None:
            stmt = stmt.where(WipAdjustment.kind == f.kind)
        if f.client_id is not None:
            stmt = stmt.where(WipAdjustment.client_id == f.client_id)
        if f.job_id is not None:
            stmt = stmt.where(WipAdjustment.job_id == f.job_id)
        if f.date_from is not None:
            stmt = stmt.where(WipAdjustment.effective_date >= f.date_from)
        if f.date_to is not None:
            stmt = stmt.where(WipAdjustment.effective_date <= f.date_to)
        return await paginate(
            self.session,
            stmt,
            sort_column=WipAdjustment.created_at,
            id_column=WipAdjustment.id,
            params=params,
            descending=True,
        )

    async def adjustment_counts(self, organization_id: uuid.UUID) -> tuple[dict[str, int], int]:
        """Adjustments per status and the total amount of the pending ones."""
        stmt = (
            select(
                WipAdjustment.status,
                func.count(),
                func.coalesce(func.sum(WipAdjustment.amount), 0),
            )
            .where(WipAdjustment.organization_id == organization_id)
            .group_by(WipAdjustment.status)
        )
        counts: dict[str, int] = {}
        pending_value = 0
        for status, count, amount in (await self.session.execute(stmt)).all():
            counts[status] = int(count)
            if status == "pending":
                pending_value = int(amount)
        return counts, pending_value

    # --- Transfers ---------------------------------------------------------------------

    async def add_transfer(self, transfer: WipTransfer) -> WipTransfer:
        self.session.add(transfer)
        await self.session.flush()
        await self.session.refresh(transfer)
        return transfer

    async def get_transfer(
        self, organization_id: uuid.UUID, transfer_id: uuid.UUID, *, for_update: bool = False
    ) -> WipTransfer | None:
        stmt = (
            select(WipTransfer)
            .where(WipTransfer.organization_id == organization_id, WipTransfer.id == transfer_id)
            .execution_options(populate_existing=True)
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_transfers(
        self, organization_id: uuid.UUID, f: TransferFilters, params: PageParams
    ) -> Page[WipTransfer]:
        """Newest first by ``(created_at, id)``."""
        stmt = select(WipTransfer).where(WipTransfer.organization_id == organization_id)
        if f.status is not None:
            stmt = stmt.where(WipTransfer.status == f.status)
        if f.client_id is not None:
            stmt = stmt.where(WipTransfer.client_id == f.client_id)
        if f.job_id is not None:
            stmt = stmt.where(
                (WipTransfer.from_job_id == f.job_id) | (WipTransfer.to_job_id == f.job_id)
            )
        if f.date_from is not None:
            stmt = stmt.where(WipTransfer.effective_date >= f.date_from)
        if f.date_to is not None:
            stmt = stmt.where(WipTransfer.effective_date <= f.date_to)
        return await paginate(
            self.session,
            stmt,
            sort_column=WipTransfer.created_at,
            id_column=WipTransfer.id,
            params=params,
            descending=True,
        )

    async def live_transferred_items(
        self, organization_id: uuid.UUID, job_id: uuid.UUID
    ) -> set[uuid.UUID]:
        """Ids of the items whose value was moved out of the job by a transfer that is not
        reversed."""
        stmt = select(WipTransfer.items).where(
            WipTransfer.organization_id == organization_id,
            WipTransfer.from_job_id == job_id,
            WipTransfer.status == "applied",
        )
        found: set[uuid.UUID] = set()
        for (items,) in (await self.session.execute(stmt)).all():
            found.update(uuid.UUID(str(item["id"])) for item in items)
        return found

    # --- Credit notes ------------------------------------------------------------------

    async def get_credit_note_wip(
        self, organization_id: uuid.UUID, credit_note_id: uuid.UUID
    ) -> CreditNoteWip | None:
        stmt = select(CreditNoteWip).where(
            CreditNoteWip.organization_id == organization_id,
            CreditNoteWip.credit_note_id == credit_note_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add_credit_note_wip(self, record: CreditNoteWip) -> CreditNoteWip:
        self.session.add(record)
        await self.session.flush()
        await self.session.refresh(record)
        return record

    async def get_credit_note(
        self, organization_id: uuid.UUID, credit_note_id: uuid.UUID
    ) -> CreditNote | None:
        stmt = select(CreditNote).where(
            CreditNote.organization_id == organization_id, CreditNote.id == credit_note_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def lock_invoice(
        self, organization_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> Invoice | None:
        """The invoice row, locked: first in the lock order every billing flow uses."""
        stmt = (
            select(Invoice)
            .where(Invoice.organization_id == organization_id, Invoice.id == invoice_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def credited_net(self, organization_id: uuid.UUID, invoice_id: uuid.UUID) -> int:
        """The net of all the credit notes raised against the invoice."""
        stmt = select(func.coalesce(func.sum(CreditNote.net_total), 0)).where(
            CreditNote.organization_id == organization_id, CreditNote.invoice_id == invoice_id
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def live_invoice_allocations(
        self, organization_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> list[WipAllocation]:
        """The invoice's allocations that are not released, in the order of its lines."""
        stmt = (
            select(WipAllocation)
            .outerjoin(
                InvoiceLine,
                and_(
                    InvoiceLine.organization_id == WipAllocation.organization_id,
                    InvoiceLine.id == WipAllocation.invoice_line_id,
                ),
            )
            .where(
                WipAllocation.organization_id == organization_id,
                WipAllocation.invoice_id == invoice_id,
                WipAllocation.reversed_at.is_(None),
            )
            .order_by(InvoiceLine.position, WipAllocation.id)
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars())
