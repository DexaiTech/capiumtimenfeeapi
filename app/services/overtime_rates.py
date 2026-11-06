"""Dated overtime rates of grades and staff members (ticket APT81, phase A;
docs/modules/team-rates.md, "Working hours and overtime rates").

Rules:

* A rate is a multiplier of the standard cost rate (``multiplier_bp``, 10000-100000) or a fixed
  amount in pence per hour (``rate``), for a grade or one staff member, from ``effective_from`` to
  ``effective_to`` inclusive (open-ended when null).
* The rates of one grade or one person never overlap: 409 ``RATE_OVERLAP`` (an exclusion
  constraint makes it safe under concurrent requests).
* Exactly one rate applies to a person on a day: their own staff rate, else their grade's rate,
  else none. The two never stack.
* A rate is ended with a last day, or deleted. Overtime already costed at a deleted rate keeps
  its rate and cost (``time_entry_overtime.overtime_rate_id`` becomes null).
* Overtime is costed at the rate in force on the entry's day as time is recorded
  (``app/services/overtime.py``); changing a rate does not re-cost recorded overtime.
* Every add, end and delete is in the audit trail (``overtime_rate.*``).
"""

import uuid
from dataclasses import replace
from datetime import date
from typing import Any, Literal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.db.models import OvertimeRate
from app.db.models.organization import GONE_STATUSES
from app.db.session import unit_of_work
from app.repositories.grades import GradeRepository
from app.repositories.members import MemberRepository
from app.repositories.overtime import OvertimeRateFilters, OvertimeRateRepository, OvertimeRateView
from app.schemas.overtime import (
    OvertimeRateCreate,
    OvertimeRateEnd,
    OvertimeRateRead,
    OvertimeResolveRead,
)
from app.services import policy
from app.services.common import record_audit
from app.services.policy import OrgContext
from app.services.rate_rules import is_exclusion_violation
from app.utils.practice_dates import today_in


def _iso(day: date | None) -> str | None:
    return day.isoformat() if day else None


