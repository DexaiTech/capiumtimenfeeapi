"""Time entries: logging, editing, the weekly grid and re-pricing (docs/domain.md, "Time").

Locking: every write takes the person's timesheet row(s) ``FOR UPDATE`` (in id order) before
it touches an entry, then the entry row. That serialises the 24-hour daily cap, the
timesheet status checks (a sheet cannot be submitted or approved halfway through an edit)
and the entry's billing status (billing locks entries before allocating them).
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import ColumnElement
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import AuthorizationError, ConflictError, NotFoundError, ValidationError
from app.db.models import ActivityCode, Client, Job, Organization, TimeEntry, Timesheet
from app.db.session import unit_of_work
from app.repositories.time_entries import (
    EntryFilters,
    EntryView,
    TimeEntryRepository,
    wip_time_clause,
)
from app.repositories.time_jobs import ClientJobRepository
from app.repositories.time_refs import TimeReferenceRepository
from app.repositories.timesheets import TimesheetRepository
from app.schemas.time import (
    TimeEntryBulkCreate,
    TimeEntryCreate,
    TimeEntryRead,
    TimeEntryUpdate,
    WeekGrid,
    WeekGridRow,
)
from app.services import audit, charge_rates, period_locks
from app.services.charge_rates import RateQuery
from app.services.job_budget_alerts import refresh_job_budgets
from app.services.policy import OrgContext, has_permission
from app.services.time_rules import (
    EDITABLE_SHEET_STATUSES,
    MAX_DAY_MINUTES,
    DayLimitError,
    TimeLockedError,
    ensure_client_accepts_work,
    ensure_job_open,
    practice_today,
    round_minutes,
    week_start_for,
)
from app.services.time_tasks import load_tasks, task_ref
from app.services.time_visibility import can_see_note
from app.services.time_week import with_targets
from app.utils.money import time_value
from app.utils.pagination import Page, PageParams

# What a job's budget usage is made of: a change to any of these re-evaluates its budget alerts.
BUDGET_FIELDS = frozenset({"minutes", "job_id", "charge_value", "is_billable"})


def wip_eligibility_clause(organization: Organization) -> ColumnElement[bool]:
    """Which time entries are billable WIP for this practice.

    Billable and ``unbilled``; when ``require_timesheet_approval`` is on, also on an
    ``approved`` timesheet. A SQL condition over ``time_entries`` (no join needed), for the
    WIP, billing and report queries.
    """
    return wip_time_clause(organization.require_timesheet_approval)


def show_cost(ctx: OrgContext) -> bool:
    return has_permission(ctx, "rate.read")


def default_billable(job: Job | None, activity: ActivityCode | None, has_client: bool) -> bool:
    if not has_client:
        return False
    if job is not None and job.billing_basis == "non_billable":
        return False
    return not (activity is not None and not activity.is_chargeable)


@dataclass(slots=True)
class _Draft:
    """A validated entry about to be written."""

    user_id: uuid.UUID
    entry_date: date
    minutes: int
    client_id: uuid.UUID | None
    job_id: uuid.UUID | None
    activity_code_id: uuid.UUID | None
    description: str | None
    is_billable: bool
    field_prefix: str = ""
    names: dict[str, Any] = field(default_factory=dict)
    internal_note: str | None = None
    task_id: uuid.UUID | None = None


def _f(prefix: str, name: str) -> str:
    return f"{prefix}{name}"


class TimeEntryService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.entries = TimeEntryRepository(session)
        self.sheets = TimesheetRepository(session)
        self.jobs = ClientJobRepository(session)
        self.refs = TimeReferenceRepository(session)

    # --- Access --------------------------------------------------------------------------

    def ensure_can_write(self, ctx: OrgContext, user_id: uuid.UUID) -> None:
        """Own time with ``time.log``; anyone else's with ``time.manage_all``."""
        code = "time.log" if user_id == ctx.user.id else "time.manage_all"
        if not has_permission(ctx, code):
            raise AuthorizationError()

    def ensure_can_read(self, ctx: OrgContext, user_id: uuid.UUID) -> None:
        if user_id != ctx.user.id and not has_permission(ctx, "time.read_all"):
            raise AuthorizationError()

    async def ensure_can_view_person(self, ctx: OrgContext, user_id: uuid.UUID) -> None:
        """Own, ``time.read_all``, or the person's manager (timesheets and the week grid)."""
        if user_id == ctx.user.id or has_permission(ctx, "time.read_all"):
            return
        member = await self.refs.get_member(ctx.organization_id, user_id)
        if member is None or member.manager_user_id != ctx.user.id:
            raise AuthorizationError()

    # --- Reads ---------------------------------------------------------------------------

    async def list_entries(
        self,
        ctx: OrgContext,
        *,
        user_id: str | None,
        date_from: date | None,
        date_to: date | None,
        client_id: uuid.UUID | None,
        job_id: uuid.UUID | None,
        billable: bool | None,
        billing_statuses: Sequence[str] | None,
        timesheet_statuses: Sequence[str] | None,
        params: PageParams,
    ) -> Page[TimeEntryRead]:
        user_ids: list[uuid.UUID] | None
        if user_id in (None, "me"):
            user_ids = [ctx.user.id]
        elif user_id == "all":
            if not has_permission(ctx, "time.read_all"):
                raise AuthorizationError()
            user_ids = None
        else:
            assert user_id is not None
            try:
                target = uuid.UUID(user_id)
            except ValueError as exc:
                raise ValidationError.for_field("user_id", "Use a user id, 'me' or 'all'.") from exc
            self.ensure_can_read(ctx, target)
            user_ids = [target]
        page = await self.entries.list_page(
            ctx.organization_id,
            EntryFilters(
                user_ids=user_ids,
                date_from=date_from,
                date_to=date_to,
                client_id=client_id,
                job_id=job_id,
                billable=billable,
                billing_statuses=billing_statuses,
                timesheet_statuses=timesheet_statuses,
            ),
            params,
        )
        cost = show_cost(ctx)
        return Page(
            items=[
                TimeEntryRead.from_view(
                    v, show_cost=cost, show_note=can_see_note(ctx, v.entry.user_id)
                )
                for v in page.items
            ],
            next_cursor=page.next_cursor,
        )

    async def get(self, ctx: OrgContext, entry_id: uuid.UUID) -> TimeEntryRead:
        views = await self.entries.views(ctx.organization_id, [entry_id])
        if not views:
            raise NotFoundError("Time entry not found.")
        self.ensure_can_read(ctx, views[0].entry.user_id)
        return TimeEntryRead.from_view(
            views[0],
            show_cost=show_cost(ctx),
            show_note=can_see_note(ctx, views[0].entry.user_id),
        )

    async def week(
        self, ctx: OrgContext, *, week_start: date | None, user_id: str | None
    ) -> WeekGrid:
        org = ctx.organization
        target = ctx.user.id
        if user_id not in (None, "me"):
            assert user_id is not None
            try:
                target = uuid.UUID(user_id)
            except ValueError as exc:
                raise ValidationError.for_field("user_id", "Use a user id or 'me'.") from exc
        await self.ensure_can_view_person(ctx, target)
        start = week_start_for(org, week_start or practice_today(org))
        days = [start + timedelta(days=i) for i in range(7)]
        views = await self.entries.range_views(ctx.organization_id, target, days[0], days[-1])
        sheet = await self.sheets.find(ctx.organization_id, target, start)
        return await with_targets(self.session, ctx, self._grid(target, days, sheet, views))

    @staticmethod
    def _grid(
        user_id: uuid.UUID, days: list[date], sheet: Timesheet | None, views: list[EntryView]
    ) -> WeekGrid:
        rows: dict[tuple[Any, ...], WeekGridRow] = {}
        day_totals = [0] * 7
        chargeable = 0
        for view in views:
            e = view.entry
            key = (e.client_id, e.job_id, e.task_id, e.activity_code_id, e.is_billable)
            row = rows.get(key)
            if row is None:
                row = rows[key] = WeekGridRow(
                    client_id=e.client_id,
                    client_code=view.client_code,
                    client_name=view.client_name,
                    job_id=e.job_id,
                    job_number=view.job_number,
                    job_name=view.job_name,
                    task_id=e.task_id,
                    task_name=view.task_name,
                    activity_code_id=e.activity_code_id,
                    activity_code=view.activity_code,
                    activity_name=view.activity_name,
                    is_billable=e.is_billable,
                    minutes=[0] * 7,
                    total_minutes=0,
                    entry_ids=[],
                )
            index = (e.entry_date - days[0]).days
            row.minutes[index] += e.minutes
            row.total_minutes += e.minutes
            row.entry_ids.append(e.id)
            day_totals[index] += e.minutes
            if e.is_billable:
                chargeable += e.minutes
        ordered = sorted(
            rows.values(),
            key=lambda r: (
                r.client_name is None,
                (r.client_name or "").lower(),
                r.job_number or "",
                (r.task_name or "").lower(),
                r.activity_code or "",
                not r.is_billable,
            ),
        )
        total = sum(day_totals)
        return WeekGrid(
            user_id=user_id,
            week_start=days[0],
            week_end=days[-1],
            days=days,
            timesheet_id=sheet.id if sheet else None,
            timesheet_status=sheet.status if sheet else "open",
            rows=ordered,
            day_totals=day_totals,
            total_minutes=total,
            chargeable_minutes=chargeable,
            non_chargeable_minutes=total - chargeable,
        )

    # --- Writes --------------------------------------------------------------------------

    async def create(
        self,
        ctx: OrgContext,
        data: TimeEntryCreate,
        rctx: RequestContext,
        *,
        source: str = "manual",
    ) -> TimeEntryRead:
        async with unit_of_work(self.session):
            [entry_id] = await self._create_many(
                ctx, [data], rctx, prefix_items=False, source=source
            )
            [view] = await self.entries.views(ctx.organization_id, [entry_id])
        return TimeEntryRead.from_view(
            view, show_cost=show_cost(ctx), show_note=can_see_note(ctx, view.entry.user_id)
        )

    async def bulk_create(
        self, ctx: OrgContext, data: TimeEntryBulkCreate, rctx: RequestContext
    ) -> list[TimeEntryRead]:
        """All or nothing: one invalid entry rolls the whole batch back."""
        async with unit_of_work(self.session):
            ids = await self._create_many(ctx, data.entries, rctx, prefix_items=True, source="bulk")
            views = await self.entries.views(ctx.organization_id, ids)
        cost = show_cost(ctx)
        return [
            TimeEntryRead.from_view(v, show_cost=cost, show_note=can_see_note(ctx, v.entry.user_id))
            for v in views
        ]

    async def _create_many(
        self,
        ctx: OrgContext,
        items: Sequence[TimeEntryCreate],
        rctx: RequestContext,
        *,
        prefix_items: bool,
        source: str,
    ) -> list[uuid.UUID]:
        org = ctx.organization
        org_id = org.id
        prefixes = [f"entries.{i}." if prefix_items else "" for i in range(len(items))]

        # Who: own time needs time.log, anyone else's time.manage_all and an active member.
        owners = [item.user_id or ctx.user.id for item in items]
        for owner in set(owners):
            self.ensure_can_write(ctx, owner)
        others = {o for o in owners if o != ctx.user.id}
        active = await self.refs.active_member_ids(org_id, others)
        for owner, prefix in zip(owners, prefixes, strict=True):
            if owner != ctx.user.id and owner not in active:
                raise ValidationError.for_field(
                    _f(prefix, "user_id"), "This person is not an active member of the practice."
                )

        # FOR KEY SHARE: a job closed or deleted at the same moment is seen (or waits).
        jobs = await self.jobs.get_many(
            org_id, {i.job_id for i in items if i.job_id}, key_share=True
        )
        clients = await self.refs.get_clients(
            org_id,
            {i.client_id for i in items if i.client_id} | {j.client_id for j in jobs.values()},
        )
        activities = await self.refs.get_activity_codes(
            org_id, {i.activity_code_id for i in items if i.activity_code_id}
        )
        allowed = await self.refs.allowed_activities(
            org_id, {j.service_id for j in jobs.values() if j.service_id}
        )
        leaving = await self.refs.leaving_dates(org_id, set(owners))
        tasks = await load_tasks(self.session, org_id, (i.task_id for i in items))

        drafts: list[_Draft] = []
        for item, owner, prefix in zip(items, owners, prefixes, strict=True):
            job = self.job_ref(jobs, item.job_id, prefix)
            task = task_ref(tasks, item.task_id, job, _f(prefix, "task_id"))
            client = self.client_ref(clients, item.client_id, job, prefix)
            activity = self.activity_ref(activities, item.activity_code_id, prefix, check=True)
            self.ensure_activity_allowed(allowed, job, activity, prefix)
            self.ensure_before_leaving(leaving.get(owner), item.entry_date, prefix)
            if client is not None:
                await ensure_client_accepts_work(self.session, client)
            if job is not None:
                ensure_job_open(job)
            billable = self.resolve_billable(item.is_billable, job, activity, client, prefix)
            drafts.append(
                _Draft(
                    user_id=owner,
                    entry_date=item.entry_date,
                    minutes=round_minutes(org, item.minutes),
                    client_id=client.id if client else None,
                    job_id=job.id if job else None,
                    activity_code_id=activity.id if activity else None,
                    description=item.description,
                    is_billable=billable,
                    field_prefix=prefix,
                    names={
                        "client_name": client.name if client else None,
                        "job_number": job.number if job else None,
                        **({"task_name": task.title} if task else {}),
                    },
                    internal_note=item.internal_note,
                    task_id=task.id if task else None,
                )
            )

        await period_locks.assert_open_many(
            self.session, org_id, {d.entry_date for d in drafts}, "time"
        )

        # Lock the sheets, then check their status and the daily cap under the lock.
        weeks = {(d.user_id, week_start_for(org, d.entry_date)) for d in drafts}
        sheets = await self.sheets.lock_weeks(org_id, weeks)
        for draft in drafts:
            sheet = sheets[(draft.user_id, week_start_for(org, draft.entry_date))]
            self._ensure_sheet_editable(sheet)
        for owner in {d.user_id for d in drafts}:
            mine = [d for d in drafts if d.user_id == owner]
            logged = await self.entries.day_minutes(org_id, owner, {d.entry_date for d in mine})
            for draft in mine:
                logged[draft.entry_date] = logged.get(draft.entry_date, 0) + draft.minutes
                self._ensure_day_cap(logged[draft.entry_date], draft.entry_date, draft.field_prefix)

        rates = await charge_rates.resolve_many(
            self.session,
            org,
            [RateQuery(d.user_id, d.client_id, d.job_id, d.entry_date) for d in drafts],
        )
        ids: list[uuid.UUID] = []
        for draft, rate in zip(drafts, rates, strict=True):
            sheet = sheets[(draft.user_id, week_start_for(org, draft.entry_date))]
            entry = TimeEntry(
                organization_id=org_id,
                user_id=draft.user_id,
                timesheet_id=sheet.id,
                entry_date=draft.entry_date,
                minutes=draft.minutes,
                client_id=draft.client_id,
                job_id=draft.job_id,
                task_id=draft.task_id,
                activity_code_id=draft.activity_code_id,
                description=draft.description,
                internal_note=draft.internal_note,
                is_billable=draft.is_billable,
                charge_rate=rate.charge_rate,
                cost_rate=rate.cost_rate,
                charge_value=time_value(draft.minutes, rate.charge_rate),
                cost_value=time_value(draft.minutes, rate.cost_rate),
                rate_source=rate.source,
                created_by=ctx.user.id,
            )
            await self.entries.add(entry)
            ids.append(entry.id)
            await self._audit(
                ctx,
                rctx,
                "time_entry.created",
                entry,
                {**draft.names, "source": source, "rate_source": rate.source},
            )
        await refresh_job_budgets(
            self.session,
            org_id,
            [draft.job_id for draft in drafts],
            actor_user_id=ctx.user.id,
            rctx=rctx,
        )
        return ids

    async def update(
        self, ctx: OrgContext, entry_id: uuid.UUID, data: TimeEntryUpdate, rctx: RequestContext
    ) -> TimeEntryRead:
        org = ctx.organization
        org_id = org.id
        fields = data.model_fields_set
        async with unit_of_work(self.session):
            current = await self.entries.get(org_id, entry_id)
            if current is None:
                raise NotFoundError("Time entry not found.")
            self.ensure_can_write(ctx, current.user_id)

            new_date = (
                data.entry_date
                if "entry_date" in fields and data.entry_date
                else current.entry_date
            )
            # The entry's own sheet is locked by id (not re-derived from its date: the
            # practice may have changed its week_start since). The entry only moves to
            # another sheet when the new date falls outside its sheet's week.
            current_sheet = await self.sheets.get(org_id, current.timesheet_id)
            if current_sheet is None:
                raise ConflictError("This entry was changed at the same time. Try again.")
            target_week: tuple[uuid.UUID, date] | None = None
            if not _in_week(current_sheet, new_date):
                target_week = (current.user_id, week_start_for(org, new_date))
            sheets = await self.sheets.lock_for_write(
                org_id, [target_week] if target_week else [], [current.timesheet_id]
            )
            entry = await self.entries.get(org_id, entry_id, for_update=True)
            if entry is None:
                raise NotFoundError("Time entry not found.")
            if entry.timesheet_id != current.timesheet_id:
                raise ConflictError("This entry was changed at the same time. Try again.")
            old_sheet = sheets[entry.timesheet_id]
            await period_locks.assert_open_many(
                self.session, org_id, {entry.entry_date, new_date}, "time"
            )
            self._ensure_entry_editable(entry, old_sheet)
            new_sheet = old_sheet
            if target_week is not None:
                new_sheet = next(
                    s for s in sheets.values() if (s.user_id, s.week_start) == target_week
                )
            self._ensure_sheet_editable(new_sheet)

            # References: a job implies its client; clearing the client clears nothing else.
            job_id = data.job_id if "job_id" in fields else entry.job_id
            client_id = data.client_id if "client_id" in fields else entry.client_id
            if "job_id" in fields and data.job_id is not None and "client_id" not in fields:
                client_id = None  # taken from the job below
            activity_id = (
                data.activity_code_id if "activity_code_id" in fields else entry.activity_code_id
            )
            jobs = await self.jobs.get_many(org_id, {job_id} if job_id else set(), key_share=True)
            job = self.job_ref(jobs, job_id, "")
            if job is None and job_id is not None:
                raise NotFoundError("Job not found.", details={"field": "job_id"})
            client_ids = {c for c in (client_id, job.client_id if job else None) if c}
            clients = await self.refs.get_clients(org_id, client_ids)
            client = self.client_ref(clients, client_id, job, "")
            activities = await self.refs.get_activity_codes(
                org_id, {activity_id} if activity_id else set()
            )
            activity = self.activity_ref(
                activities, activity_id, "", check=activity_id != entry.activity_code_id
            )
            # A task belongs to one job: moving the entry to another job drops its task unless a
            # task of the new job is sent.
            task_id = data.task_id if "task_id" in fields else entry.task_id
            if "task_id" not in fields and (job.id if job else None) != entry.job_id:
                task_id = None
            tasks = await load_tasks(self.session, org_id, [task_id])
            task = task_ref(tasks, task_id, job, "task_id")
            task_changed = (task.id if task else None) != entry.task_id

            new_minutes = (
                round_minutes(org, data.minutes)
                if "minutes" in fields and data.minutes is not None
                else entry.minutes
            )
            refs_changed = (
                (client.id if client else None) != entry.client_id
                or (job.id if job else None) != entry.job_id
                or (activity.id if activity else None) != entry.activity_code_id
            )
            time_changed = new_minutes != entry.minutes or new_date != entry.entry_date
            if refs_changed:
                allowed = await self.refs.allowed_activities(
                    org_id, {job.service_id} if job and job.service_id else set()
                )
                self.ensure_activity_allowed(allowed, job, activity, "")
            if new_date != entry.entry_date:
                leaving = await self.refs.leaving_dates(org_id, {entry.user_id})
                self.ensure_before_leaving(leaving.get(entry.user_id), new_date, "")
            if refs_changed or time_changed or (task_changed and task is not None):
                if client is not None:
                    await ensure_client_accepts_work(self.session, client)
                if job is not None:
                    ensure_job_open(job)

            if "is_billable" in fields or refs_changed:
                billable = self.resolve_billable(data.is_billable, job, activity, client, "")
            else:
                billable = entry.is_billable
                if billable and client is None:
                    billable = False

            if time_changed:
                logged = await self.entries.day_minutes(
                    org_id, entry.user_id, {new_date}, exclude_ids={entry.id}
                )
                self._ensure_day_cap(logged.get(new_date, 0) + new_minutes, new_date, "")

            [rate] = await charge_rates.resolve_many(
                self.session,
                org,
                [
                    RateQuery(
                        entry.user_id,
                        client.id if client else None,
                        job.id if job else None,
                        new_date,
                    )
                ],
            )
            new_values: dict[str, Any] = {
                "entry_date": new_date,
                "minutes": new_minutes,
                "client_id": client.id if client else None,
                "job_id": job.id if job else None,
                "task_id": task.id if task else None,
                "activity_code_id": activity.id if activity else None,
                "description": data.description if "description" in fields else entry.description,
                "is_billable": billable,
                "timesheet_id": new_sheet.id,
                "charge_rate": rate.charge_rate,
                "cost_rate": rate.cost_rate,
                "charge_value": time_value(new_minutes, rate.charge_rate),
                "cost_value": time_value(new_minutes, rate.cost_rate),
                "rate_source": rate.source,
            }
            changes: dict[str, list[Any]] = {}
            old_job_id = entry.job_id
            for name, value in new_values.items():
                old = getattr(entry, name)
                if old != value:
                    changes[name] = [_json(old), _json(value)]
                    setattr(entry, name, value)
            if "internal_note" in fields and data.internal_note != entry.internal_note:
                # The audit trail says that a note changed, never what it says.
                changes["internal_note"] = [
                    entry.internal_note is not None,
                    data.internal_note is not None,
                ]
                entry.internal_note = data.internal_note
            if changes:
                await self.session.flush()
                await self._audit(
                    ctx,
                    rctx,
                    "time_entry.updated",
                    entry,
                    {"client_name": client.name if client else None, "changes": changes},
                )
                if changes.keys() & BUDGET_FIELDS:
                    await refresh_job_budgets(
                        self.session,
                        org_id,
                        [old_job_id, entry.job_id],
                        actor_user_id=ctx.user.id,
                        rctx=rctx,
                    )
            [view] = await self.entries.views(org_id, [entry.id])
        return TimeEntryRead.from_view(
            view, show_cost=show_cost(ctx), show_note=can_see_note(ctx, view.entry.user_id)
        )

    async def delete(self, ctx: OrgContext, entry_id: uuid.UUID, rctx: RequestContext) -> None:
        org = ctx.organization
        async with unit_of_work(self.session):
            current = await self.entries.get(org.id, entry_id)
            if current is None:
                raise NotFoundError("Time entry not found.")
            self.ensure_can_write(ctx, current.user_id)
            # Lock the entry's own sheet by id (see update()).
            [sheet] = await self.sheets.lock_many(org.id, [current.timesheet_id])
            entry = await self.entries.get(org.id, entry_id, for_update=True)
            if entry is None:
                raise NotFoundError("Time entry not found.")
            if entry.timesheet_id != sheet.id:
                raise ConflictError("This entry was changed at the same time. Try again.")
            await period_locks.assert_open(self.session, org.id, entry.entry_date, "time")
            self._ensure_entry_editable(entry, sheet)
            await self._audit(ctx, rctx, "time_entry.deleted", entry, {})
            job_id = entry.job_id
            await self.entries.delete(entry)
            await refresh_job_budgets(
                self.session, org.id, [job_id], actor_user_id=ctx.user.id, rctx=rctx
            )

    # --- Rule helpers --------------------------------------------------------------------

    @staticmethod
    def job_ref(jobs: dict[uuid.UUID, Job], job_id: uuid.UUID | None, prefix: str) -> Job | None:
        if job_id is None:
            return None
        job = jobs.get(job_id)
        if job is None:
            raise NotFoundError("Job not found.", details={"field": _f(prefix, "job_id")})
        return job

    @staticmethod
    def client_ref(
        clients: dict[uuid.UUID, Client],
        client_id: uuid.UUID | None,
        job: Job | None,
        prefix: str,
    ) -> Client | None:
        if job is not None:
            if client_id is not None and client_id != job.client_id:
                raise ValidationError.for_field(
                    _f(prefix, "job_id"), "This job belongs to another client."
                )
            client_id = job.client_id
        if client_id is None:
            return None
        client = clients.get(client_id)
        if client is None:
            raise NotFoundError("Client not found.", details={"field": _f(prefix, "client_id")})
        return client

    @staticmethod
    def activity_ref(
        activities: dict[uuid.UUID, ActivityCode],
        activity_id: uuid.UUID | None,
        prefix: str,
        *,
        check: bool,
    ) -> ActivityCode | None:
        if activity_id is None:
            return None
        activity = activities.get(activity_id)
        if activity is None:
            raise NotFoundError(
                "Activity code not found.", details={"field": _f(prefix, "activity_code_id")}
            )
        if check and not activity.is_active:
            raise ValidationError.for_field(
                _f(prefix, "activity_code_id"), "This activity code is inactive."
            )
        return activity

    @staticmethod
    def ensure_activity_allowed(
        allowed: dict[uuid.UUID, set[uuid.UUID]],
        job: Job | None,
        activity: ActivityCode | None,
        prefix: str,
    ) -> None:
        """A service can limit the activity codes of its jobs' time (none listed = any)."""
        if job is None or activity is None or job.service_id is None:
            return
        permitted = allowed.get(job.service_id)
        if permitted and activity.id not in permitted:
            raise ValidationError.for_field(
                _f(prefix, "activity_code_id"),
                "This activity code is not available for the job's service.",
                "activity_not_allowed",
            )

    @staticmethod
    def ensure_before_leaving(leaving: date | None, day: date, prefix: str) -> None:
        if leaving is not None and day > leaving:
            raise ValidationError.for_field(
                _f(prefix, "entry_date"),
                f"This person left the practice on {leaving.isoformat()}; "
                "time cannot be logged after that day.",
                "after_leaving_date",
            )

    @staticmethod
    def resolve_billable(
        requested: bool | None,
        job: Job | None,
        activity: ActivityCode | None,
        client: Client | None,
        prefix: str,
    ) -> bool:
        if requested is None:
            return default_billable(job, activity, client is not None)
        if requested and client is None:
            raise ValidationError.for_field(
                _f(prefix, "is_billable"), "Billable time needs a client."
            )
        if requested and job is not None and job.billing_basis == "non_billable":
            raise ValidationError.for_field(
                _f(prefix, "is_billable"), "Time on a non-billable job cannot be billable."
            )
        return requested

    @staticmethod
    def _ensure_sheet_editable(sheet: Timesheet) -> None:
        if sheet.status not in EDITABLE_SHEET_STATUSES:
            raise TimeLockedError(
                f"The timesheet for the week of {sheet.week_start.isoformat()} is "
                f"{sheet.status}; its time cannot be changed.",
                details={"timesheet_id": str(sheet.id), "timesheet_status": sheet.status},
            )

    def _ensure_entry_editable(self, entry: TimeEntry, sheet: Timesheet) -> None:
        if entry.billing_status != "unbilled":
            raise TimeLockedError(
                "This time has been billed or written off and cannot be changed.",
                details={"billing_status": entry.billing_status},
            )
        self._ensure_sheet_editable(sheet)

    @staticmethod
    def _ensure_day_cap(total: int, day: date, prefix: str) -> None:
        if total > MAX_DAY_MINUTES:
            raise DayLimitError(
                details=[
                    {
                        "field": _f(prefix, "minutes"),
                        "code": "day_limit",
                        "message": f"This would make {total} minutes on {day.isoformat()}; "
                        f"the most is {MAX_DAY_MINUTES}.",
                    }
                ]
            )

    async def _audit(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        action: str,
        entry: TimeEntry,
        extra: dict[str, Any],
    ) -> None:
        await audit.record(
            self.session,
            action,
            ctx=rctx,
            actor_user_id=ctx.user.id,
            organization_id=ctx.organization_id,
            target_type="time_entry",
            target_id=entry.id,
            meta={
                "user_id": str(entry.user_id),
                "entry_date": entry.entry_date.isoformat(),
                "minutes": entry.minutes,
                "client_id": str(entry.client_id) if entry.client_id else None,
                **({"task_id": str(entry.task_id)} if entry.task_id else {}),
                "job_id": str(entry.job_id) if entry.job_id else None,
                "is_billable": entry.is_billable,
                "charge_rate": entry.charge_rate,
                "charge_value": entry.charge_value,
                **extra,
            },
        )


def _in_week(sheet: Timesheet, day: date) -> bool:
    return sheet.week_start <= day <= sheet.week_start + timedelta(days=6)


def _json(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    return value
