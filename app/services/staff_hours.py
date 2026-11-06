"""A staff member's dated working hours: the standard minutes of a week or a month, beyond which
time is overtime (ticket APT81, phase A; docs/modules/team-rates.md, "Working hours and overtime
rates").

Rules:

* A person's periods never overlap: 409 ``HOURS_OVERLAP`` (an exclusion constraint makes it safe
  under concurrent requests).
* New hours are open-ended from ``effective_from``; the person's open-ended hours that started
  earlier end the day before. Any other overlap is refused.
* Every add, end and delete is in the audit trail (``staff_hours.*``, on the staff member).
* Nothing prices time from these hours yet.
"""

import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import ConflictError, NotFoundError
from app.db.models import StaffHoursBasis
from app.db.session import unit_of_work
from app.repositories.members import MemberRepository
from app.repositories.overtime import StaffHoursRepository
from app.schemas.overtime import StaffHoursCreate, StaffHoursRead
from app.services.common import record_audit
from app.services.policy import OrgContext
from app.services.rate_rules import is_exclusion_violation
from app.utils.practice_dates import today_in


def _iso(day: date | None) -> str | None:
    return day.isoformat() if day else None


def overlap_error(conflicts: list[StaffHoursBasis] | None = None) -> ConflictError:
    return ConflictError(
        "This staff member already has working hours for part of this period.",
        code="HOURS_OVERLAP",
        details={
            "conflicts": [
                {
                    "id": str(r.id),
                    "effective_from": r.effective_from.isoformat(),
                    "effective_to": _iso(r.effective_to),
                }
                for r in conflicts or []
            ]
        },
    )


def _meta(row: StaffHoursBasis) -> dict[str, Any]:
    return {
        "staff_hours_id": str(row.id),
        "basis": row.basis,
        "standard_minutes": row.standard_minutes,
        "effective_from": row.effective_from.isoformat(),
        "effective_to": _iso(row.effective_to),
    }


class StaffHoursService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.hours = StaffHoursRepository(session)
        self.members = MemberRepository(session)

    async def list_hours(self, ctx: OrgContext, user_id: uuid.UUID) -> list[StaffHoursRead]:
        """The person's hours, the current period first, then newest first."""
        await self._ensure_member(ctx, user_id)
        today = today_in(ctx.organization.timezone)
        rows = [
            StaffHoursRead.build(row, today=today)
            for row in await self.hours.list_for(ctx.organization_id, user_id)
        ]
        return sorted(rows, key=lambda row: row.status != "current")

    async def add(
        self, ctx: OrgContext, user_id: uuid.UUID, data: StaffHoursCreate, rctx: RequestContext
    ) -> StaffHoursRead:
        org_id = ctx.organization_id
        start = data.effective_from
        try:
            async with unit_of_work(self.session):
                await self._ensure_member(ctx, user_id)
                await self.hours.lock_person(org_id, user_id)
                overlaps = await self.hours.overlapping(org_id, user_id, start, None)
                if overlaps:
                    running = overlaps[0]
                    if (
                        len(overlaps) > 1
                        or running.effective_to is not None
                        or running.effective_from >= start
                    ):
                        raise overlap_error(overlaps)
                    running.effective_to = start - timedelta(days=1)
                    await self.hours.save(running)
                    await self._audit(
                        ctx,
                        rctx,
                        "staff_hours.ended",
                        running,
                        {"changes": {"effective_to": [None, running.effective_to.isoformat()]}},
                    )
                row = await self.hours.add(
                    StaffHoursBasis(
                        organization_id=org_id,
                        user_id=user_id,
                        basis=data.basis,
                        standard_minutes=data.standard_minutes,
                        effective_from=start,
                        created_by=ctx.user.id,
                    )
                )
                await self._audit(ctx, rctx, "staff_hours.added", row)
        except IntegrityError as exc:
            if is_exclusion_violation(exc):
                raise overlap_error() from exc
            raise
        return StaffHoursRead.build(row, today=today_in(ctx.organization.timezone))

    async def delete(
        self, ctx: OrgContext, user_id: uuid.UUID, hours_id: uuid.UUID, rctx: RequestContext
    ) -> None:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            await self._ensure_member(ctx, user_id)
            await self.hours.lock_person(org_id, user_id)
            row = await self.hours.get(org_id, user_id, hours_id)
            if row is None:
                raise NotFoundError("Working hours not found.")
            await self.hours.delete(row)
            await self._audit(ctx, rctx, "staff_hours.deleted", row)

    async def _ensure_member(self, ctx: OrgContext, user_id: uuid.UUID) -> None:
        if await self.members.get(ctx.organization_id, user_id) is None:
            raise NotFoundError("Staff member not found.")

    async def _audit(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        action: str,
        row: StaffHoursBasis,
        extra: dict[str, Any] | None = None,
    ) -> None:
        await record_audit(
            self.session, ctx, rctx, action, "member", row.user_id, {**_meta(row), **(extra or {})}
        )
