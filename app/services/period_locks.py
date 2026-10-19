"""Period locks (Settings > Period Lock): closing time, expenses and WIP up to a date.

A practice has one lock: the last locked day (``locked_through``, inclusive; none = nothing
locked). Locking moves it forward, up to today; reopening moves it back and needs a reason.
Every change is a row of the append-only history and an audit entry.

Enforcement is :func:`assert_open` / :func:`assert_open_many`. Every write path that creates,
edits or deletes dated time, expense or WIP data calls one of them inside its unit of work,
before it writes (docs/modules/period-locks.md lists the call sites). Reads, invoices and
payments are never blocked. :func:`locked_dates` serves re-rating, which skips locked time.
"""

import uuid
from collections.abc import Collection
from datetime import date
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import ConflictError, ValidationError
from app.db.models import PeriodLock, PeriodLockEvent
from app.db.session import unit_of_work
from app.repositories.period_locks import PeriodLockRepository
from app.schemas.period_locks import (
    PeriodLockCreate,
    PeriodLockEventRead,
    PeriodLockRead,
    PeriodLockReopen,
)
from app.services.common import plain, record_audit
from app.services.policy import OrgContext
from app.utils.pagination import Page, PageParams
from app.utils.practice_dates import today_in

PERIOD_LOCKED = "PERIOD_LOCKED"
NOT_FORWARD = "PERIOD_LOCK_NOT_FORWARD"
IN_FUTURE = "PERIOD_LOCK_IN_FUTURE"
NOT_BACKWARD = "PERIOD_LOCK_NOT_BACKWARD"
REASON_REQUIRED = "PERIOD_LOCK_REASON_REQUIRED"

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

    def __init__(self, on_date: date, locked_through: date, area: PeriodArea) -> None:
        super().__init__(
            f"{_SUBJECT[area]} dated {_uk(on_date)} cannot be changed: "
            f"the period is locked through {_uk(locked_through)}.",
            details={
                "date": on_date.isoformat(),
                "locked_through": locked_through.isoformat(),
                "area": area,
            },
        )


async def assert_open(
    session: AsyncSession, organization_id: uuid.UUID, on_date: date, area: PeriodArea
) -> None:
    """Refuse a change dated ``on_date`` when it lies in a locked period (409 ``PERIOD_LOCKED``).

    Every write path that creates, edits or deletes dated time, expense or WIP data calls this
    inside its unit of work, before it writes; reads are never blocked. A day is locked when it
    is on or before ``locked_through``. The lock row stays share-locked until the transaction
    ends, so a lock that is being set waits for this write, and a write that starts after the
    lock waits for it and is refused.
    """
    locked_through = await PeriodLockRepository(session).locked_through_shared(organization_id)
    if locked_through is not None and on_date <= locked_through:
        raise PeriodLockedError(on_date, locked_through, area)


async def assert_open_many(
    session: AsyncSession,
    organization_id: uuid.UUID,
    days: Collection[date],
    area: PeriodArea,
) -> None:
    """:func:`assert_open` for several dates at once (one lookup; the error names the earliest)."""
    if not days:
        return
    locked_through = await PeriodLockRepository(session).locked_through_shared(organization_id)
    earliest = min(days)
    if locked_through is not None and earliest <= locked_through:
        raise PeriodLockedError(earliest, locked_through, area)


async def locked_dates(
    session: AsyncSession, organization_id: uuid.UUID, days: Collection[date]
) -> dict[date, str]:
    """``{day: reason}`` for each of ``days`` that lies in a locked period of the practice.

    The reason is shown to the user next to the skipped entry (``Period locked until
    31/03/2026``). Days that are not locked are left out.
    """
    if not days:
        return {}
    locked_through = await PeriodLockRepository(session).locked_through_shared(organization_id)
    if locked_through is None:
        return {}
    reason = f"Period locked until {_uk(locked_through)}"
    return {day: reason for day in days if day <= locked_through}


