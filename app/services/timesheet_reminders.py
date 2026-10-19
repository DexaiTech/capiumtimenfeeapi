"""Timesheet reminders: staff with a missing timesheet and approvers with sheets waiting.

A reminder is sent once per person and week (``timesheet_reminders`` records it, and a unique
key makes two runs at once safe), so running again, or the daily scheduled job, only reaches
people who were not reminded about that week yet. Everything goes through the notification
service, which honours the practice's switches and each person's channels.
"""

import uuid
from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import AuthorizationError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.notification_types import NotificationType
from app.db.models import Organization, Timesheet
from app.db.session import unit_of_work
from app.repositories.notifications import NotificationSettingsRepository
from app.repositories.time_overview import TimeOverviewRepository
from app.repositories.time_refs import TimeReferenceRepository
from app.repositories.timesheet_reminders import TimesheetReminderRepository
from app.repositories.timesheets import SheetTotals, TimesheetRepository
from app.schemas.time_views import MissingTimesheet, RemindersResult, RemindersRun
from app.services import audit, notification_events
from app.services.notification_settings import type_enabled
from app.services.notifications import NotifyResult, dedupe_key
from app.services.policy import OrgContext, ensure_any, has_permission
from app.services.time_entries import TimeEntryService
from app.services.time_rules import week_start_for
from app.services.timesheet_missing import (
    MissingScope,
    last_completed_week,
    load_practice,
    missing_timesheets,
)

log = get_logger(__name__)


async def run_reminders(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    week_start: date | None = None,
    scope: MissingScope | None = None,
    actor_user_id: uuid.UUID | None = None,
    rctx: RequestContext | None = None,
    organization: Organization | None = None,
) -> RemindersResult:
    """Send the reminders of one practice for the week of ``week_start`` (default: the last
    completed week). Does not commit: the caller's transaction carries the rows.

    ``actor_user_id`` is the person who ran it (``None``: the scheduler). A run is audited when
    a person ran it or when it sent anything.
    """
    org = organization or await load_practice(session, organization_id)
    scope = scope or MissingScope()
    week = week_start_for(org, week_start) if week_start is not None else last_completed_week(org)
    settings = NotificationSettingsRepository(session)
    reminders = TimesheetReminderRepository(session)
    disabled: list[str] = []

    rows = await missing_timesheets(session, org.id, week, scope, organization=org)
    reminded, already, unreachable = await _remind_staff(
        session, org.id, week, rows, settings, reminders, disabled
    )
    waiting, approvers_reminded, approvers_already = await _remind_approvers(
        session, org.id, week, scope, settings, reminders, disabled
    )
    result = RemindersResult(
        week_start=week,
        missing=len(rows),
        reminded=reminded,
        already_reminded=already,
        unreachable=unreachable,
        sheets_waiting=waiting,
        approvers_reminded=approvers_reminded,
        approvers_already_reminded=approvers_already,
        disabled=disabled,
    )
    if actor_user_id is not None or reminded or approvers_reminded:
        await audit.record(
            session,
            "timesheet.reminders_sent",
            ctx=rctx,
            actor_user_id=actor_user_id,
            organization_id=org.id,
            target_type="timesheet_reminders",
            meta={
                **result.model_dump(mode="json"),
                "source": "manual" if actor_user_id else "scheduler",
            },
        )
    return result


async def run_for_all_practices(session: AsyncSession) -> dict[str, int]:
    """The daily job: every practice gets its reminders for the last completed week. A practice
    that fails is logged and skipped; the others still run."""
    stmt = (
        select(Organization.id).where(Organization.deleted_at.is_(None)).order_by(Organization.id)
    )
    organization_ids = list((await session.execute(stmt)).scalars())
    totals = {"practices": 0, "failed": 0, "reminded": 0, "approvers_reminded": 0}
    for organization_id in organization_ids:
        try:
            async with unit_of_work(session):
                result = await run_reminders(session, organization_id)
        except Exception:
            log.exception("timesheet_reminders_failed", organization_id=str(organization_id))
            totals["failed"] += 1
            continue
        totals["practices"] += 1
        totals["reminded"] += result.reminded
        totals["approvers_reminded"] += result.approvers_reminded
    return totals


