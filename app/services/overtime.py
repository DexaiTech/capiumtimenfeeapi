"""Overtime of time entries: capture and approval (ticket APT81, phases B and C;
docs/modules/time.md, "Overtime").

Capture:

* Only while the practice has overtime on (``organizations.overtime_enabled``). Off, nothing is
  worked out, nobody is asked, and overtime already recorded stays as it is.
* A person's overtime period is the week (Monday to Sunday) or the calendar month of the working
  hours (``staff_hours_bases``) in force on the day, and those hours' standard minutes are its
  allowance. A day without working hours has no overtime. When the hours change inside a week or
  month, each set of hours counts only its own days.
* The period's entries count in order of entry date, then the time they were recorded (then id):
  the minutes beyond the allowance are overtime. The entry that crosses the line is split, only
  the part beyond it being overtime.
* Every time write works the periods it touches out again (edits and deletes re-split later
  entries). A write that adds overtime is refused with 409 ``OVERTIME_CONFIRMATION_REQUIRED``
  unless it says ``confirm_overtime: true``; a re-split that adds none needs no confirmation.
  Days in a locked period are never rewritten.
* An entry's overtime row (``time_entry_overtime``) keeps the overtime minutes, whether it is
  allocated (on a job of a client other than the built-in Internal client), the overtime rate
  that applied (the person's own, else their grade's, else none at 0), its cost and the premium
  over the standard cost. It is ``pending``, or ``approved`` straight away when the person's time
  needs no approval. ``time_entries.is_overtime`` says whether the entry has an overtime portion.
* Charge-out, WIP, invoices and profitability never read overtime rows: overtime cost shows only
  on the Staff Cost report.

Approval: the person's approver (``timesheet.approve`` and their manager) or anyone with
``time.manage_all`` approves or rejects an overtime row; rejecting removes only the premium (the
hours stay, at their standard cost).
"""

import uuid
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import AuthorizationError, ConflictError, NotFoundError
from app.db.models import (
    Organization,
    OvertimeRate,
    StaffHoursBasis,
    TimeEntry,
    TimeEntryOvertime,
)
from app.db.session import unit_of_work
from app.repositories.overtime import (
    OvertimeFilters,
    OvertimeRateFilters,
    OvertimeRateRepository,
    PeriodEntry,
    StaffHoursRepository,
    TimeOvertimeRepository,
)
from app.repositories.time_rates import RateLookupRepository
from app.repositories.time_refs import TimeReferenceRepository
from app.schemas.overtime import OvertimeRead
from app.services import audit, period_locks
from app.services.common import record_audit
from app.services.policy import OrgContext, has_permission
from app.services.time_rules import InvalidTransitionError, practice_now
from app.utils.money import time_value
from app.utils.pagination import Page, PageParams

# Who sees overtime rates and costs outside the Staff Cost report (the approval list).
COST_PERMISSION = "report.staff_cost"


class OvertimeConfirmationRequiredError(ConflictError):
    code = "OVERTIME_CONFIRMATION_REQUIRED"
    message = "You are adding overtime hours. Are you sure?"


class SelfOvertimeDecisionError(AuthorizationError):
    code = "SELF_APPROVAL_FORBIDDEN"
    message = "You cannot decide on your own overtime."


def period_of(hours: StaffHoursBasis, day: date) -> tuple[date, date]:
    """The overtime period of ``day`` under ``hours``: its week (Monday to Sunday) or calendar
    month, cut to the days the hours are in force."""
    if hours.basis == "weekly":
        start = day - timedelta(days=day.weekday())
        end = start + timedelta(days=6)
    else:
        start = day.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    start = max(start, hours.effective_from)
    if hours.effective_to is not None:
        end = min(end, hours.effective_to)
    return start, end


def hours_on(bases: Iterable[StaffHoursBasis], day: date) -> StaffHoursBasis | None:
    """The working hours in force on ``day``, if any."""
    for hours in bases:
        if hours.effective_from <= day and (
            hours.effective_to is None or day <= hours.effective_to
        ):
            return hours
    return None