def _refusal(code: str, field: str, message: str) -> ValidationError:
    """422 whose ``error.code`` and ``details[0].code`` both carry ``code``."""
    return ValidationError(
        message, code=code, details=[{"field": field, "code": code, "message": message}]
    )


class PeriodLockService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = PeriodLockRepository(session)

    async def _state(self, row: PeriodLock | None, event: PeriodLockEvent | None) -> PeriodLockRead:
        names = await self.repo.actor_names({event.actor_user_id}) if event else {}
        return PeriodLockRead(
            locked_through=row.locked_through if row else None,
            updated_at=row.updated_at if row is not None and event is not None else None,
            last_event=PeriodLockEventRead.from_model(event, names.get(event.actor_user_id))
            if event
            else None,
        )

    async def get(self, ctx: OrgContext) -> PeriodLockRead:
        row = await self.repo.get(ctx.organization_id)
        return await self._state(row, await self.repo.last_event(ctx.organization_id))

    async def history(self, ctx: OrgContext, params: PageParams) -> Page[PeriodLockEventRead]:
        page = await self.repo.list_events(ctx.organization_id, params)
        names = await self.repo.actor_names({e.actor_user_id for e in page.items})
        return Page(
            items=[
                PeriodLockEventRead.from_model(e, names.get(e.actor_user_id)) for e in page.items
            ],
            next_cursor=page.next_cursor,
        )

    async def lock(
        self, ctx: OrgContext, data: PeriodLockCreate, rctx: RequestContext
    ) -> PeriodLockRead:
        today = today_in(ctx.organization.timezone)
        if data.locked_through > today:
            raise _refusal(
                IN_FUTURE,
                "locked_through",
                f"A period can be locked up to today ({_uk(today)}) at most.",
            )
        async with unit_of_work(self.session):
            row = await self.repo.lock_row(ctx.organization_id)
            previous = row.locked_through
            if previous is not None and data.locked_through <= previous:
                raise _refusal(
                    NOT_FORWARD,
                    "locked_through",
                    f"The period is already locked through {_uk(previous)}. "
                    "Lock a later date, or reopen the period first.",
                )
            row.locked_through = data.locked_through
            await self.session.flush()
            await self.session.refresh(row)
            event = await self.repo.add_event(
                PeriodLockEvent(
                    organization_id=ctx.organization_id,
                    event_type="locked",
                    previous_locked_through=previous,
                    locked_through=data.locked_through,
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
                    "locked_through": data.locked_through.isoformat(),
                    "previous_locked_through": plain(previous),
                    "reason": data.reason,
                    "changes": {"locked_through": [plain(previous), plain(data.locked_through)]},
                },
            )
            result = await self._state(row, event)
        return result

    async def reopen(
        self, ctx: OrgContext, data: PeriodLockReopen, rctx: RequestContext
    ) -> PeriodLockRead:
        if not data.reason:
            raise _refusal(REASON_REQUIRED, "reason", "Say why the period is being reopened.")
        target = data.reopen_to
        async with unit_of_work(self.session):
            row = await self.repo.lock_row(ctx.organization_id)
            previous = row.locked_through
            if previous is None:
                raise _refusal(
                    NOT_BACKWARD, "reopen_to", "Nothing is locked, so there is nothing to reopen."
                )
            if target is not None and target >= previous:
                raise _refusal(
                    NOT_BACKWARD,
                    "reopen_to",
                    f"The period is locked through {_uk(previous)}: reopen to an earlier date, "
                    "or to none to unlock everything.",
                )
            row.locked_through = target
            await self.session.flush()
            await self.session.refresh(row)
            event = await self.repo.add_event(
                PeriodLockEvent(
                    organization_id=ctx.organization_id,
                    event_type="reopened",
                    previous_locked_through=previous,
                    locked_through=target,
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
                    "locked_through": plain(target),
                    "previous_locked_through": previous.isoformat(),
                    "reason": data.reason,
                    "changes": {"locked_through": [plain(previous), plain(target)]},
                },
            )
            result = await self._state(row, event)
        return result
