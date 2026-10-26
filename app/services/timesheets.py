"""Weekly timesheets: submission, recall, approval, rejection and reopening.

``open -> submitted -> approved``, or ``submitted -> rejected -> (edit) -> submitted``
(docs/domain.md, "Timesheets"). Every transition locks the sheet row ``FOR UPDATE``; time
entry writes take the same lock, so a sheet never changes status halfway through an edit.
"""

import uuid
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import AuthorizationError, ConflictError, NotFoundError, ValidationError
from app.db.models import Timesheet
from app.db.session import unit_of_work
from app.repositories.time_entries import TimeEntryRepository
from app.repositories.time_refs import TimeReferenceRepository
from app.repositories.timesheets import SheetTotals, TimesheetRepository
from app.schemas.time import DayTotal, TimeEntryRead, TimesheetDetail, TimesheetRead
from app.services import audit, notification_events
from app.services.policy import OrgContext, has_permission
from app.services.time_entries import TimeEntryService, show_cost
from app.services.time_rules import InvalidTransitionError, practice_now
from app.services.time_visibility import can_see_note
from app.utils.pagination import Page, PageParams


class TimesheetEmptyError(ValidationError):
    code = "TIMESHEET_EMPTY"
    message = "A timesheet needs at least one entry before it can be submitted."


class TimesheetBilledError(ConflictError):
    code = "TIMESHEET_HAS_BILLED_TIME"
    message = "Some of this timesheet's time has been billed or written off; it cannot reopen."


class SelfApprovalError(AuthorizationError):
    code = "SELF_APPROVAL_FORBIDDEN"
    message = "You cannot decide on your own timesheet."


