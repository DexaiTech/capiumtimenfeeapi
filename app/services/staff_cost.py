"""The Staff Cost report (ticket APT81, phase D; docs/modules/reports.md, "Staff Cost").

The only place overtime cost is shown. Per staff member and overtime period (the week, Monday to
Sunday, or the calendar month of the working hours in force; days without working hours by
week), within the report's range:

* ``standard_minutes`` / ``standard_cost``: the time within the standard hours at each entry's
  standard cost rate (``time_entries.cost_value`` less the standard cost of its overtime
  minutes). Rejected overtime counts here: rejecting removes only the premium.
* ``overtime_minutes_*`` / ``overtime_cost_*``: pending and approved overtime at the overtime
  rate applied, ``allocated`` (client jobs) and ``unallocated`` (the Internal client's jobs).
* ``total_cost`` = standard + overtime cost; ``pending_overtime_cost``: the unapproved part.

The jobs drill-down lists each person's jobs worked in overtime (the Overtime flag) with the
minutes, rate, cost and status. ``report.staff_cost`` (checked by the route); a download needs
``report.export`` too.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass, fields
from datetime import date, timedelta
from typing import Any, Literal, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.db.models import StaffHoursBasis
from app.repositories.overtime import StaffHoursRepository
from app.repositories.reports import ItemFilter
from app.repositories.staff_cost import OvertimePiece, StaffCostRepository, StaffCostScope
from app.schemas.overtime import OvertimeClientRef, OvertimeJobRef, OvertimeStatus
from app.schemas.staff_cost import (
    StaffCostFigures,
    StaffCostFilters,
    StaffCostJobRow,
    StaffCostJobsQuery,
    StaffCostJobsReport,
    StaffCostJobTotals,
    StaffCostQuery,
    StaffCostReport,
    StaffCostRow,
)
from app.services.overtime import hours_on, period_of
from app.services.policy import OrgContext
from app.services.report_paging import window
from app.services.reports import ReportService
from app.utils.money import time_value


@dataclass(slots=True)
class _Tally:
    standard_minutes: int = 0
    standard_cost: int = 0
    overtime_minutes_allocated: int = 0
    overtime_cost_allocated: int = 0
    overtime_minutes_unallocated: int = 0
    overtime_cost_unallocated: int = 0
    pending_overtime_cost: int = 0
    # Overtime rows that match the status and type filters.
    matched: int = 0

    def figures(self) -> dict[str, int]:
        found = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "matched"}
        found["total_cost"] = (
            self.standard_cost + self.overtime_cost_allocated + self.overtime_cost_unallocated
        )
        return found


_STATUS_ORDER = ("pending", "approved", "rejected")


def _week(day: date) -> tuple[date, date]:
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


class StaffCostService(ReportService):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.repo = StaffCostRepository(session)
        self.hours = StaffHoursRepository(session)

    async def _scope(
        self, ctx: OrgContext, q: StaffCostQuery
    ) -> tuple[StaffCostScope, StaffCostFilters]:
        period = self._range(ctx, q.date_from, q.date_to)
        assert period.start is not None and period.end is not None
        await self._check_refs(
            ctx, ItemFilter(client_id=q.client_id, job_id=q.job_id, user_id=q.user_id)
        )
        if q.grade_id is not None and not await self.repo.grade_exists(
            ctx.organization_id, q.grade_id
        ):
            raise NotFoundError("Grade not found.")
        scope = StaffCostScope(
            period.start, period.end, q.user_id, q.grade_id, q.job_id, q.client_id
        )
        filters = StaffCostFilters(
            date_from=period.start,
            date_to=period.end,
            user_id=q.user_id,
            grade_id=q.grade_id,
            job_id=q.job_id,
            client_id=q.client_id,
            status=list(q.status),
            overtime_type=q.overtime_type,
        )
        return scope, filters

    @staticmethod
    def _matches(piece: OvertimePiece, q: StaffCostQuery) -> bool:
        if q.status and piece.status not in q.status:
            return False
        if q.overtime_type is not None:
            return piece.allocated == (q.overtime_type == "allocated")
        return True

    async def report(self, ctx: OrgContext, q: StaffCostQuery) -> StaffCostReport:
        scope, filters = await self._scope(ctx, q)
        org_id = ctx.organization_id
        days = await self.repo.day_time(org_id, scope)
        pieces = await self.repo.overtime(org_id, scope)
        users = {d.user_id for d in days}
        bases = await self.hours.for_users(org_id, users)

        def key(user_id: uuid.UUID, day: date) -> tuple[uuid.UUID, date, date, uuid.UUID | None]:
            hours = hours_on(bases.get(user_id, []), day)
            start, end = period_of(hours, day) if hours else _week(day)
            return (
                user_id,
                max(start, scope.date_from),
                min(end, scope.date_to),
                (hours.id if hours else None),
            )

        tallies: dict[tuple[uuid.UUID, date, date, uuid.UUID | None], _Tally] = defaultdict(_Tally)
        for d in days:
            t = tallies[key(d.user_id, d.day)]
            t.standard_minutes += d.minutes
            t.standard_cost += d.cost_value
        for p in pieces:
            t = tallies[key(p.user_id, p.day)]
            if p.status == "rejected":
                continue  # its hours stay standard, at their standard cost
            t.standard_minutes -= p.minutes
            t.standard_cost -= time_value(p.minutes, p.cost_rate)
            if not self._matches(p, q):
                continue
            t.matched += 1
            if p.allocated:
                t.overtime_minutes_allocated += p.minutes
                t.overtime_cost_allocated += p.overtime_cost
            else:
                t.overtime_minutes_unallocated += p.minutes
                t.overtime_cost_unallocated += p.overtime_cost
            if p.status == "pending":
                t.pending_overtime_cost += p.overtime_cost
        if q.status or q.overtime_type is not None:
            # Rejected overtime is matched too when asked for: its period is shown, all standard.
            wanted = {key(p.user_id, p.day) for p in pieces if self._matches(p, q)}
            tallies = {k: t for k, t in tallies.items() if k in wanted}

        people = await self.repo.people(org_id, users)
        by_id = {h.id: h for rows in bases.values() for h in rows}
        rows: list[StaffCostRow] = []
        for (user_id, start, end, hours_id), t in tallies.items():
            person = people[user_id]
            hours: StaffHoursBasis | None = by_id.get(hours_id) if hours_id else None
            rows.append(
                StaffCostRow(
                    user_id=user_id,
                    name=person.name,
                    staff_code=person.staff_code,
                    grade_name=person.grade_name,
                    basis=cast(Literal["weekly", "monthly"], hours.basis) if hours else None,
                    period_start=start,
                    period_end=end,
                    standard_hours_minutes=hours.standard_minutes if hours else None,
                    **t.figures(),
                )
            )
        rows.sort(key=lambda r: (r.name.lower(), str(r.user_id), r.period_start))
        totals = _Tally()
        for t in tallies.values():
            for name in t.figures():
                if name != "total_cost":
                    setattr(totals, name, getattr(totals, name) + getattr(t, name))
        page_rows, page = window(rows, q)
        return StaffCostReport(
            rows=page_rows, totals=StaffCostFigures(**totals.figures()), filters=filters, page=page
        )

    async def jobs(self, ctx: OrgContext, q: StaffCostJobsQuery) -> StaffCostJobsReport:
        """The jobs worked in overtime: one row per job and status."""
        scope, filters = await self._scope(ctx, q)
        org_id = ctx.organization_id
        pieces = [p for p in await self.repo.overtime(org_id, scope) if self._matches(p, q)]
        groups: dict[tuple[Any, ...], list[OvertimePiece]] = defaultdict(list)
        for p in pieces:
            groups[(p.job_id, p.status)].append(p)
        rows: list[StaffCostJobRow] = []
        for (job_id, status), found in groups.items():
            first = found[0]
            minutes = sum(p.minutes for p in found)
            cost = sum(p.overtime_cost for p in found)
            rates = {p.overtime_rate for p in found}
            rows.append(
                StaffCostJobRow(
                    job=OvertimeJobRef(id=job_id, number=first.job_number, name=first.job_name)
                    if job_id is not None
                    else None,
                    client=OvertimeClientRef(id=first.client_id, name=first.client_name)
                    if first.client_id is not None
                    else None,
                    allocated=first.allocated,
                    overtime=True,
                    overtime_minutes=minutes,
                    overtime_rate=rates.pop()
                    if len(rates) == 1
                    else (cost * 60 * 2 + minutes) // (2 * minutes),
                    overtime_cost=cost,
                    status=cast(OvertimeStatus, status),
                    entry_count=len(found),
                )
            )
        rows.sort(
            key=lambda r: (
                (r.client_name or "").lower(),
                r.job_number or "",
                _STATUS_ORDER.index(r.status),
            )
        )
        counted = [r for r in rows if r.status != "rejected"]
        totals = StaffCostJobTotals(
            overtime_minutes=sum(r.overtime_minutes for r in counted),
            overtime_cost=sum(r.overtime_cost for r in counted),
        )
        page_rows, page = window(rows, q)
        return StaffCostJobsReport(rows=page_rows, totals=totals, filters=filters, page=page)
