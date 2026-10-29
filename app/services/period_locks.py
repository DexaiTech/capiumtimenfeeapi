"""Period locks (Settings > Period Lock): closing time, expenses and WIP up to a date.

A practice locks whole weeks, months, quarters or accounting years, each on its own
(``locked_periods``). Which of them is a practice setting (``weekly`` unless it chooses
otherwise) and a new lock is exactly one period of that length inside the accounting year
(``app/utils/lock_periods.py``); locks made earlier, of any length, stay in force. A period can be
locked once it has ended and cannot overlap another lock; unlocking one needs a reason. Every
change is a row of the append-only history and an audit entry.

Enforcement is :func:`assert_open` / :func:`assert_open_many`. Every write path that creates,
edits or deletes dated time, expense or WIP data calls one of them inside its unit of work,
before it writes (docs/modules/period-locks.md lists the call sites). Reads, invoices and
payments are never blocked. :func:`locked_dates` serves re-rating, which skips locked time.
"""

import calendar
import uuid
from collections.abc import Collection
from datetime import date
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.db.models import LockedPeriod, Organization, PeriodLockEvent
from app.db.session import unit_of_work
from app.repositories.period_locks import PeriodLockRepository
from app.schemas.period_locks import (
    LockedPeriodRead,
    LockPeriodRow,
    LockYearSummary,
    PeriodLockCalendar,
    PeriodLockCreate,
    PeriodLockEventRead,
    PeriodLockRead,
    PeriodLockReopen,
    PeriodLockSettingsRead,
    PeriodLockSettingsUpdate,
)
from app.services.common import record_audit
from app.services.policy import OrgContext
from app.utils.lock_periods import (
    GRANULARITY_PERIOD,
    accounting_year_of,
    period_bounds,
    period_label,
    year_bounds,
    year_label,
    year_periods,
)
from app.utils.pagination import Page, PageParams
from app.utils.practice_dates import today_in

PERIOD_LOCKED = "PERIOD_LOCKED"
OVERLAPS = "PERIOD_LOCK_OVERLAPS"
IN_FUTURE = "PERIOD_LOCK_IN_FUTURE"
INVALID_PERIOD = "PERIOD_LOCK_INVALID_PERIOD"
REASON_REQUIRED = "PERIOD_LOCK_REASON_REQUIRED"
WRONG_LENGTH = "PERIOD_LOCK_WRONG_LENGTH"
# Accounting years offered next to the current one when nothing older is locked.
YEARS_BACK = 5

# The data a lock closes: recorded time, expenses (claims and disbursements) and WIP movements.
PeriodArea = Literal["time", "expense", "wip"]
_SUBJECT: dict[PeriodArea, str] = {
    "time": "Time",
    "expense": "An expense",
    "wip": "A WIP movement",
}


def _uk(day: date) -> str:
    return day.strftime("%d/%m/%Y")


class PeriodLockedError(ConflictError):
    """409 ``PERIOD_LOCKED``: the change is dated inside a locked period."""

    code = PERIOD_LOCKED

    def __init__(self, on_date: date, period: LockedPeriod, area: PeriodArea) -> None:
        label = period_label(period.period_type, period.start_date, period.end_date)
        super().__init__(
            f"{_SUBJECT[area]} dated {_uk(on_date)} cannot be changed: "
            f"the period is locked ({label}, through {_uk(period.end_date)}). "
            "Unlock it in Settings > Period Lock to make changes.",
            details={
                "date": on_date.isoformat(),
                "locked_through": period.end_date.isoformat(),
                "area": area,
            },
        )


def _covering(periods: Collection[LockedPeriod], day: date) -> LockedPeriod | None:
    """The locked period containing ``day`` that ends last, if any."""
    found = [p for p in periods if p.start_date <= day <= p.end_date]
    return max(found, key=lambda p: p.end_date) if found else None


