"""Corrections of approved, unbilled time (docs/modules/time.md, "Corrections").

An approved timesheet is locked, so a mistake in it cannot be edited. An approver corrects the
entry instead: the change must carry a reason, follows the rules of an edit, and leaves one
row in ``time_entry_history`` (the entry before and after, append-only) and an audit entry.
"""

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import AuthorizationError, ConflictError, NotFoundError, ValidationError
from app.db.models import ActivityCode, Client, Job, TimeEntry, TimeEntryHistory, Timesheet
from app.db.session import unit_of_work
from app.repositories.time_entries import EntryView
from app.repositories.time_history import TimeHistoryRepository
from app.schemas.time import TimeEntryRead
from app.schemas.time_views import TimeCorrection, TimeEntryHistoryRead, TimeRemoval
from app.services import charge_rates, period_locks
from app.services.charge_rates import RateQuery
from app.services.job_budget_alerts import refresh_job_budgets
from app.services.policy import OrgContext, ensure_any, has_permission
from app.services.time_entries import BUDGET_FIELDS, TimeEntryService, show_cost
from app.services.time_rules import (
    ensure_client_accepts_work,
    ensure_job_open,
    round_minutes,
    week_start_for,
)
from app.services.time_visibility import can_see_note
from app.utils.money import time_value
from app.utils.pagination import Page, PageParams

# The fields a snapshot keeps; the names beside them say what the ids were called then.
SNAPSHOT_FIELDS = (
    "entry_date",
    "minutes",
    "client_id",
    "job_id",
    "task_id",
    "activity_code_id",
    "description",
    "internal_note",
    "is_billable",
    "charge_rate",
    "cost_rate",
    "charge_value",
    "cost_value",
    "rate_source",
    "timesheet_id",
)
COST_FIELDS = ("cost_rate", "cost_value")


class TimeNotCorrectableError(ConflictError):
    code = "TIME_NOT_CORRECTABLE"
    message = "Only approved time that has not been billed can be corrected."


class TimeCorrectionReasonRequiredError(ValidationError):
    code = "TIME_CORRECTION_REASON_REQUIRED"
    message = "A correction of approved time needs a reason."


class TimeCorrectionEmptyError(ValidationError):
    code = "TIME_CORRECTION_EMPTY"
    message = "The correction changes nothing: change at least one field of the entry."


class SelfCorrectionError(AuthorizationError):
    code = "SELF_CORRECTION_FORBIDDEN"
    message = "You cannot correct your own approved time."


@dataclass(frozen=True, slots=True)
class _Names:
    client_name: str | None
    job_number: str | None
    job_name: str | None
    activity_code: str | None


