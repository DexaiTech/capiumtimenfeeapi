"""Staff: records, lifecycle and the reads behind them (docs/modules/team-rates.md, "Staff").

A staff member is a practice member (``organization_members``) with a staff code, grade,
approver, working hours and dates. The staff id is the user id. Someone without a login is
backed by an inactive placeholder user, so time, rates and jobs refer to them like to anyone
else; ``staff_login.py`` gives them access later.

Rules:

* Codes are unique per practice (409 ``STAFF_CODE_TAKEN``) and come from the ``staff``
  number sequence when not given. No two active staff share an email address, whether it is a
  work email or a login address (409 ``STAFF_EMAIL_TAKEN``).
* Staff are deactivated (``inactive``, can return) or marked ``left`` (leaving date set, their
  approver links cleared); everything recorded for them stays. A record that nothing refers to
  can be deleted, any other answers 409 ``STAFF_IN_USE``.
* The practice always keeps one active owner who can sign in; only an owner changes another
  owner; nobody deactivates or deletes themselves.
* Every change takes the practice row lock, so these checks cannot be raced.
"""

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import AuthorizationError, ConflictError, NotFoundError, ValidationError
from app.core.permissions import OWNER_KEY
from app.db.models import Grade, OrganizationMember, Role, User
from app.db.session import unit_of_work
from app.repositories import staff_references
from app.repositories.grades import GradeRepository
from app.repositories.members import MemberRepository
from app.repositories.rate_rules import Scope
from app.repositories.roles import RoleRepository
from app.repositories.staff import StaffFilters, StaffRepository, StaffRow, normalised_status
from app.repositories.users import UserRepository
from app.schemas.staff import (
    StaffCreate,
    StaffDeactivate,
    StaffDetail,
    StaffRead,
    StaffUpdate,
    UsageRead,
)
from app.services import charge_rates, policy
from app.services.charge_rates import RateQuery, ResolvedRate
from app.services.common import apply_changes, record_audit
from app.services.members import ensure_initials_free, ensure_not_last_owner
from app.services.organizations import lock_and_refresh
from app.services.policy import OrgContext
from app.services.rate_rules import RateRuleService
from app.services.sessions import SessionService
from app.services.staff_codes import next_staff_code
from app.services.staff_common import (
    new_placeholder_user,
    staff_code_taken_error,
    staff_email_taken_error,
    staff_integrity_error,
)
from app.utils.pagination import Page, PageParams
from app.utils.practice_dates import today_in

DEFAULT_TARGET_BP = 7500
USAGE_LABELS = {
    "time_entries": "Time entries",
    "timesheets": "Timesheets",
    "timers": "Running timers",
    "disbursements": "Expenses",
    "invoices": "Invoices",
    "credit_notes": "Credit notes",
    "payments": "Payments",
    "jobs": "Jobs",
    "job_assignees": "Job assignments",
    "clients": "Clients",
    "rate_rules": "Rates",
    "rate_changes": "Rate history",
    "rate_reruns": "Re-rate runs",
    "rate_rerun_items": "Re-rate history",
    "wip_allocations": "Billed time",
    "wip_write_offs": "Written-off time",
    "organizations": "Practices created",
}


def usage_rows(counts: dict[str, int]) -> list[UsageRead]:
    return [
        UsageRead(
            kind=name,
            label=USAGE_LABELS.get(name, name.replace("_", " ").capitalize()),
            count=count,
        )
        for name, count in sorted(counts.items())
    ]