async def assert_open(
    session: AsyncSession, organization_id: uuid.UUID, on_date: date, area: PeriodArea
) -> None:
    """Refuse a change dated ``on_date`` when it lies in a locked period (409 ``PERIOD_LOCKED``).

    Every write path that creates, edits or deletes dated time, expense or WIP data calls this
    inside its unit of work, before it writes; reads are never blocked. The practice's lock row
    stays share-locked until the transaction ends, so a lock that is being set waits for this
    write, and a write that starts after the lock waits for it and is refused.
    """
    periods = await PeriodLockRepository(session).locked_periods_shared(organization_id)
    period = _covering(periods, on_date)
    if period is not None:
        raise PeriodLockedError(on_date, period, area)


async def assert_open_many(
    session: AsyncSession,
    organization_id: uuid.UUID,
    days: Collection[date],
    area: PeriodArea,
) -> None:
    """:func:`assert_open` for several dates at once (one lookup; the error names the earliest)."""
    if not days:
        return
    periods = await PeriodLockRepository(session).locked_periods_shared(organization_id)
    for day in sorted(days):
        period = _covering(periods, day)
        if period is not None:
            raise PeriodLockedError(day, period, area)


async def locked_dates(
    session: AsyncSession, organization_id: uuid.UUID, days: Collection[date]
) -> dict[date, str]:
    """``{day: reason}`` for each of ``days`` that lies in a locked period of the practice.

    The reason is shown to the user next to the skipped entry (``Period locked until
    31/03/2026``). Days that are not locked are left out.
    """
    if not days:
        return {}
    periods = await PeriodLockRepository(session).locked_periods_shared(organization_id)
    result: dict[date, str] = {}
    for day in days:
        period = _covering(periods, day)
        if period is not None:
            result[day] = f"Period locked until {_uk(period.end_date)}"
    return result


def _refusal(code: str, field: str, message: str) -> ValidationError:
    """422 whose ``error.code`` and ``details[0].code`` both carry ``code``."""
    return ValidationError(
        message, code=code, details=[{"field": field, "code": code, "message": message}]
    )