def overlap_error(conflicts: list[OvertimeRate] | None = None) -> ConflictError:
    return ConflictError(
        "Another overtime rate of this grade or staff member already covers part of this period.",
        code="RATE_OVERLAP",
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


def _meta(view: OvertimeRateView) -> dict[str, Any]:
    rate = view.rate
    return {
        # Overtime rates are cost rates: the trail hides ``rate`` without ``rate.read``.
        "kind": "cost",
        "level": rate.level,
        "grade_id": str(rate.grade_id) if rate.grade_id else None,
        "grade_name": view.grade_name,
        "user_id": str(rate.user_id) if rate.user_id else None,
        "member_name": view.user_name,
        "mode": rate.mode,
        "multiplier_bp": rate.multiplier_bp,
        "rate": rate.rate,
        "effective_from": rate.effective_from.isoformat(),
        "effective_to": _iso(rate.effective_to),
    }


class OvertimeRateService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.rates = OvertimeRateRepository(session)
        self.members = MemberRepository(session)
        self.grades = GradeRepository(session)

    @staticmethod
    def can_see_costs(ctx: OrgContext) -> bool:
        """A fixed overtime amount is shown only with ``rate.read``."""
        return policy.has_permission(ctx, "rate.read")

    @staticmethod
    def today(ctx: OrgContext) -> date:
        return today_in(ctx.organization.timezone)

    async def list_rates(
        self, ctx: OrgContext, filters: OvertimeRateFilters
    ) -> list[OvertimeRateView]:
        return await self.rates.list_all(
            ctx.organization_id, replace(filters, today=self.today(ctx))
        )

    async def create(
        self, ctx: OrgContext, data: OvertimeRateCreate, rctx: RequestContext
    ) -> OvertimeRateView:
        org_id = ctx.organization_id
        try:
            async with unit_of_work(self.session):
                await self._check_references(org_id, data)
                await self.rates.lock_scope(org_id, data.user_id, data.grade_id)
                overlaps = await self.rates.overlapping(
                    org_id,
                    user_id=data.user_id,
                    grade_id=data.grade_id,
                    start=data.effective_from,
                    end=data.effective_to,
                )
                if overlaps:
                    raise overlap_error(overlaps)
                rate = await self.rates.add(
                    OvertimeRate(
                        organization_id=org_id,
                        level=data.level,
                        grade_id=data.grade_id,
                        user_id=data.user_id,
                        mode=data.mode,
                        multiplier_bp=data.multiplier_bp,
                        rate=data.rate,
                        effective_from=data.effective_from,
                        effective_to=data.effective_to,
                        created_by=ctx.user.id,
                    )
                )
                view = await self._view(org_id, rate.id)
                await self._audit(ctx, rctx, "overtime_rate.added", view, {})
        except IntegrityError as exc:
            if is_exclusion_violation(exc):
                raise overlap_error() from exc
            raise
        return view

    async def end(
        self, ctx: OrgContext, rate_id: uuid.UUID, data: OvertimeRateEnd, rctx: RequestContext
    ) -> OvertimeRateView:
        """Give the rate a last day (409 ``RATE_OVERLAP`` when the new period meets another)."""
        org_id = ctx.organization_id
        try:
            async with unit_of_work(self.session):
                rate = await self._locked(org_id, rate_id)
                end = data.effective_to
                if end < rate.effective_from:
                    raise ValidationError.for_field(
                        "effective_to", "Must not be before the rate's first day."
                    )
                overlaps = await self.rates.overlapping(
                    org_id,
                    user_id=rate.user_id,
                    grade_id=rate.grade_id,
                    start=rate.effective_from,
                    end=end,
                    exclude_id=rate.id,
                )
                if overlaps:
                    raise overlap_error(overlaps)
                old = rate.effective_to
                if old != end:
                    rate.effective_to = end
                    await self.rates.save(rate)
                view = await self._view(org_id, rate.id)
                if old != end:
                    await self._audit(
                        ctx,
                        rctx,
                        "overtime_rate.ended",
                        view,
                        {"changes": {"effective_to": [_iso(old), end.isoformat()]}},
                    )
        except IntegrityError as exc:
            if is_exclusion_violation(exc):
                raise overlap_error() from exc
            raise
        return view

    async def delete(self, ctx: OrgContext, rate_id: uuid.UUID, rctx: RequestContext) -> None:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            rate = await self._locked(org_id, rate_id)
            view = await self._view(org_id, rate.id)
            await self.rates.delete(org_id, rate.id)
            await self._audit(ctx, rctx, "overtime_rate.deleted", view, {})

    async def resolve(
        self, ctx: OrgContext, user_id: uuid.UUID, on_date: date | None
    ) -> OvertimeResolveRead:
        """The one overtime rate of the person on the day: their own, else their grade's."""
        org_id = ctx.organization_id
        member = await self.members.get(org_id, user_id)
        if member is None:
            raise NotFoundError("Staff member not found.")
        today = self.today(ctx)
        day = on_date or today
        source: Literal["staff", "grade"] | None = None
        found = await self.rates.list_all(
            org_id, OvertimeRateFilters(level="staff", user_id=user_id, on_date=day)
        )
        if found:
            source = "staff"
        elif member.grade_id is not None:
            found = await self.rates.list_all(
                org_id, OvertimeRateFilters(level="grade", grade_id=member.grade_id, on_date=day)
            )
            if found:
                source = "grade"
        return OvertimeResolveRead(
            user_id=user_id,
            on_date=day,
            grade_id=member.grade_id,
            source=source,
            rate=OvertimeRateRead.from_view(found[0], today=today, show_cost=True)
            if found
            else None,
        )

    # --- Internals ------------------------------------------------------------------------

    async def _check_references(self, org_id: uuid.UUID, data: OvertimeRateCreate) -> None:
        """The grade or person must belong to this practice (422 on the field otherwise)."""
        if data.grade_id is not None and await self.grades.get(org_id, data.grade_id) is None:
            raise ValidationError.for_field("grade_id", "This grade does not exist.")
        if data.user_id is not None:
            member = await self.members.get(org_id, data.user_id)
            if member is None:
                raise ValidationError.for_field(
                    "user_id", "Choose a staff member of this practice."
                )
            if member.status in GONE_STATUSES:
                raise ConflictError("This person has left the practice.", code="MEMBER_REMOVED")

    async def _locked(self, org_id: uuid.UUID, rate_id: uuid.UUID) -> OvertimeRate:
        found = await self.rates.get(org_id, rate_id)
        if found is None:
            raise NotFoundError("Overtime rate not found.")
        await self.rates.lock_scope(org_id, found.user_id, found.grade_id)
        rate = await self.rates.get(org_id, rate_id, lock=True)
        if rate is None:
            raise NotFoundError("Overtime rate not found.")
        return rate

    async def _view(self, org_id: uuid.UUID, rate_id: uuid.UUID) -> OvertimeRateView:
        view = await self.rates.view(org_id, rate_id)
        assert view is not None
        return view

    async def _audit(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        action: str,
        view: OvertimeRateView,
        extra: dict[str, Any],
    ) -> None:
        await record_audit(
            self.session,
            ctx,
            rctx,
            action,
            "overtime_rate",
            view.rate.id,
            {**_meta(view), **extra},
        )
