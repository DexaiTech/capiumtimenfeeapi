"""What is waiting for approval in each area of a practice, as ids. Used when a practice switches
an approval flow off (``app/services/approval_switches.py``)."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ExpenseClaim, Invoice, Timesheet, WipAdjustment


class WaitingApprovalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def timesheets(self, organization_id: uuid.UUID) -> list[uuid.UUID]:
        stmt = (
            select(Timesheet.id)
            .where(Timesheet.organization_id == organization_id, Timesheet.status == "submitted")
            .order_by(Timesheet.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def expense_claims(self, organization_id: uuid.UUID) -> list[uuid.UUID]:
        stmt = (
            select(ExpenseClaim.id)
            .where(
                ExpenseClaim.organization_id == organization_id,
                ExpenseClaim.status == "submitted",
            )
            .order_by(ExpenseClaim.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def wip_adjustments(self, organization_id: uuid.UUID) -> list[uuid.UUID]:
        """Pending adjustments, oldest request first: a later write-off is decided on the WIP
        that the earlier ones leave."""
        stmt = (
            select(WipAdjustment.id)
            .where(
                WipAdjustment.organization_id == organization_id,
                WipAdjustment.status == "pending",
            )
            .order_by(WipAdjustment.created_at, WipAdjustment.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def invoices(self, organization_id: uuid.UUID) -> list[uuid.UUID]:
        stmt = (
            select(Invoice.id)
            .where(
                Invoice.organization_id == organization_id,
                Invoice.status == "awaiting_approval",
            )
            .order_by(Invoice.id)
        )
        return list((await self.session.execute(stmt)).scalars())