async def _enabled(
    settings: NotificationSettingsRepository,
    organization_id: uuid.UUID,
    type_: NotificationType,
    disabled: list[str],
) -> bool:
    on = type_enabled(await settings.practice_setting(organization_id, type_))
    if not on:
        disabled.append(str(type_))
    return on


async def _remind_staff(
    session: AsyncSession,
    organization_id: uuid.UUID,
    week: date,
    rows: list[MissingTimesheet],
    settings: NotificationSettingsRepository,
    reminders: TimesheetReminderRepository,
    disabled: list[str],
) -> tuple[int, int, int]:
    """``(reminded, already reminded, unreachable)`` of the people with a missing timesheet."""
    people = await settings.recipients(organization_id, [row.user_id for row in rows])
    reachable = [row.user_id for row in rows if row.user_id in people]
    unreachable = len(rows) - len(reachable)
    if not reachable or not await _enabled(
        settings, organization_id, NotificationType.TIMESHEET_REMINDER, disabled
    ):
        return 0, 0, unreachable
    claimed = await reminders.claim(organization_id, week, "timesheet", reachable)
    already = len(reachable) - len(claimed)
    if not claimed:
        return 0, already, unreachable
    sent = await notification_events.timesheet_reminder(
        session, organization_id=organization_id, recipients=sorted(claimed), week_start=week
    )
    if sent is None:
        # The notification could not be raised: take the claims back so a later run retries.
        await reminders.release(organization_id, week, "timesheet", claimed)
        return 0, already, unreachable
    return len(claimed), already, unreachable


async def _remind_approvers(
    session: AsyncSession,
    organization_id: uuid.UUID,
    week: date,
    scope: MissingScope,
    settings: NotificationSettingsRepository,
    reminders: TimesheetReminderRepository,
    disabled: list[str],
) -> tuple[int, int, int]:
    """``(sheets waiting, approvers reminded, approvers already reminded)``: one summary per
    approver for the submitted sheets of the week and earlier ones."""
    sheets = await TimeOverviewRepository(session).waiting_sheets(
        organization_id, week, user_ids=scope.user_ids, manager_user_id=scope.manager_user_id
    )
    if not sheets or not await _enabled(
        settings, organization_id, NotificationType.APPROVAL_PENDING, disabled
    ):
        return len(sheets), 0, 0
    per_approver: dict[uuid.UUID, list[Timesheet]] = defaultdict(list)
    for sheet in sheets:
        for approver in await notification_events.approvers_of(
            session, organization_id, sheet.user_id
        ):
            per_approver[approver].append(sheet)
    if not per_approver:
        return len(sheets), 0, 0
    claimed = await reminders.claim(organization_id, week, "approval", list(per_approver))
    already = len(per_approver) - len(claimed)
    names = await TimeReferenceRepository(session).user_names(
        organization_id, {sheet.user_id for sheet in sheets}
    )
    totals = await TimesheetRepository(session).totals(organization_id, [s.id for s in sheets])
    failed: list[uuid.UUID] = []
    for approver in sorted(claimed):
        sent = await _notify_approver(
            session, organization_id, approver, per_approver[approver], names, totals, week
        )
        if sent is None:
            failed.append(approver)
    await reminders.release(organization_id, week, "approval", failed)
    return len(sheets), len(claimed) - len(failed), already


