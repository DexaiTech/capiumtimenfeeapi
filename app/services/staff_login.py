"""Login access of staff: invite, link an account, unlink it (docs/modules/team-rates.md).

* ``invite``: turns a staff member without a login into a pending login and emails a one-time
  link (valid 7 days) for choosing a password; sending again re-issues the link. The person is
  ``invited`` until they choose a password.
* ``link_login`` / ``create_account``: makes the account straight away with a temporary
  password, returned once, that must be changed at first sign-in.
* ``link_login`` / ``existing_user``: the record is merged into the practice member who already
  signs in with the given address: their time, rates and jobs move to that member, who takes
  over the staff profile (code, grade, hours, dates).
* ``unlink_login``: the person keeps their record and history but can no longer sign in.

Granting access needs ``member.invite`` and the right to hand out the role; taking it away
needs ``member.remove``. Every change takes the practice row lock.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import AuthorizationError, ConflictError, ValidationError
from app.core.security import generate_password, hash_password
from app.db.models import OrganizationMember, PasswordResetToken, Role, User
from app.db.session import unit_of_work
from app.repositories import staff_references
from app.repositories.auth_tokens import AuthTokenRepository
from app.repositories.members import MemberRepository
from app.repositories.roles import RoleRepository
from app.repositories.staff import StaffRepository, StaffRow
from app.repositories.users import UserRepository
from app.schemas.staff import (
    StaffDetail,
    StaffInvite,
    StaffInvited,
    StaffLinked,
    StaffLinkLogin,
    login_status,
)
from app.services import policy
from app.services.common import record_audit
from app.services.members import integrity_constraint
from app.services.organizations import lock_and_refresh
from app.services.people import queue_password_link
from app.services.policy import OrgContext
from app.services.sessions import SessionService
from app.services.staff import StaffService
from app.services.staff_common import (
    placeholder_email,
    staff_email_taken_error,
    staff_integrity_error,
    unusable_password_hash,
)

# What the login member takes over from the record that is merged into it.
_PROFILE = (
    "staff_code",
    "work_email",
    "grade_id",
    "working_minutes_per_day",
    "working_days",
    "weekly_capacity_minutes",
    "chargeable_target_bp",
    "start_date",
    "leaving_date",
)


def _email_taken() -> ConflictError:
    message = "This email address already belongs to an account."
    return ConflictError(
        message,
        code="EMAIL_TAKEN",
        details=[{"field": "email", "code": "duplicate", "message": message}],
    )


class StaffLoginService:
    def __init__(self, session: AsyncSession, sessions: SessionService) -> None:
        self.session = session
        self.sessions = sessions
        self.staff = StaffService(session, sessions)
        self.directory = StaffRepository(session)
        self.members = MemberRepository(session)
        self.roles = RoleRepository(session)
        self.users = UserRepository(session)

    # --- Link -----------------------------------------------------------------------------

    async def link_login(
        self, ctx: OrgContext, user_id: uuid.UUID, data: StaffLinkLogin, rctx: RequestContext
    ) -> StaffLinked:
        try:
            if data.mode == "create_account":
                return await self._create_account(ctx, user_id, data, rctx)
            return await self._merge(ctx, user_id, data, rctx)
        except IntegrityError as exc:
            if integrity_constraint(exc) == "uq_users_email_lower":
                raise _email_taken() from exc
            mapped = staff_integrity_error(exc)
            if mapped is not None:
                raise mapped from exc
            raise

    async def _create_account(
        self, ctx: OrgContext, user_id: uuid.UUID, data: StaffLinkLogin, rctx: RequestContext
    ) -> StaffLinked:
        password = generate_password()
        password_hash = await hash_password(password)
        async with unit_of_work(self.session):
            ctx = await lock_and_refresh(self.session, ctx, "member.invite")
            row = await self._without_login(ctx, user_id)
            member, user = row.member, row.user
            address = await self._login_address(ctx, row, data.email)
            role = await self._role_for_login(ctx, member, data.role_id)
            now = datetime.now(UTC)
            user.email = address
            user.password_hash = password_hash
            user.is_active = True
            user.email_verified_at = now
            user.must_change_password = True
            user.password_changed_at = now
            member.login_enabled = True
            member.invited_at = None
            member.role_id = role.id
            member.work_email = member.work_email or address
            await self.session.flush()
            if data.send_email:
                await queue_password_link(
                    self.session,
                    user,
                    template="account_created",
                    organization_id=ctx.organization_id,
                    context={
                        "organization_name": ctx.organization.name,
                        "added_by": ctx.user.full_name,
                    },
                )
            await record_audit(
                self.session,
                ctx,
                rctx,
                "staff.login_linked",
                "member",
                user.id,
                {
                    "staff_name": user.full_name,
                    "staff_code": member.staff_code,
                    "mode": "create_account",
                    "login_email": address,
                    "role_name": role.name,
                },
            )
            detail = await self.staff.detail(ctx, await self.staff.fetch_row(ctx, user_id))
        return StaffLinked(
            staff=detail,
            temporary_password=password,
            email_sent=data.send_email,
            merged_from=None,
        )

    async def _merge(
        self, ctx: OrgContext, user_id: uuid.UUID, data: StaffLinkLogin, rctx: RequestContext
    ) -> StaffLinked:
        assert data.email is not None
        async with unit_of_work(self.session):
            ctx = await lock_and_refresh(self.session, ctx, "member.invite")
            org_id = ctx.organization_id
            row = await self._without_login(ctx, user_id)
            source, source_user = row.member, row.user
            target = await self.directory.login_member_by_email(org_id, data.email)
            if target is None or target.user_id == source.user_id:
                raise ValidationError.for_field(
                    "email", "No active staff member of this practice signs in with this address."
                )
            target_user = await self.users.get(target.user_id)
            assert target_user is not None
            source_id = source.user_id
            profile: dict[str, Any] = {name: getattr(source, name) for name in _PROFILE}
            job_title = target.job_title or source.job_title
            initials = target.initials or source.initials
            approver = target.manager_user_id or source.manager_user_id
            counts = await staff_references.usage(self.session, org_id, source_id)

            if target.manager_user_id == source_id:
                target.manager_user_id = None
                await self.session.flush()
            try:
                async with self.session.begin_nested():
                    await staff_references.reassign(self.session, org_id, source_id, target.user_id)
            except staff_references.ReferenceConflict as exc:
                raise ConflictError(
                    "Both records have the same kind of record that cannot be combined "
                    f"({exc.table.replace('_', ' ')}). Remove one of them first.",
                    code="LINK_CONFLICT",
                    details={"table": exc.table},
                ) from exc

            # The placeholder goes first: its code, email and initials are unique.
            await self.session.execute(
                delete(OrganizationMember).where(
                    OrganizationMember.organization_id == org_id,
                    OrganizationMember.user_id == source_id,
                )
            )
            await self.session.execute(delete(User).where(User.id == source_id))
            await self.session.flush()
            for name, value in profile.items():
                setattr(target, name, value)
            target.job_title = job_title
            target.initials = initials
            target.manager_user_id = approver if approver != target.user_id else None
            await self.session.flush()
            await record_audit(
                self.session,
                ctx,
                rctx,
                "staff.login_linked",
                "member",
                target.user_id,
                {
                    "staff_name": target_user.full_name,
                    "staff_code": target.staff_code,
                    "mode": "existing_user",
                    "login_email": target_user.email,
                    "merged_from": str(source_id),
                    "merged_name": source_user.full_name,
                    "moved": counts,
                },
            )
            detail = await self.staff.detail(ctx, await self.staff.fetch_row(ctx, target.user_id))
        return StaffLinked(
            staff=detail, temporary_password=None, email_sent=False, merged_from=source_id
        )

    # --- Invite ---------------------------------------------------------------------------

    async def invite(
        self, ctx: OrgContext, user_id: uuid.UUID, data: StaffInvite, rctx: RequestContext
    ) -> StaffInvited:
        try:
            return await self._invite(ctx, user_id, data, rctx)
        except IntegrityError as exc:
            if integrity_constraint(exc) == "uq_users_email_lower":
                raise _email_taken() from exc
            mapped = staff_integrity_error(exc)
            if mapped is not None:
                raise mapped from exc
            raise

    async def _invite(
        self, ctx: OrgContext, user_id: uuid.UUID, data: StaffInvite, rctx: RequestContext
    ) -> StaffInvited:
        async with unit_of_work(self.session):
            ctx = await lock_and_refresh(self.session, ctx, "member.invite")
            row = await self.staff.fetch_row(ctx, user_id)
            member, user = row.member, row.user
            self._ensure_active(member)
            now = datetime.now(UTC)
            resent = member.login_enabled
            if resent:
                if login_status(row) == "active":
                    raise ConflictError(
                        "This person already has an active login.", code="LOGIN_ALREADY_ACTIVE"
                    )
            else:
                address = await self._login_address(ctx, row, data.email)
                role = await self._role_for_login(ctx, member, data.role_id)
                user.email = address
                user.password_hash = await unusable_password_hash()
                user.is_active = True
                user.email_verified_at = now
                user.must_change_password = False
                user.password_changed_at = now
                member.login_enabled = True
                member.role_id = role.id
                member.work_email = member.work_email or address
            member.invited_at = now
            await self.session.flush()
            await AuthTokenRepository(self.session).invalidate_for_user(
                PasswordResetToken, user.id, now
            )
            expires_at = await queue_password_link(
                self.session,
                user,
                template="staff_invitation",
                organization_id=ctx.organization_id,
                context={
                    "organization_name": ctx.organization.name,
                    "invited_by": ctx.user.full_name,
                },
            )
            await record_audit(
                self.session,
                ctx,
                rctx,
                "staff.invited",
                "member",
                user.id,
                {
                    "staff_name": user.full_name,
                    "staff_code": member.staff_code,
                    "login_email": user.email,
                    "resent": resent,
                },
            )
            detail = await self.staff.detail(ctx, await self.staff.fetch_row(ctx, user_id))
        return StaffInvited(staff=detail, invited_at=now, expires_at=expires_at, resent=resent)

    # --- Unlink ---------------------------------------------------------------------------

    async def unlink_login(
        self, ctx: OrgContext, user_id: uuid.UUID, rctx: RequestContext
    ) -> StaffDetail:
        async with unit_of_work(self.session):
            ctx = await lock_and_refresh(self.session, ctx, "member.remove")
            org_id = ctx.organization_id
            row = await self.staff.fetch_row(ctx, user_id)
            member, user = row.member, row.user
            if not member.login_enabled:
                raise ConflictError("This person has no login.", code="NO_LOGIN")
            if member.user_id == ctx.user.id:
                raise AuthorizationError("You cannot remove your own login.")
            await self.staff.guard_owner(ctx, row)
            if await self.members.other_organizations(org_id, user.id):
                raise ConflictError(
                    "This person also belongs to another practice, so their account cannot be "
                    "changed here.",
                    code="ACCOUNT_SHARED",
                )
            now = datetime.now(UTC)
            old_email = user.email
            await self.sessions.revoke_all(user.id, "admin")
            await AuthTokenRepository(self.session).invalidate_for_user(
                PasswordResetToken, user.id, now
            )
            user.email = placeholder_email(user.id)
            user.password_hash = await unusable_password_hash()
            user.is_active = False
            user.must_change_password = False
            member.login_enabled = False
            member.invited_at = None
            if member.work_email is None and not await self.directory.email_taken(
                org_id, old_email, exclude_user_id=user.id
            ):
                member.work_email = old_email
            cleared = await self.members.clear_manager(org_id, user.id)
            await self.session.flush()
            await record_audit(
                self.session,
                ctx,
                rctx,
                "staff.login_unlinked",
                "member",
                user.id,
                {
                    "staff_name": user.full_name,
                    "staff_code": member.staff_code,
                    "login_email": old_email,
                    "approver_links_cleared": cleared,
                },
            )
            detail = await self.staff.detail(ctx, await self.staff.fetch_row(ctx, user_id))
        return detail

    # --- Internals ------------------------------------------------------------------------

    @staticmethod
    def _ensure_active(member: OrganizationMember) -> None:
        if member.status != "active":
            raise ConflictError(
                "Only active staff can be given a login. Reactivate them first.",
                code="STAFF_NOT_ACTIVE",
            )

    async def _without_login(self, ctx: OrgContext, user_id: uuid.UUID) -> StaffRow:
        row = await self.staff.fetch_row(ctx, user_id)
        if row.member.login_enabled:
            raise ConflictError("This person already has a login.", code="LOGIN_ALREADY_LINKED")
        self._ensure_active(row.member)
        return row

    async def _login_address(self, ctx: OrgContext, row: StaffRow, email: str | None) -> str:
        """The sign-in address for someone who has none: free as an account and among staff."""
        address = email or row.member.work_email
        if not address:
            raise ValidationError.for_field(
                "email", "An email address is needed to give this person a login.", "required"
            )
        if await self.users.get_by_email(address) is not None:
            raise _email_taken()
        if await self.directory.email_taken(
            ctx.organization_id, address, exclude_user_id=row.member.user_id
        ):
            raise staff_email_taken_error()
        return address

    async def _role_for_login(
        self, ctx: OrgContext, member: OrganizationMember, role_id: uuid.UUID | None
    ) -> Role:
        """The role the new login gets; the caller must be allowed to hand it out."""
        role = await self.roles.get(ctx.organization_id, role_id or member.role_id)
        if role is None:
            raise ValidationError.for_field("role_id", "This role does not exist.")
        policy.ensure_can_assign_role(ctx, role.system_key, (p.code for p in role.permissions))
        return role