class StaffService:
    def __init__(self, session: AsyncSession, sessions: SessionService) -> None:
        self.session = session
        self.sessions = sessions
        self.staff = StaffRepository(session)
        self.members = MemberRepository(session)
        self.roles = RoleRepository(session)
        self.grades = GradeRepository(session)
        self.users = UserRepository(session)

    # --- Reads ----------------------------------------------------------------------------

    @staticmethod
    def can_see_rates(ctx: OrgContext) -> bool:
        return policy.has_permission(ctx, "rate.read")

    async def _reads(self, ctx: OrgContext, rows: list[StaffRow]) -> list[StaffRead]:
        today = today_in(ctx.organization.timezone)
        show = self.can_see_rates(ctx)
        rates: list[ResolvedRate | None] = [None] * len(rows)
        if show and rows:
            resolved = await charge_rates.resolve_many(
                self.session,
                ctx.organization,
                [RateQuery(r.member.user_id, None, None, today) for r in rows],
            )
            rates = list(resolved)
        return [
            StaffRead.from_row(row, today=today, rate=rate, show_rates=show)
            for row, rate in zip(rows, rates, strict=True)
        ]

    async def fetch_row(self, ctx: OrgContext, user_id: uuid.UUID) -> StaffRow:
        row = await self.staff.get_row(ctx.organization_id, user_id)
        if row is None:
            raise NotFoundError("Staff member not found.")
        return row

    async def detail(self, ctx: OrgContext, row: StaffRow) -> StaffDetail:
        [read] = await self._reads(ctx, [row])
        counts = await staff_references.usage(self.session, ctx.organization_id, row.member.user_id)
        return StaffDetail(**read.model_dump(), usage=usage_rows(counts), can_delete=not counts)

    async def list_staff(
        self,
        ctx: OrgContext,
        filters: StaffFilters,
        *,
        sort: str,
        descending: bool,
        params: PageParams,
    ) -> Page[StaffRead]:
        today = today_in(ctx.organization.timezone)
        page = await self.staff.list_page(
            ctx.organization_id,
            replace(filters, today=today),
            sort=sort,
            descending=descending,
            params=params,
        )
        return Page(items=await self._reads(ctx, page.items), next_cursor=page.next_cursor)

    async def get(self, ctx: OrgContext, user_id: uuid.UUID) -> StaffDetail:
        return await self.detail(ctx, await self.fetch_row(ctx, user_id))

    # --- Create ---------------------------------------------------------------------------

    async def create(self, ctx: OrgContext, data: StaffCreate, rctx: RequestContext) -> StaffDetail:
        try:
            return await self._create(ctx, data, rctx)
        except IntegrityError as exc:
            mapped = staff_integrity_error(exc)
            if mapped is not None:
                raise mapped from exc
            raise

    async def _create(
        self, ctx: OrgContext, data: StaffCreate, rctx: RequestContext
    ) -> StaffDetail:
        async with unit_of_work(self.session):
            ctx = await lock_and_refresh(self.session, ctx, "member.invite")
            if data.cost_rate is not None or data.charge_rate is not None:
                policy.ensure(ctx, "rate.manage")
            org = ctx.organization
            grade = await self._grade(org.id, data.grade_id)
            role = await self._role(org.id, data.role_id)
            await self._approver(org.id, data.approver_id, own_id=None)
            if data.code is not None:
                if await self.staff.staff_code_taken(org.id, data.code):
                    raise staff_code_taken_error()
                code = data.code
            else:
                code = await next_staff_code(self.session, org.id)
            if data.email and await self.staff.email_taken(org.id, data.email):
                raise staff_email_taken_error()
            await ensure_initials_free(self.members, org.id, data.initials)

            user = await self.users.add(await new_placeholder_user(data.full_name))
            minutes = data.working_minutes_per_day or org.minutes_per_day
            days = data.working_days or sorted(org.working_days)
            target = data.chargeable_target_bp
            if target is None:
                target = grade.default_chargeable_target_bp
            if target is None:
                target = DEFAULT_TARGET_BP
            await self.members.add(
                OrganizationMember(
                    organization_id=org.id,
                    user_id=user.id,
                    role_id=role.id,
                    status="active",
                    manager_user_id=data.approver_id,
                    weekly_capacity_minutes=minutes * len(days),
                    chargeable_target_bp=target,
                    job_title=data.job_title,
                    initials=data.initials,
                    staff_code=code,
                    work_email=data.email,
                    grade_id=grade.id,
                    working_minutes_per_day=minutes,
                    working_days=days,
                    start_date=data.start_date,
                    leaving_date=data.leaving_date,
                    login_enabled=False,
                )
            )
            await record_audit(
                self.session,
                ctx,
                rctx,
                "staff.created",
                "member",
                user.id,
                {
                    "staff_name": user.full_name,
                    "staff_code": code,
                    "grade_id": str(grade.id),
                    "grade_name": grade.name,
                    "start_date": data.start_date.isoformat(),
                },
            )
            # The person's own rates, in the same transaction: nothing is left half done.
            rates = RateRuleService(self.session)
            starts = data.rate_effective_from or data.start_date
            for kind, rate in (("cost", data.cost_rate), ("charge", data.charge_rate)):
                if rate is not None:
                    await rates.write_rule(
                        ctx,
                        rctx,
                        kind=kind,
                        scope=Scope(None, None, user.id, None),
                        rate=rate,
                        start=starts,
                        end=None,
                        audit_action="rate.created",
                        source="staff_rates",
                    )
            result = await self.detail(ctx, await self.fetch_row(ctx, user.id))
        return result

    # --- Update ---------------------------------------------------------------------------

    async def update(
        self, ctx: OrgContext, user_id: uuid.UUID, data: StaffUpdate, rctx: RequestContext
    ) -> StaffDetail:
        try:
            return await self._update(ctx, user_id, data, rctx)
        except IntegrityError as exc:
            mapped = staff_integrity_error(exc)
            if mapped is not None:
                raise mapped from exc
            raise

    async def _update(
        self, ctx: OrgContext, user_id: uuid.UUID, data: StaffUpdate, rctx: RequestContext
    ) -> StaffDetail:
        fields = data.model_fields_set
        async with unit_of_work(self.session):
            ctx = await lock_and_refresh(self.session, ctx, "member.update")
            org_id = ctx.organization_id
            row = await self.fetch_row(ctx, user_id)
            member, user = row.member, row.user
            changes: dict[str, list[Any]] = {}
            values: dict[str, Any] = {}

            if "full_name" in fields and data.full_name != user.full_name:
                # The name lives on the account, which a person in two practices shares.
                if member.login_enabled and await self.members.other_organizations(org_id, user.id):
                    raise ConflictError(
                        "This person also belongs to another practice, so their name can only "
                        "be changed by them.",
                        code="ACCOUNT_SHARED",
                    )
                changes.update(apply_changes(user, {"full_name": data.full_name}))
            if "code" in fields and data.code != member.staff_code:
                assert data.code is not None
                if await self.staff.staff_code_taken(org_id, data.code, exclude_user_id=user.id):
                    raise staff_code_taken_error()
                values["staff_code"] = data.code
            if "email" in fields and data.email != member.work_email:
                if (
                    data.email
                    and member.status == "active"
                    and await self.staff.email_taken(org_id, data.email, exclude_user_id=user.id)
                ):
                    raise staff_email_taken_error()
                values["work_email"] = data.email
            if "grade_id" in fields and data.grade_id != member.grade_id:
                assert data.grade_id is not None
                await self._grade(org_id, data.grade_id)
                values["grade_id"] = data.grade_id
            if "approver_id" in fields and data.approver_id != member.manager_user_id:
                await self._approver(org_id, data.approver_id, own_id=user.id)
                values["manager_user_id"] = data.approver_id
            if "initials" in fields and data.initials != member.initials:
                await ensure_initials_free(
                    self.members, org_id, data.initials, exclude_user_id=user.id
                )
                values["initials"] = data.initials
            for name in (
                "job_title",
                "chargeable_target_bp",
                "overtime_multiplier_bp",
                "start_date",
                "leaving_date",
                "working_minutes_per_day",
                "working_days",
            ):
                if name in fields:
                    values[name] = getattr(data, name)

            start = values.get("start_date", member.start_date)
            leaving = values.get("leaving_date", member.leaving_date)
            if start is not None and leaving is not None and leaving < start:
                raise ValidationError.for_field(
                    "leaving_date", "The leaving date cannot be before the start date."
                )
            if "working_minutes_per_day" in values or "working_days" in values:
                minutes = values.get("working_minutes_per_day", member.working_minutes_per_day)
                days = values.get("working_days", member.working_days)
                values["weekly_capacity_minutes"] = minutes * len(days)

            changes.update(apply_changes(member, values))
            if changes:
                await self.session.flush()
                await record_audit(
                    self.session,
                    ctx,
                    rctx,
                    "staff.updated",
                    "member",
                    user.id,
                    {
                        "staff_name": user.full_name,
                        "staff_code": member.staff_code,
                        "changes": changes,
                    },
                )
            result = await self.detail(ctx, await self.fetch_row(ctx, user_id))
        return result

    # --- Lifecycle ------------------------------------------------------------------------

    async def deactivate(
        self, ctx: OrgContext, user_id: uuid.UUID, data: StaffDeactivate, rctx: RequestContext
    ) -> StaffDetail:
        """Pause (``inactive``) or retire (``left``) a staff member; their records stay."""
        async with unit_of_work(self.session):
            ctx = await lock_and_refresh(self.session, ctx, "member.remove")
            org_id = ctx.organization_id
            row = await self.fetch_row(ctx, user_id)
            member, user = row.member, row.user
            if member.user_id == ctx.user.id:
                raise AuthorizationError("You cannot deactivate yourself.")
            current = normalised_status(member.status)
            if current == "left" and data.status == "inactive":
                raise ConflictError(
                    "This person has left the practice. Reactivate them first.",
                    code="STAFF_LEFT",
                )
            leaving = member.leaving_date
            if data.status == "left":
                leaving = (
                    data.leaving_date or member.leaving_date or today_in(ctx.organization.timezone)
                )
                if member.start_date is not None and leaving < member.start_date:
                    raise ValidationError.for_field(
                        "leaving_date", "The leaving date cannot be before the start date."
                    )
            if current == data.status and leaving == member.leaving_date:
                return await self.detail(ctx, row)
            await self.guard_owner(ctx, row)

            old_status = member.status
            changes = apply_changes(member, {"status": data.status, "leaving_date": leaving})
            cleared = 0
            if data.status == "left":
                cleared = await self.members.clear_manager(org_id, user.id)
            await self.session.flush()
            if member.login_enabled and not await self.members.other_organizations(org_id, user.id):
                await self.sessions.revoke_all(user.id, "admin")
            await record_audit(
                self.session,
                ctx,
                rctx,
                "staff.deactivated" if data.status == "inactive" else "staff.left",
                "member",
                user.id,
                {
                    "staff_name": user.full_name,
                    "staff_code": member.staff_code,
                    "old": {"status": old_status},
                    "new": {"status": data.status},
                    "approver_links_cleared": cleared,
                    "changes": changes,
                },
            )
            result = await self.detail(ctx, await self.fetch_row(ctx, user_id))
        return result

    async def reactivate(
        self, ctx: OrgContext, user_id: uuid.UUID, rctx: RequestContext
    ) -> StaffDetail:
        """Return an inactive or departed staff member to active and clear the leaving date."""
        try:
            return await self._reactivate(ctx, user_id, rctx)
        except IntegrityError as exc:
            mapped = staff_integrity_error(exc)
            if mapped is not None:
                raise mapped from exc
            raise

    async def _reactivate(
        self, ctx: OrgContext, user_id: uuid.UUID, rctx: RequestContext
    ) -> StaffDetail:
        async with unit_of_work(self.session):
            ctx = await lock_and_refresh(self.session, ctx, "member.update")
            org_id = ctx.organization_id
            row = await self.fetch_row(ctx, user_id)
            member, user = row.member, row.user
            if row.role.system_key == OWNER_KEY and not policy.is_owner(ctx):
                raise AuthorizationError("Only an owner can change another owner.")
            if member.status == "active" and member.leaving_date is None:
                return await self.detail(ctx, row)
            if member.work_email and await self.staff.email_taken(
                org_id, member.work_email, exclude_user_id=user.id
            ):
                raise staff_email_taken_error()
            if member.status == "removed":
                await ensure_initials_free(
                    self.members, org_id, member.initials, exclude_user_id=user.id
                )
            old_status = member.status
            changes = apply_changes(member, {"status": "active", "leaving_date": None})
            await self.session.flush()
            await record_audit(
                self.session,
                ctx,
                rctx,
                "staff.reactivated",
                "member",
                user.id,
                {
                    "staff_name": user.full_name,
                    "staff_code": member.staff_code,
                    "old": {"status": old_status},
                    "new": {"status": "active"},
                    "changes": changes,
                },
            )
            result = await self.detail(ctx, await self.fetch_row(ctx, user_id))
        return result

    async def delete(self, ctx: OrgContext, user_id: uuid.UUID, rctx: RequestContext) -> None:
        """Delete a staff record that nothing refers to (409 ``STAFF_IN_USE`` otherwise)."""
        async with unit_of_work(self.session):
            ctx = await lock_and_refresh(self.session, ctx, "member.remove")
            org_id = ctx.organization_id
            row = await self.fetch_row(ctx, user_id)
            member, user = row.member, row.user
            if member.user_id == ctx.user.id:
                raise AuthorizationError("You cannot delete yourself.")
            await self.guard_owner(ctx, row)
            counts = await staff_references.usage(self.session, org_id, user.id)
            if counts:
                raise ConflictError(
                    "This person has time, expenses, invoices or other records, so they cannot "
                    "be deleted. Deactivate them instead.",
                    code="STAFF_IN_USE",
                    details={"usage": [u.model_dump() for u in usage_rows(counts)]},
                )
            meta = {
                "staff_name": user.full_name,
                "staff_code": member.staff_code,
                "email": member.work_email,
            }
            login = member.login_enabled
            await self.members.clear_manager(org_id, user.id)
            await self.session.execute(
                delete(OrganizationMember).where(
                    OrganizationMember.organization_id == org_id,
                    OrganizationMember.user_id == user.id,
                )
            )
            if not login:
                await self.session.execute(delete(User).where(User.id == user.id))
            elif not await self.members.other_organizations(org_id, user.id):
                await self.sessions.revoke_all(user.id, "admin")
                user.deleted_at = datetime.now(UTC)
                user.is_active = False
            await self.session.flush()
            await record_audit(self.session, ctx, rctx, "staff.deleted", "member", user.id, meta)

    # --- Internals ------------------------------------------------------------------------

    async def _grade(self, org_id: uuid.UUID, grade_id: uuid.UUID) -> Grade:
        grade = await self.grades.get(org_id, grade_id)
        if grade is None:
            raise ValidationError.for_field("grade_id", "This grade does not exist.")
        if not grade.is_active:
            raise ValidationError.for_field("grade_id", "This grade is inactive.")
        return grade

    async def _role(self, org_id: uuid.UUID, role_id: uuid.UUID | None) -> Role:
        role = (
            await self.roles.get(org_id, role_id)
            if role_id is not None
            else await self.roles.get_by_system_key(org_id, "staff")
        )
        if role is None:
            raise ValidationError.for_field("role_id", "This role does not exist.")
        return role

    async def _approver(
        self, org_id: uuid.UUID, approver_id: uuid.UUID | None, *, own_id: uuid.UUID | None
    ) -> None:
        if approver_id is None:
            return
        if approver_id == own_id:
            raise ValidationError.for_field(
                "approver_id", "Staff cannot approve their own timesheets."
            )
        approver = await self.members.get(org_id, approver_id)
        if approver is None or approver.status != "active" or not approver.login_enabled:
            raise ValidationError.for_field(
                "approver_id", "Choose an active staff member who can sign in."
            )

    async def guard_owner(self, ctx: OrgContext, row: StaffRow) -> None:
        """The practice keeps an owner who can sign in, and only an owner changes an owner."""
        if row.role.system_key != OWNER_KEY:
            return
        if row.member.status == "active" and row.member.login_enabled:
            await ensure_not_last_owner(self.members, ctx.organization_id)
        if not policy.is_owner(ctx):
            raise AuthorizationError("Only an owner can change another owner.")