async def _notify_approver(
    session: AsyncSession,
    organization_id: uuid.UUID,
    approver_id: uuid.UUID,
    sheets: list[Timesheet],
    names: dict[uuid.UUID, str],
    totals: dict[uuid.UUID, SheetTotals],
    week: date,
) -> NotifyResult | None:
    people = {sheet.user_id for sheet in sheets}
    who = (
        names.get(next(iter(people)), "A colleague")
        if len(people) == 1
        else f"{len(people)} staff members"
    )
    oldest = min(sheet.week_start for sheet in sheets)
    count = len(sheets)
    minutes = sum(totals.get(sheet.id, SheetTotals()).total_minutes for sheet in sheets)
    return await notification_events.notify_safely(
        session,
        organization_id=organization_id,
        type=NotificationType.APPROVAL_PENDING,
        recipients=[approver_id],
        title="Timesheets waiting for approval",
        body=(
            f"{count} timesheet{'s' if count != 1 else ''} from {who} "
            f"{'is' if count == 1 else 'are'} waiting for your approval "
            f"(oldest: week starting {notification_events.uk_date(oldest)})."
        ),
        link=notification_events.LINK_APPROVALS,
        payload={"week_start": week.isoformat(), "timesheets": count},
        dedupe_key=dedupe_key(NotificationType.APPROVAL_PENDING, "reminder", week.isoformat()),
        merge={
            "submitter_name": who,
            "week_start": notification_events.uk_date(oldest),
            "total_hours": notification_events.hours(minutes),
        },
    )


class TimesheetReminderService:
    """The missing-timesheet list and the reminder run, with their access rules."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.time = TimeEntryService(session)
        self.refs = TimeReferenceRepository(session)

    async def missing(
        self,
        ctx: OrgContext,
        *,
        week_start: date | None,
        user_id: str | None,
        manager_user_id: uuid.UUID | None,
    ) -> list[MissingTimesheet]:
        """Everyone with ``time.read_all``; otherwise the caller's own status and the people
        they manage (nobody managed: an empty list)."""
        org = ctx.organization
        read_all = has_permission(ctx, "time.read_all")
        user_ids: frozenset[uuid.UUID] | None = None
        if user_id is not None:
            person = await self._person(ctx, user_id)
            user_ids = frozenset({person})
        elif not read_all:
            user_ids = frozenset(await self.refs.managed_user_ids(org.id, ctx.user.id))
            if not user_ids:
                return []
        if manager_user_id is not None:
            if not read_all and manager_user_id != ctx.user.id:
                raise AuthorizationError()
            if await self.refs.get_member(org.id, manager_user_id) is None:
                raise NotFoundError("Staff member not found.", details={"field": "manager_user_id"})
        week = week_start or last_completed_week(org)
        scope = MissingScope(user_ids=user_ids, manager_user_id=manager_user_id)
        return await missing_timesheets(self.session, org.id, week, scope, organization=org)

    async def run(
        self, ctx: OrgContext, data: RemindersRun, rctx: RequestContext
    ) -> RemindersResult:
        """``organization.update`` or ``timesheet.approve``. Practice-wide with
        ``organization.update`` or ``time.read_all``; otherwise for the people the caller
        manages."""
        ensure_any(ctx, "organization.update", "timesheet.approve")
        wide = has_permission(ctx, "organization.update") or has_permission(ctx, "time.read_all")
        scope = MissingScope()
        if not wide:
            managed = await self.refs.managed_user_ids(ctx.organization_id, ctx.user.id)
            scope = MissingScope(user_ids=frozenset(managed))
        async with unit_of_work(self.session):
            result = await run_reminders(
                self.session,
                ctx.organization_id,
                week_start=data.week_start,
                scope=scope,
                actor_user_id=ctx.user.id,
                rctx=rctx,
                organization=ctx.organization,
            )
        return result

    async def _person(self, ctx: OrgContext, user_id: str) -> uuid.UUID:
        if user_id == "me":
            return ctx.user.id
        try:
            person = uuid.UUID(user_id)
        except ValueError as exc:
            raise ValidationError.for_field("user_id", "Use a user id or 'me'.") from exc
        await self.time.ensure_can_view_person(ctx, person)
        if await self.refs.get_member(ctx.organization_id, person) is None:
            raise NotFoundError("Staff member not found.", details={"field": "user_id"})
        return person