def _json(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


def snapshot(entry: TimeEntry, week_start: date, names: _Names) -> dict[str, Any]:
    """The entry as a JSON object: its fields, the week of its timesheet and the names then."""
    values = {name: _json(getattr(entry, name)) for name in SNAPSHOT_FIELDS}
    values.update(
        week_start=week_start.isoformat(),
        client_name=names.client_name,
        job_number=names.job_number,
        job_name=names.job_name,
        activity_code=names.activity_code,
    )
    return values


def _names_of(view: EntryView) -> _Names:
    return _Names(view.client_name, view.job_number, view.job_name, view.activity_code)


def _in_week(sheet: Timesheet, day: date) -> bool:
    return sheet.week_start <= day <= sheet.week_start + timedelta(days=6)


class TimeCorrectionService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.time = TimeEntryService(session)
        self.entries = self.time.entries
        self.sheets = self.time.sheets
        self.jobs = self.time.jobs
        self.refs = self.time.refs
        self.history = TimeHistoryRepository(session)

    async def remove(
        self, ctx: OrgContext, entry_id: uuid.UUID, data: TimeRemoval, rctx: RequestContext
    ) -> None:
        """Delete approved, unbilled time that was posted by mistake, saying why.

        The same people and rules as a correction; the reason is kept in the audit entry
        ``time_entry.deleted`` (the entry itself, and its history, go).
        """
        ensure_any(ctx, "time.manage_all", "timesheet.approve")
        reason = (data.reason or "").strip()
        if not reason:
            raise TimeCorrectionReasonRequiredError(
                "Say why the approved time is being removed.",
                details=[
                    {
                        "field": "reason",
                        "code": "required",
                        "message": "Say why the approved time is being removed.",
                    }
                ],
            )
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            current = await self.entries.get(org_id, entry_id)
            if current is None:
                raise NotFoundError("Time entry not found.")
            if current.user_id == ctx.user.id and not has_permission(ctx, "organization.update"):
                raise SelfCorrectionError("You cannot remove your own approved time.")
            [sheet] = await self.sheets.lock_many(org_id, [current.timesheet_id])
            entry = await self.entries.get(org_id, entry_id, for_update=True)
            if entry is None:
                raise NotFoundError("Time entry not found.")
            if entry.timesheet_id != sheet.id:
                raise ConflictError("This entry was changed at the same time. Try again.")
            if sheet.status != "approved" or entry.billing_status != "unbilled":
                raise TimeNotCorrectableError(
                    "Only approved time that has not been billed can be removed this way.",
                    details={
                        "timesheet_status": sheet.status,
                        "billing_status": entry.billing_status,
                    },
                )
            await period_locks.assert_open(self.session, org_id, entry.entry_date, "time")
            await self.time._audit(
                ctx, rctx, "time_entry.deleted", entry, {"reason": reason, "was_approved": True}
            )
            job_id = entry.job_id
            await self.entries.delete(entry)
            await refresh_job_budgets(
                self.session, org_id, [job_id], actor_user_id=ctx.user.id, rctx=rctx
            )

    async def correct(
        self, ctx: OrgContext, entry_id: uuid.UUID, data: TimeCorrection, rctx: RequestContext
    ) -> TimeEntryRead:
        ensure_any(ctx, "time.manage_all", "timesheet.approve")
        reason = (data.reason or "").strip()
        if not reason:
            raise TimeCorrectionReasonRequiredError(
                details=[
                    {
                        "field": "reason",
                        "code": "required",
                        "message": "Say why the approved time is being corrected.",
                    }
                ]
            )
        fields = data.model_fields_set - {"reason"}
        if not fields:
            raise TimeCorrectionEmptyError()
        org = ctx.organization
        org_id = org.id
        async with unit_of_work(self.session):
            current = await self.entries.get(org_id, entry_id)
            if current is None:
                raise NotFoundError("Time entry not found.")
            if current.user_id == ctx.user.id and not has_permission(ctx, "organization.update"):
                raise SelfCorrectionError()

            new_date = current.entry_date
            if "entry_date" in fields and data.entry_date is not None:
                new_date = data.entry_date
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
            if old_sheet.status != "approved" or entry.billing_status != "unbilled":
                raise TimeNotCorrectableError(
                    details={
                        "timesheet_status": old_sheet.status,
                        "billing_status": entry.billing_status,
                    }
                )
            new_sheet = old_sheet
            if target_week is not None:
                new_sheet = next(
                    s for s in sheets.values() if (s.user_id, s.week_start) == target_week
                )
            await period_locks.assert_open_many(
                self.session, org_id, {entry.entry_date, new_date}, "time"
            )
            [before_view] = await self.entries.views(org_id, [entry.id])

            # References, as in an edit: a job implies its client.
            job_id = data.job_id if "job_id" in fields else entry.job_id
            client_id = data.client_id if "client_id" in fields else entry.client_id
            if "job_id" in fields and data.job_id is not None and "client_id" not in fields:
                client_id = None  # taken from the job below
            activity_id = (
                data.activity_code_id if "activity_code_id" in fields else entry.activity_code_id
            )
            jobs = await self.jobs.get_many(org_id, {job_id} if job_id else set(), key_share=True)
            job = self.time.job_ref(jobs, job_id, "")
            client_ids = {c for c in (client_id, job.client_id if job else None) if c}
            client = self.time.client_ref(
                await self.refs.get_clients(org_id, client_ids), client_id, job, ""
            )
            activity = self.time.activity_ref(
                await self.refs.get_activity_codes(org_id, {activity_id} if activity_id else set()),
                activity_id,
                "",
                check=activity_id != entry.activity_code_id,
            )

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
                self.time.ensure_activity_allowed(allowed, job, activity, "")
            if new_date != entry.entry_date:
                leaving = await self.refs.leaving_dates(org_id, {entry.user_id})
                self.time.ensure_before_leaving(leaving.get(entry.user_id), new_date, "")
            if refs_changed or time_changed:
                if client is not None:
                    await ensure_client_accepts_work(self.session, client)
                if job is not None:
                    ensure_job_open(job)
            if "is_billable" in fields or refs_changed:
                billable = self.time.resolve_billable(data.is_billable, job, activity, client, "")
            else:
                billable = entry.is_billable and client is not None
            if time_changed:
                logged = await self.entries.day_minutes(
                    org_id, entry.user_id, {new_date}, exclude_ids={entry.id}
                )
                self.time._ensure_day_cap(logged.get(new_date, 0) + new_minutes, new_date, "")

            new_values = await self._priced(
                ctx, entry, client, job, new_date, new_minutes, refs_changed
            )
            note = data.internal_note if "internal_note" in fields else entry.internal_note
            new_values.update(
                entry_date=new_date,
                minutes=new_minutes,
                client_id=client.id if client else None,
                job_id=job.id if job else None,
                # A task belongs to one job: moving the entry to another job drops it.
                task_id=entry.task_id if (job.id if job else None) == entry.job_id else None,
                activity_code_id=activity.id if activity else None,
                description=data.description if "description" in fields else entry.description,
                internal_note=note,
                is_billable=billable,
                timesheet_id=new_sheet.id,
            )
            changed = {k: v for k, v in new_values.items() if getattr(entry, k) != v}
            if not changed:
                raise TimeCorrectionEmptyError()

            before = snapshot(entry, old_sheet.week_start, _names_of(before_view))
            old_job_id = entry.job_id
            for name, value in changed.items():
                setattr(entry, name, value)
            await self.session.flush()
            after = snapshot(entry, new_sheet.week_start, _names(client, job, activity))
            row = await self.history.add(
                TimeEntryHistory(
                    organization_id=org_id,
                    time_entry_id=entry.id,
                    user_id=entry.user_id,
                    corrected_by=ctx.user.id,
                    reason=reason,
                    before=before,
                    after=after,
                )
            )
            await self.time._audit(
                ctx,
                rctx,
                "time_entry.corrected",
                entry,
                {
                    "reason": reason,
                    "history_id": str(row.id),
                    "client_name": client.name if client else None,
                    "changes": _audit_changes(changed, before, after),
                },
            )
            if changed.keys() & BUDGET_FIELDS:
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

    async def _priced(
        self,
        ctx: OrgContext,
        entry: TimeEntry,
        client: Client | None,
        job: Job | None,
        new_date: date,
        new_minutes: int,
        refs_changed: bool,
    ) -> dict[str, Any]:
        """The rate and values after the correction. The rate is kept unless the client, job,
        date or activity changed; the values follow the minutes."""
        rate_inputs_changed = refs_changed or new_date != entry.entry_date
        source: str | None
        if rate_inputs_changed:
            [rate] = await charge_rates.resolve_many(
                self.session,
                ctx.organization,
                [
                    RateQuery(
                        entry.user_id,
                        client.id if client else None,
                        job.id if job else None,
                        new_date,
                    )
                ],
            )
            charge_rate, cost_rate, source = rate.charge_rate, rate.cost_rate, rate.source
        else:
            charge_rate, cost_rate, source = entry.charge_rate, entry.cost_rate, entry.rate_source
        if rate_inputs_changed or new_minutes != entry.minutes:
            charge_value = time_value(new_minutes, charge_rate)
            cost_value = time_value(new_minutes, cost_rate)
        else:
            charge_value, cost_value = entry.charge_value, entry.cost_value
        return {
            "charge_rate": charge_rate,
            "cost_rate": cost_rate,
            "charge_value": charge_value,
            "cost_value": cost_value,
            "rate_source": source,
        }

    async def history_of(
        self, ctx: OrgContext, entry_id: uuid.UUID, params: PageParams
    ) -> Page[TimeEntryHistoryRead]:
        """The corrections of an entry, newest first: the person, their approver or anyone with
        ``time.read_all``. It stays readable after the entry is deleted."""
        org_id = ctx.organization_id
        entry = await self.entries.get(org_id, entry_id)
        owner = entry.user_id if entry else await self.history.owner_of(org_id, entry_id)
        if owner is None:
            raise NotFoundError("Time entry not found.")
        await self.time.ensure_can_view_person(ctx, owner)
        page = await self.history.list_page(org_id, entry_id, params)
        names = await self.refs.user_names(
            org_id, {r.user_id for r in page.items} | {r.corrected_by for r in page.items}
        )
        cost, note = show_cost(ctx), can_see_note(ctx, owner)
        items = []
        for row in page.items:
            before = _masked(row.before, cost=cost, note=note)
            after = _masked(row.after, cost=cost, note=note)
            items.append(
                TimeEntryHistoryRead(
                    id=row.id,
                    time_entry_id=row.time_entry_id,
                    user_id=row.user_id,
                    user_name=names.get(row.user_id),
                    corrected_by=row.corrected_by,
                    corrected_by_name=names.get(row.corrected_by),
                    reason=row.reason,
                    before=before,
                    after=after,
                    changes={
                        key: [before.get(key), value]
                        for key, value in after.items()
                        if before.get(key) != value
                    },
                    created_at=row.created_at,
                )
            )
        return Page(items=items, next_cursor=page.next_cursor)


def _names(client: Client | None, job: Job | None, activity: ActivityCode | None) -> _Names:
    return _Names(
        client.name if client else None,
        job.number if job else None,
        job.name if job else None,
        activity.code if activity else None,
    )


def _audit_changes(
    changed: dict[str, Any], before: dict[str, Any], after: dict[str, Any]
) -> dict[str, list[Any]]:
    """``{field: [before, after]}`` of the fields that changed. The audit trail says that a note
    changed, never what it says (the history table keeps the text)."""
    result = {name: [before[name], after[name]] for name in changed}
    if "internal_note" in result:
        had, has = before["internal_note"] is not None, after["internal_note"] is not None
        result["internal_note"] = [had, has]
    return result


def _masked(snap: dict[str, Any], *, cost: bool, note: bool) -> dict[str, Any]:
    """The snapshot without what the reader may not see (cost figures, the internal note)."""
    result = dict(snap)
    if not cost:
        for name in COST_FIELDS:
            if name in result:
                result[name] = None
    if not note and "internal_note" in result:
        result["internal_note"] = None
    return result