class PeriodLockService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = PeriodLockRepository(session)

    async def _state(self, organization_id: uuid.UUID) -> PeriodLockRead:
        periods = await self.repo.list_periods(organization_id)
        event = await self.repo.last_event(organization_id)
        ids = {p.locked_by for p in periods if p.locked_by}
        if event:
            ids.add(event.actor_user_id)
        names = await self.repo.actor_names(ids)
        organization = await self.session.get(Organization, organization_id)
        assert organization is not None
        return PeriodLockRead(
            settings=PeriodLockSettingsRead.from_model(organization),
            periods=[
                LockedPeriodRead.from_model(p, names.get(p.locked_by) if p.locked_by else None)
                for p in periods
            ],
            last_event=PeriodLockEventRead.from_model(event, names.get(event.actor_user_id))
            if event
            else None,
        )

    async def get(self, ctx: OrgContext) -> PeriodLockRead:
        return await self._state(ctx.organization_id)

    async def history(self, ctx: OrgContext, params: PageParams) -> Page[PeriodLockEventRead]:
        page = await self.repo.list_events(ctx.organization_id, params)
        names = await self.repo.actor_names({e.actor_user_id for e in page.items})
        return Page(
            items=[
                PeriodLockEventRead.from_model(e, names.get(e.actor_user_id)) for e in page.items
            ],
            next_cursor=page.next_cursor,
        )

    async def calendar(self, ctx: OrgContext, year: int | None) -> PeriodLockCalendar:
        """The periods of one accounting year (this one by default) with their lock state."""
        org = ctx.organization
        month, day = org.financial_year_start_month, org.financial_year_start_day
        today = today_in(org.timezone)
        current = accounting_year_of(today, month, day)
        year = current if year is None else year
        period_type = GRANULARITY_PERIOD[org.period_lock_granularity]
        locks = await self.repo.list_periods(ctx.organization_id)
        names = await self.repo.actor_names({p.locked_by for p in locks if p.locked_by})

        def periods_of(of_year: int) -> list[tuple[int, date, date]]:
            return year_periods(
                period_type,
                of_year,
                start_month=month,
                start_day=day,
                week_start=org.week_start,
            )

        rows = []
        for index, start, end in periods_of(year):
            exact = next((p for p in locks if (p.start_date, p.end_date) == (start, end)), None)
            other = next((p for p in locks if p.start_date <= end and p.end_date >= start), None)
            status: Literal["open", "locked", "blocked", "future"]
            if exact is not None:
                status = "locked"
            elif other is not None:
                status = "blocked"
            elif end > today:
                status = "future"
            else:
                status = "open"
            rows.append(
                LockPeriodRow(
                    period_type=period_type,
                    year=year,
                    period=index,
                    start_date=start,
                    end_date=end,
                    label=period_label(period_type, start, end),
                    status=status,
                    lock_id=exact.id if exact else None,
                    blocked_by=(
                        period_label(other.period_type, other.start_date, other.end_date)
                        if other and not exact
                        else None
                    ),
                    locked_at=exact.locked_at if exact else None,
                    locked_by_name=names.get(exact.locked_by)
                    if exact and exact.locked_by
                    else None,
                )
            )
        oldest = min(
            (
                accounting_year_of(p.start_date, month, day)
                for p in locks
                if p.period_type != "legacy"
            ),
            default=current,
        )
        years = []
        for of_year in range(current, min(oldest, current - YEARS_BACK) - 1, -1):
            first, last = year_bounds(of_year, month, day)
            periods = periods_of(of_year)
            years.append(
                LockYearSummary(
                    year=of_year,
                    label=year_label(of_year, month, day),
                    start_date=first,
                    end_date=last,
                    locked=sum(
                        1
                        for _, s, e in periods
                        if any((p.start_date, p.end_date) == (s, e) for p in locks)
                    ),
                    total=len(periods),
                )
            )
        first, last = year_bounds(year, month, day)
        return PeriodLockCalendar(
            settings=PeriodLockSettingsRead.from_model(org),
            year=year,
            label=year_label(year, month, day),
            start_date=first,
            end_date=last,
            periods=rows,
            years=years,
        )

    async def update_settings(
        self, ctx: OrgContext, data: PeriodLockSettingsUpdate, rctx: RequestContext
    ) -> PeriodLockRead:
        async with unit_of_work(self.session):
            row = await self.repo.lock_row(ctx.organization_id)
            org = ctx.organization
            await self.session.refresh(org)
            values: dict[str, Any] = {
                "period_lock_granularity": data.granularity,
                "period_lock_configured": True,
            }
            if data.financial_year_start_month is not None:
                values["financial_year_start_month"] = data.financial_year_start_month
            if data.financial_year_start_day is not None:
                values["financial_year_start_day"] = data.financial_year_start_day
            month = values.get("financial_year_start_month", org.financial_year_start_month)
            day = values.get("financial_year_start_day", org.financial_year_start_day)
            if day > calendar.monthrange(2001, month)[1]:
                raise _refusal(
                    INVALID_PERIOD,
                    "financial_year_start_day",
                    "That day does not exist in that month.",
                )
            changes: dict[str, list[Any]] = {}
            for name, value in values.items():
                old = getattr(org, name)
                if old != value:
                    changes[name] = [old, value]
                    setattr(org, name, value)
            if changes:
                await self.session.flush()
                await record_audit(
                    self.session,
                    ctx,
                    rctx,
                    "period_lock.settings_changed",
                    "period_lock",
                    row.id,
                    {"changes": changes},
                )
            result = await self._state(ctx.organization_id)
        return result

    async def lock(
        self, ctx: OrgContext, data: PeriodLockCreate, rctx: RequestContext
    ) -> PeriodLockRead:
        async with unit_of_work(self.session):
            row = await self.repo.lock_row(ctx.organization_id)
            # Read under the row lock: the settings cannot change between this check and the write.
            org = ctx.organization
            await self.session.refresh(org)
            expected = GRANULARITY_PERIOD[org.period_lock_granularity]
            if data.period_type != expected:
                raise _refusal(
                    WRONG_LENGTH,
                    "period_type",
                    f"This practice locks {org.period_lock_granularity}, one {expected} at a "
                    f"time, not by the {data.period_type}. Change the lock length in Settings > "
                    "Period Lock first.",
                )
            try:
                start, end = period_bounds(
                    data.period_type,
                    data.year,
                    data.period,
                    start_month=org.financial_year_start_month,
                    start_day=org.financial_year_start_day,
                    week_start=org.week_start,
                )
            except ValueError as exc:
                raise _refusal(INVALID_PERIOD, "period", str(exc)) from exc
            label = period_label(data.period_type, start, end)
            if end > today_in(org.timezone):
                raise _refusal(
                    IN_FUTURE,
                    "period",
                    f"{label} has not ended yet: a period can be locked once its last day "
                    f"({_uk(end)}) has passed.",
                )
            clash = await self.repo.overlapping(ctx.organization_id, start, end)
            if clash is not None:
                raise _refusal(
                    OVERLAPS,
                    "period",
                    f"{label} overlaps a period that is already locked "
                    f"({period_label(clash.period_type, clash.start_date, clash.end_date)}). "
                    "Unlock that one first.",
                )
            await self.repo.add_period(
                LockedPeriod(
                    organization_id=ctx.organization_id,
                    period_type=data.period_type,
                    start_date=start,
                    end_date=end,
                    reason=data.reason,
                    locked_by=ctx.user.id,
                )
            )
            await self.repo.add_event(
                PeriodLockEvent(
                    organization_id=ctx.organization_id,
                    event_type="locked",
                    period_type=data.period_type,
                    period_start=start,
                    period_end=end,
                    reason=data.reason,
                    actor_user_id=ctx.user.id,
                )
            )
            await record_audit(
                self.session,
                ctx,
                rctx,
                "period_lock.locked",
                "period_lock",
                row.id,
                {
                    "event_type": "locked",
                    "period": label,
                    "period_type": data.period_type,
                    "period_start": start.isoformat(),
                    "period_end": end.isoformat(),
                    "reason": data.reason,
                    "changes": {"period": [None, label]},
                },
            )
            result = await self._state(ctx.organization_id)
        return result

    async def reopen(
        self, ctx: OrgContext, data: PeriodLockReopen, rctx: RequestContext
    ) -> PeriodLockRead:
        if not data.reason:
            raise _refusal(REASON_REQUIRED, "reason", "Say why the period is being unlocked.")
        async with unit_of_work(self.session):
            row = await self.repo.lock_row(ctx.organization_id)
            period = await self.repo.get_period(ctx.organization_id, data.lock_id)
            if period is None:
                raise NotFoundError("That period is not locked.")
            label = period_label(period.period_type, period.start_date, period.end_date)
            await self.repo.delete_period(period)
            await self.repo.add_event(
                PeriodLockEvent(
                    organization_id=ctx.organization_id,
                    event_type="reopened",
                    period_type=period.period_type,
                    period_start=period.start_date,
                    period_end=period.end_date,
                    reason=data.reason,
                    actor_user_id=ctx.user.id,
                )
            )
            await record_audit(
                self.session,
                ctx,
                rctx,
                "period_lock.reopened",
                "period_lock",
                row.id,
                {
                    "event_type": "reopened",
                    "period": label,
                    "period_type": period.period_type,
                    "period_start": period.start_date.isoformat(),
                    "period_end": period.end_date.isoformat(),
                    "reason": data.reason,
                    "changes": {"period": [label, None]},
                },
            )
            result = await self._state(ctx.organization_id)
        return result
