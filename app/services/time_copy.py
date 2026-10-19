"""Copying time: a whole week into another week, or one entry to another day.

A copy is a new entry: it is validated exactly like one (closed job, daily cap, time
increment, period lock, editable timesheet) by the same code that creates entries.
"""

import uuid
from datetime import date, timedelta

from pydantic import ValidationError as SchemaValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import AppException, ConflictError, NotFoundError, ValidationError
from app.db.models import TimeEntry
from app.db.session import unit_of_work
from app.schemas.time import TimeEntryCreate, TimeEntryRead
from app.schemas.time_views import CopyEntryRequest, CopySkipped, CopyWeekRequest, CopyWeekResult
from app.services import audit
from app.services.policy import OrgContext
from app.services.time_entries import TimeEntryService, show_cost
from app.services.time_rules import EDITABLE_SHEET_STATUSES, week_start_for
from app.services.time_visibility import can_see_note


class TimesheetNotEditableError(ConflictError):
    code = "TIMESHEET_NOT_EDITABLE"
    message = "The timesheet of that week is submitted or approved; time cannot be added to it."


class TimeCopyService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.time = TimeEntryService(session)

    async def _ensure_access(self, ctx: OrgContext, owner: uuid.UUID) -> None:
        """Own time needs ``time.log``; another person's ``time.manage_all`` and the right to
        see their time (the copy reveals what the source says)."""
        self.time.ensure_can_write(ctx, owner)
        if owner != ctx.user.id:
            await self.time.ensure_can_view_person(ctx, owner)

    @staticmethod
    def _draft(entry: TimeEntry, day: date, include_notes: bool) -> TimeEntryCreate:
        return TimeEntryCreate(
            user_id=entry.user_id,
            entry_date=day,
            minutes=entry.minutes,
            client_id=entry.client_id,
            job_id=entry.job_id,
            task_id=entry.task_id,
            activity_code_id=entry.activity_code_id,
            description=entry.description,
            internal_note=entry.internal_note if include_notes else None,
            is_billable=entry.is_billable,
        )

    async def copy_week(
        self, ctx: OrgContext, data: CopyWeekRequest, rctx: RequestContext
    ) -> CopyWeekResult:
        """Copy the person's entries of one week into another, day for day. Entries that cannot
        be copied are reported, the others are created (partial success)."""
        org = ctx.organization
        owner = data.user_id or ctx.user.id
        await self._ensure_access(ctx, owner)
        if owner != ctx.user.id and owner not in await self.time.refs.active_member_ids(
            org.id, {owner}
        ):
            raise ValidationError.for_field(
                "user_id", "This person is not an active member of the practice."
            )
        source = week_start_for(org, data.from_week_start)
        target = week_start_for(org, data.to_week_start)
        if source == target:
            raise ValidationError.for_field(
                "to_week_start", "Choose a different week to copy into."
            )
        views = await self.time.entries.range_views(
            org.id, owner, source, source + timedelta(days=6)
        )
        if data.entry_ids is not None:
            wanted = set(data.entry_ids)
            unknown = wanted - {v.entry.id for v in views}
            if unknown:
                raise NotFoundError(
                    "Time entry not found.",
                    details={"field": "entry_ids", "entry_ids": sorted(str(i) for i in unknown)},
                )
            views = [v for v in views if v.entry.id in wanted]
        sheet = await self.time.sheets.find(org.id, owner, target)
        if sheet is not None and sheet.status not in EDITABLE_SHEET_STATUSES:
            raise TimesheetNotEditableError(
                details={
                    "timesheet_id": str(sheet.id),
                    "timesheet_status": sheet.status,
                    "week_start": target.isoformat(),
                }
            )

        offset = target - source
        made: list[tuple[uuid.UUID, uuid.UUID]] = []
        skipped: list[CopySkipped] = []
        async with unit_of_work(self.session):
            for view in views:
                day = view.entry.entry_date + offset
                try:
                    item = self._draft(view.entry, day, data.include_notes)
                    # A failed copy rolls back to here and the others carry on. The period
                    # lock is checked by the code that creates the entry.
                    async with self.session.begin_nested():
                        [entry_id] = await self.time._create_many(
                            ctx, [item], rctx, prefix_items=False, source="copy_week"
                        )
                except AppException as exc:
                    skipped.append(_skipped(view.entry.id, exc))
                except SchemaValidationError:
                    skipped.append(
                        CopySkipped(
                            source_entry_id=view.entry.id,
                            code="VALIDATION_ERROR",
                            message=f"The entry cannot be copied to {day.isoformat()}.",
                        )
                    )
                else:
                    made.append((view.entry.id, entry_id))
            created = await self.time.entries.views(org.id, [new_id for _, new_id in made])
            if made or skipped:
                await audit.record(
                    self.session,
                    "time_entry.week_copied",
                    ctx=rctx,
                    actor_user_id=ctx.user.id,
                    organization_id=org.id,
                    target_type="timesheet" if created else None,
                    target_id=created[0].entry.timesheet_id if created else None,
                    meta={
                        "user_id": str(owner),
                        "from_week_start": source.isoformat(),
                        "to_week_start": target.isoformat(),
                        "include_notes": data.include_notes,
                        "created": [
                            {"source_entry_id": str(s), "entry_id": str(n)} for s, n in made
                        ],
                        "skipped": [
                            {"source_entry_id": str(s.source_entry_id), "code": s.code}
                            for s in skipped
                        ],
                    },
                )
        cost = show_cost(ctx)
        return CopyWeekResult(
            from_week_start=source,
            to_week_start=target,
            created=[
                TimeEntryRead.from_view(
                    v, show_cost=cost, show_note=can_see_note(ctx, v.entry.user_id)
                )
                for v in created
            ],
            skipped=skipped,
        )

    async def copy_entry(
        self, ctx: OrgContext, entry_id: uuid.UUID, data: CopyEntryRequest, rctx: RequestContext
    ) -> TimeEntryRead:
        """Copy one entry to another day (the same day too), for the same person."""
        source = await self.time.entries.get(ctx.organization_id, entry_id)
        if source is None:
            raise NotFoundError("Time entry not found.")
        await self._ensure_access(ctx, source.user_id)
        item = self._draft(source, data.date, data.include_notes)
        async with unit_of_work(self.session):
            created = await self.time.create(ctx, item, rctx, source="copy")
            await audit.record(
                self.session,
                "time_entry.copied",
                ctx=rctx,
                actor_user_id=ctx.user.id,
                organization_id=ctx.organization_id,
                target_type="time_entry",
                target_id=created.id,
                meta={
                    "user_id": str(source.user_id),
                    "source_entry_id": str(source.id),
                    "entry_date": data.date.isoformat(),
                    "include_notes": data.include_notes,
                },
            )
        return created


def _skipped(source_entry_id: uuid.UUID, exc: AppException) -> CopySkipped:
    """The code and message of the first problem: a field error's own code (for example
    ``activity_not_allowed``) and message say more than the generic validation error."""
    code, message = exc.code, exc.message
    details = exc.details
    if isinstance(details, list) and details and isinstance(details[0], dict):
        first = details[0]
        message = str(first.get("message") or message)
        field_code = first.get("code")
        if code == ValidationError.code and field_code and field_code != "invalid":
            code = str(field_code).upper()
    return CopySkipped(source_entry_id=source_entry_id, code=code, message=message)