def _times_bp(rate: int, bp: int) -> int:
    """``rate`` x ``bp`` / 10000, rounded half up."""
    return (rate * bp + 5000) // 10000


def _covering(rates: Sequence[OvertimeRate], day: date) -> OvertimeRate | None:
    for rate in rates:
        if rate.effective_from <= day and (rate.effective_to is None or day <= rate.effective_to):
            return rate
    return None


@dataclass(frozen=True, slots=True)
class _Price:
    rate_id: uuid.UUID | None
    source: str
    rate: int
    cost: int
    premium: int


@dataclass(frozen=True, slots=True)
class _Period:
    hours: StaffHoursBasis
    start: date
    end: date


async def lock_people(
    session: AsyncSession, organization: Organization, user_ids: Iterable[uuid.UUID]
) -> None:
    """Serialise the overtime of these people while the practice has overtime on. Every time
    write takes it before it locks any timesheet or entry, so two writes in one person's month
    never work it out from each other's half."""
    if not organization.overtime_enabled:
        return
    repo = TimeOvertimeRepository(session)
    for user_id in sorted(set(user_ids)):
        await repo.lock_person(organization.id, user_id)


class OvertimeCapture:
    """Works out the overtime of the periods a time write touched (inside its transaction)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = TimeOvertimeRepository(session)
        self.hours = StaffHoursRepository(session)
        self.rates = OvertimeRateRepository(session)
        self.lookup = RateLookupRepository(session)

    async def recompute(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        touched: Mapping[uuid.UUID, Collection[date]],
        *,
        confirmed: bool = False,
        ask: bool = True,
        written: Collection[uuid.UUID] = (),
    ) -> None:
        """Re-split the periods of ``touched`` (person -> days written, before and after).

        ``ask``: whether added overtime needs ``confirmed`` (a delete never adds any).
        ``written``: the entries this write changed; their overtime is priced again.
        """
        org = ctx.organization
        if not org.overtime_enabled or not touched:
            return
        await self.session.flush()
        await lock_people(self.session, org, touched)
        seen: set[uuid.UUID] = set()
        for user_id in sorted(touched):
            seen |= await self._person(
                ctx, rctx, user_id, touched[user_id], confirmed, ask, set(written)
            )
        # A written entry now on a day without working hours has no overtime any more.
        stray = set(written) - seen
        for row in (await self.repo.for_entries(org.id, stray)).values():
            await self.repo.delete(row)
        for entry_id in sorted(stray):
            entry = await self.session.get(TimeEntry, entry_id)
            if entry is not None and entry.is_overtime:
                entry.is_overtime = False
        await self.session.flush()

    async def _person(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        user_id: uuid.UUID,
        days: Collection[date],
        confirmed: bool,
        ask: bool,
        written: set[uuid.UUID],
    ) -> set[uuid.UUID]:
        """Re-split the person's periods of ``days``; the ids of the entries in them."""
        org_id = ctx.organization_id
        bases = await self.hours.list_for(org_id, user_id)
        periods: dict[tuple[uuid.UUID, date], _Period] = {}
        for day in days:
            hours = hours_on(bases, day)
            if hours is not None:
                start, end = period_of(hours, day)
                periods[(hours.id, start)] = _Period(hours, start, end)
        if not periods:
            return set()
        ordered = sorted(periods.values(), key=lambda p: p.start)
        found = {
            p.start: await self.repo.period_entries(org_id, user_id, p.start, p.end)
            for p in ordered
        }
        everything = [pe for p in ordered for pe in found[p.start]]
        existing = await self.repo.for_entries(org_id, [pe.entry.id for pe in everything])
        locked = await period_locks.locked_dates(
            self.session, org_id, {pe.entry.entry_date for pe in everything}
        )

        plan: list[tuple[PeriodEntry, int]] = []
        added = 0
        first: _Period | None = None
        for period in ordered:
            running = before = after = 0
            for pe in found[period.start]:
                entry = pe.entry
                over = max(
                    0, min(entry.minutes, running + entry.minutes - period.hours.standard_minutes)
                )
                running += entry.minutes
                row = existing.get(entry.id)
                old = row.overtime_minutes if row else 0
                if entry.entry_date in locked:
                    over = old  # a locked period is never rewritten
                else:
                    plan.append((pe, over))
                before += old
                after += over
            if after > before and first is None:
                first = period
            added += after - before

        if ask and added > 0 and not confirmed:
            assert first is not None
            raise OvertimeConfirmationRequiredError(
                details={
                    "overtime_minutes": added,
                    "standard_minutes": first.hours.standard_minutes,
                    "period_start": first.start.isoformat(),
                    "period_end": first.end.isoformat(),
                }
            )

        confirming = ask and confirmed and added > 0
        now = practice_now()
        prices = await self._pricer(org_id, user_id)
        increased: list[uuid.UUID] = []
        for pe, over in plan:
            entry = pe.entry
            row = existing.get(entry.id)
            if over == 0:
                if row is not None:
                    await self.repo.delete(row)
                if entry.is_overtime:
                    entry.is_overtime = False
                continue
            entry.is_overtime = True
            grew = row is None or over > row.overtime_minutes
            moved = row is None or row.overtime_minutes != over or row.allocated != pe.allocated
            if not moved and entry.id not in written:
                continue
            price = prices(entry, over)
            if row is None:
                row = TimeEntryOvertime(
                    organization_id=org_id, time_entry_id=entry.id, user_id=user_id
                )
                self.session.add(row)
            row.overtime_minutes = over
            row.allocated = pe.allocated
            row.overtime_rate_id = price.rate_id
            row.rate_source = price.source
            row.overtime_rate = price.rate
            row.overtime_cost = price.cost
            row.premium_cost = price.premium
            if moved:
                # Different overtime: decided again (straight away when no approval is needed).
                row.status = "approved" if pe.approval_exempt else "pending"
                row.decided_by = row.decided_at = row.decision_note = None
            if confirming and grew:
                row.confirmed_by = ctx.user.id
                row.confirmed_at = now
                increased.append(entry.id)
        await self.session.flush()

        if confirming:
            assert first is not None
            await record_audit(
                self.session,
                ctx,
                rctx,
                "overtime.confirmed",
                "member",
                user_id,
                {
                    "user_id": str(user_id),
                    "overtime_minutes": added,
                    "standard_minutes": first.hours.standard_minutes,
                    "period_start": first.start.isoformat(),
                    "period_end": first.end.isoformat(),
                    "time_entry_ids": [str(i) for i in increased],
                },
            )
        return {pe.entry.id for pe in everything}

    async def _pricer(
        self, org_id: uuid.UUID, user_id: uuid.UUID
    ) -> Callable[[TimeEntry, int], _Price]:
        """``price(entry, minutes)``: the overtime rate that applies on the entry's day (the
        person's own, else their grade's; never both), its cost and the premium over the
        standard cost of the same minutes."""
        own = [
            v.rate
            for v in await self.rates.list_all(
                org_id, OvertimeRateFilters(level="staff", user_id=user_id)
            )
        ]
        grade_id = (await self.lookup.member_grades(org_id, {user_id})).get(user_id)
        graded = (
            [
                v.rate
                for v in await self.rates.list_all(
                    org_id, OvertimeRateFilters(level="grade", grade_id=grade_id)
                )
            ]
            if grade_id is not None
            else []
        )

        def price(entry: TimeEntry, minutes: int) -> _Price:
            day = entry.entry_date
            rate, source = _covering(own, day), "staff"
            if rate is None:
                rate, source = _covering(graded, day), "grade"
            if rate is None:
                return _Price(None, "none", 0, 0, 0)
            if rate.mode == "multiplier":
                assert rate.multiplier_bp is not None
                applied = _times_bp(entry.cost_rate, rate.multiplier_bp)
            else:
                assert rate.rate is not None
                applied = rate.rate
            cost = time_value(minutes, applied)
            premium = max(0, cost - time_value(minutes, entry.cost_rate))
            return _Price(rate.id, source, applied, cost, premium)

        return price