class TimesheetService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.sheets = TimesheetRepository(session)
        self.entries = TimeEntryRepository(session)
        self.refs = TimeReferenceRepository(session)
        self.time = TimeEntryService(session)

    # --- Reads ---------------------------------------------------------------------------

    async def list_sheets(
        self,
        ctx: OrgContext,
        *,
        user_id: str | None,
        date_from: date | None,
        date_to: date | None,
        statuses: Sequence[str] | None,
        params: PageParams,
    ) -> Page[TimesheetRead]:
        user_ids: list[uuid.UUID] | None
        if user_id in (None, "me"):
            user_ids = [ctx.user.id]
        elif user_id == "all":
            if has_permission(ctx, "time.read_all"):
                user_ids = None
            else:
                user_ids = [
                    ctx.user.id,
                    *await self.refs.managed_user_ids(ctx.organization_id, ctx.user.id),
                ]
        else:
            assert user_id is not None
            try:
                target = uuid.UUID(user_id)
            except ValueError as exc:
                raise ValidationError.for_field("user_id", "Use a user id, 'me' or 'all'.") from exc
            await self.time.ensure_can_view_person(ctx, target)
            user_ids = [target]
        page = await self.sheets.list_page(
            ctx.organization_id,
            user_ids=user_ids,
            date_from=date_from,
            date_to=date_to,
            statuses=statuses,
            params=params,
        )
        return Page(items=await self._reads(ctx, page.items), next_cursor=page.next_cursor)

    async def get(self, ctx: OrgContext, timesheet_id: uuid.UUID) -> TimesheetDetail:
        sheet = await self.sheets.get(ctx.organization_id, timesheet_id)
        if sheet is None:
            raise NotFoundError("Timesheet not found.")
        await self.time.ensure_can_view_person(ctx, sheet.user_id)
        return await self._detail(ctx, sheet)

    async def approvals(self, ctx: OrgContext, params: PageParams) -> Page[TimesheetRead]:
        """Submitted sheets of the people the caller manages (everyone with time.read_all),
        oldest week first."""
        user_ids: list[uuid.UUID] | None = None
        if not has_permission(ctx, "time.read_all"):
            user_ids = sorted(await self.refs.managed_user_ids(ctx.organization_id, ctx.user.id))
            if not user_ids:
                return Page(items=[], next_cursor=None)
        page = await self.sheets.list_page(
            ctx.organization_id,
            user_ids=user_ids,
            date_from=None,
            date_to=None,
            statuses=["submitted"],
            params=params,
            descending=False,
        )
        return Page(items=await self._reads(ctx, page.items), next_cursor=page.next_cursor)

    # --- Transitions ---------------------------------------------------------------------

    async def submit(
        self, ctx: OrgContext, timesheet_id: uuid.UUID, rctx: RequestContext
    ) -> TimesheetDetail:
        async with unit_of_work(self.session):
            sheet = await self._lock(ctx, timesheet_id)
            self._ensure_owner(ctx, sheet)
            self._ensure_status(sheet, ("open", "rejected"), "submitted")
            totals = (await self.sheets.totals(ctx.organization_id, [sheet.id])).get(sheet.id)
            if totals is None or totals.entry_count == 0:
                raise TimesheetEmptyError()
            old = sheet.status
            submitted_at = practice_now()
            sheet.submitted_at = submitted_at
            if sheet.approval_exempt:
                # The owner's time needs no approval (practice setting + `timesheet.exempt`):
                # the week is approved as it is submitted and nobody is asked.
                sheet.status = "approved"
                sheet.decided_by = ctx.user.id
                sheet.decided_at = submitted_at
                sheet.decision_note = "Approval not required"
                await self._save(ctx, rctx, "timesheet.approved", sheet, old, totals)
                return await self._detail(ctx, sheet)
            sheet.status = "submitted"
            sheet.decided_by = None
            sheet.decided_at = None
            sheet.decision_note = None
            await self._save(ctx, rctx, "timesheet.submitted", sheet, old, totals)
            await notification_events.approval_pending(
                self.session,
                organization_id=ctx.organization_id,
                timesheet_id=sheet.id,
                submitter_id=sheet.user_id,
                submitter_name=ctx.user.full_name,
                week_start=sheet.week_start,
                total_minutes=totals.total_minutes,
                submitted_at=submitted_at,
            )
            result = await self._detail(ctx, sheet)
        return result

    async def recall(
        self, ctx: OrgContext, timesheet_id: uuid.UUID, rctx: RequestContext
    ) -> TimesheetDetail:
        async with unit_of_work(self.session):
            sheet = await self._lock(ctx, timesheet_id)
            self._ensure_owner(ctx, sheet)
            self._ensure_status(sheet, ("submitted",), "open")
            sheet.status = "open"
            sheet.submitted_at = None
            await self._save(ctx, rctx, "timesheet.recalled", sheet, "submitted")
            result = await self._detail(ctx, sheet)
        return result

    async def approve(
        self,
        ctx: OrgContext,
        timesheet_id: uuid.UUID,
        note: str | None,
        rctx: RequestContext,
    ) -> TimesheetDetail:
        async with unit_of_work(self.session):
            sheet = await self._lock(ctx, timesheet_id)
            await self._approve(ctx, sheet, note, rctx)
            result = await self._detail(ctx, sheet)
        return result

    async def bulk_approve(
        self, ctx: OrgContext, timesheet_ids: Sequence[uuid.UUID], rctx: RequestContext
    ) -> list[TimesheetRead]:
        """All or nothing: every sheet must exist, be submitted and not be the caller's own
        (unless the caller holds organization.update)."""
        wanted = list(dict.fromkeys(timesheet_ids))
        async with unit_of_work(self.session):
            sheets = await self.sheets.lock_many(ctx.organization_id, wanted)
            found = {s.id for s in sheets}
            missing = [str(i) for i in wanted if i not in found]
            if missing:
                raise NotFoundError("Timesheet not found.", details={"timesheet_ids": missing})
            for sheet in sheets:
                await self._approve(ctx, sheet, None, rctx)
            by_id = {s.id: s for s in sheets}
            result = await self._reads(ctx, [by_id[i] for i in wanted])
        return result

    async def reject(
        self, ctx: OrgContext, timesheet_id: uuid.UUID, note: str, rctx: RequestContext
    ) -> TimesheetDetail:
        async with unit_of_work(self.session):
            sheet = await self._lock(ctx, timesheet_id)
            self._ensure_not_self(ctx, sheet)
            self._ensure_status(sheet, ("submitted",), "rejected")
            decided_at = practice_now()
            sheet.status = "rejected"
            sheet.decided_by = ctx.user.id
            sheet.decided_at = decided_at
            sheet.decision_note = note
            await self._save(ctx, rctx, "timesheet.rejected", sheet, "submitted")
            if sheet.user_id != ctx.user.id:
                await notification_events.timesheet_returned(
                    self.session,
                    organization_id=ctx.organization_id,
                    timesheet_id=sheet.id,
                    owner_id=sheet.user_id,
                    approver_name=ctx.user.full_name,
                    week_start=sheet.week_start,
                    note=note,
                    decided_at=decided_at,
                )
            result = await self._detail(ctx, sheet)
        return result

    async def reopen(
        self, ctx: OrgContext, timesheet_id: uuid.UUID, rctx: RequestContext
    ) -> TimesheetDetail:
        async with unit_of_work(self.session):
            sheet = await self._lock(ctx, timesheet_id)
            self._ensure_not_self(ctx, sheet)
            self._ensure_status(sheet, ("approved",), "open")
            # Lock the entries too: billing locks them before allocating, so a sheet cannot
            # reopen while its time is being put on an invoice.
            statuses = await self.sheets.lock_entry_statuses(ctx.organization_id, sheet.id)
            if any(s != "unbilled" for s in statuses):
                raise TimesheetBilledError()
            sheet.status = "open"
            sheet.submitted_at = None
            sheet.decided_by = None
            sheet.decided_at = None
            sheet.decision_note = None
            await self._save(ctx, rctx, "timesheet.reopened", sheet, "approved")
            result = await self._detail(ctx, sheet)
        return result

    # --- Helpers -------------------------------------------------------------------------

    async def _approve(
        self, ctx: OrgContext, sheet: Timesheet, note: str | None, rctx: RequestContext
    ) -> None:
        self._ensure_not_self(ctx, sheet)
        self._ensure_status(sheet, ("submitted",), "approved")
        sheet.status = "approved"
        sheet.decided_by = ctx.user.id
        sheet.decided_at = practice_now()
        sheet.decision_note = note
        await self._save(ctx, rctx, "timesheet.approved", sheet, "submitted")

    async def _lock(self, ctx: OrgContext, timesheet_id: uuid.UUID) -> Timesheet:
        sheet = await self.sheets.get(ctx.organization_id, timesheet_id, for_update=True)
        if sheet is None:
            raise NotFoundError("Timesheet not found.")
        return sheet

    @staticmethod
    def _ensure_owner(ctx: OrgContext, sheet: Timesheet) -> None:
        if sheet.user_id != ctx.user.id:
            raise AuthorizationError("Only the owner of a timesheet can submit or recall it.")

    @staticmethod
    def _ensure_not_self(ctx: OrgContext, sheet: Timesheet) -> None:
        if sheet.user_id == ctx.user.id and not has_permission(ctx, "organization.update"):
            raise SelfApprovalError()

    @staticmethod
    def _ensure_status(sheet: Timesheet, allowed: Sequence[str], target: str) -> None:
        if sheet.status not in allowed:
            raise InvalidTransitionError(
                f"A {sheet.status} timesheet cannot be {target}.",
                details={"from": sheet.status, "to": target},
            )

    async def _save(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        action: str,
        sheet: Timesheet,
        old_status: str,
        totals: SheetTotals | None = None,
    ) -> None:
        await self.session.flush()
        await self.session.refresh(sheet)
        if totals is None:
            totals = (await self.sheets.totals(ctx.organization_id, [sheet.id])).get(
                sheet.id, SheetTotals()
            )
        names = await self.refs.user_names(ctx.organization_id, [sheet.user_id])
        meta: dict[str, Any] = {
            "user_id": str(sheet.user_id),
            "user_name": names.get(sheet.user_id),
            "week_start": sheet.week_start.isoformat(),
            "old_status": old_status,
            "new_status": sheet.status,
            "total_minutes": totals.total_minutes,
            "billable_minutes": totals.billable_minutes,
            "billable_value": totals.billable_value,
        }
        if sheet.decision_note:
            meta["decision_note"] = sheet.decision_note
        await audit.record(
            self.session,
            action,
            ctx=rctx,
            actor_user_id=ctx.user.id,
            organization_id=ctx.organization_id,
            target_type="timesheet",
            target_id=sheet.id,
            meta=meta,
        )

    async def _reads(self, ctx: OrgContext, sheets: Sequence[Timesheet]) -> list[TimesheetRead]:
        ids = [s.id for s in sheets]
        totals = await self.sheets.totals(ctx.organization_id, ids)
        names = await self.refs.user_names(ctx.organization_id, {s.user_id for s in sheets})
        return [
            TimesheetRead.build(s, totals.get(s.id, SheetTotals()), names.get(s.user_id))
            for s in sheets
        ]

    async def _detail(self, ctx: OrgContext, sheet: Timesheet) -> TimesheetDetail:
        [base] = await self._reads(ctx, [sheet])
        views = await self.entries.sheet_views(ctx.organization_id, sheet.id)
        cost = show_cost(ctx)
        days = [sheet.week_start + timedelta(days=i) for i in range(7)]
        per_day = dict.fromkeys(days, 0)
        for view in views:
            per_day[view.entry.entry_date] = per_day.get(view.entry.entry_date, 0) + (
                view.entry.minutes
            )
        return TimesheetDetail(
            **base.model_dump(),
            entries=[
                TimeEntryRead.from_view(
                    v, show_cost=cost, show_note=can_see_note(ctx, v.entry.user_id)
                )
                for v in views
            ],
            day_totals=[DayTotal(day=d, minutes=m) for d, m in per_day.items()],
        )