class OvertimeService:
    """The overtime approval list and decisions (``GET /overtime``, approve, reject)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = TimeOvertimeRepository(session)
        self.refs = TimeReferenceRepository(session)

    async def list_overtime(
        self,
        ctx: OrgContext,
        *,
        user_id: uuid.UUID | None,
        status: str | None,
        date_from: date | None,
        date_to: date | None,
        allocated: bool | None,
        params: PageParams,
    ) -> Page[OvertimeRead]:
        """Everyone's with ``time.read_all`` or ``time.manage_all``; otherwise one's own and the
        people one manages."""
        scope: set[uuid.UUID] | None = None
        if not (has_permission(ctx, "time.read_all") or has_permission(ctx, "time.manage_all")):
            scope = {
                ctx.user.id,
                *await self.refs.managed_user_ids(ctx.organization_id, ctx.user.id),
            }
            if user_id is not None and user_id not in scope:
                raise AuthorizationError()
        users: Collection[uuid.UUID] | None = scope
        if user_id is not None:
            users = [user_id]
        page = await self.repo.views_page(
            ctx.organization_id,
            OvertimeFilters(
                user_ids=users,
                status=status,
                date_from=date_from,
                date_to=date_to,
                allocated=allocated,
            ),
            params,
        )
        cost = has_permission(ctx, COST_PERMISSION)
        return Page(
            items=[OvertimeRead.from_view(v, show_cost=cost) for v in page.items],
            next_cursor=page.next_cursor,
        )

    async def decide(
        self,
        ctx: OrgContext,
        overtime_id: uuid.UUID,
        status: str,
        note: str | None,
        rctx: RequestContext,
    ) -> OvertimeRead:
        """Approve (``approved``) or reject (``rejected``) an overtime row."""
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            row = await self.repo.get(org_id, overtime_id, lock=True)
            if row is None:
                raise NotFoundError("Overtime not found.")
            await self._ensure_can_decide(ctx, row.user_id)
            if row.status == status:
                raise InvalidTransitionError(
                    f"This overtime is already {status}.",
                    details={"from": row.status, "to": status},
                )
            view = await self.repo.view(org_id, row.id)
            await period_locks.assert_open(self.session, org_id, view.entry_date, "time")
            old = row.status
            row.status = status
            row.decided_by = ctx.user.id
            row.decided_at = practice_now()
            row.decision_note = note
            await self.session.flush()
            await audit.record(
                self.session,
                f"overtime.{status}",
                ctx=rctx,
                actor_user_id=ctx.user.id,
                organization_id=org_id,
                target_type="time_entry",
                target_id=row.time_entry_id,
                meta={
                    "overtime_id": str(row.id),
                    "user_id": str(row.user_id),
                    "entry_date": view.entry_date.isoformat(),
                    "overtime_minutes": row.overtime_minutes,
                    "allocated": row.allocated,
                    "changes": {"status": [old, status]},
                    **({"note": note} if note else {}),
                },
            )
            result = OvertimeRead.from_view(
                await self.repo.view(org_id, row.id),
                show_cost=has_permission(ctx, COST_PERMISSION),
            )
        return result

    async def _ensure_can_decide(self, ctx: OrgContext, user_id: uuid.UUID) -> None:
        """``time.manage_all``, or ``timesheet.approve`` and the person's manager; one's own
        overtime only with ``organization.update`` (as timesheets)."""
        if user_id == ctx.user.id and not has_permission(ctx, "organization.update"):
            raise SelfOvertimeDecisionError()
        if has_permission(ctx, "time.manage_all"):
            return
        if has_permission(ctx, "timesheet.approve"):
            member = await self.refs.get_member(ctx.organization_id, user_id)
            if member is not None and member.manager_user_id == ctx.user.id:
                return
        raise AuthorizationError()
