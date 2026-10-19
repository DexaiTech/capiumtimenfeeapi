"""People created by an administrator: accounts with a temporary password.

In a closed deployment nobody signs up or accepts an invitation; an administrator makes the
account and hands over a generated password that must be changed at first sign-in. The
password is returned once and is never logged, audited or stored in a job payload.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.security import generate_password, generate_token, hash_password, hash_token
from app.db.models import OrganizationMember, PasswordResetToken, User
from app.db.models.organization import GONE_STATUSES
from app.db.session import unit_of_work
from app.repositories.auth_tokens import AuthTokenRepository
from app.repositories.members import MemberRepository
from app.repositories.roles import RoleRepository
from app.repositories.staff import StaffRepository
from app.repositories.users import UserRepository
from app.schemas.organization import PersonCreate
from app.services import audit, jobs, policy
from app.services.members import ensure_initials_free, integrity_constraint
from app.services.organizations import lock_and_refresh
from app.services.policy import OrgContext
from app.services.sessions import SessionService
from app.services.staff_codes import next_staff_code
from app.services.staff_common import staff_email_taken_error, staff_integrity_error
from app.utils.ids import new_id

# How long the emailed link for choosing a password stays valid.
SETUP_TTL = timedelta(days=7)


async def queue_password_link(
    session: AsyncSession,
    user: User,
    *,
    template: str,
    context: dict[str, str],
    organization_id: uuid.UUID,
) -> datetime:
    """Email a one-time link for choosing a password through the job queue (the link, never a
    password, is in the message). Returns when the link expires. The practice's own version of
    the template, if it customised one, is the one sent."""
    raw = generate_token()
    expires_at = datetime.now(UTC) + SETUP_TTL
    await AuthTokenRepository(session).add(
        PasswordResetToken(
            id=new_id(), user_id=user.id, token_hash=hash_token(raw), expires_at=expires_at
        )
    )
    await jobs.enqueue(
        session,
        "send_email",
        {
            "template": template,
            "organization_id": str(organization_id),
            "to": user.email,
            "name": user.full_name,
            "token": raw,
            "context": context,
        },
    )
    return expires_at


class PeopleService:
    def __init__(self, session: AsyncSession, sessions: SessionService) -> None:
        self.session = session
        self.sessions = sessions
        self.users = UserRepository(session)
        self.members = MemberRepository(session)
        self.roles = RoleRepository(session)

    async def create(
        self, ctx: OrgContext, data: PersonCreate, rctx: RequestContext
    ) -> tuple[OrganizationMember, str, bool]:
        """Create a verified account with a generated password and the membership.

        Returns the member, the temporary password (shown to the administrator once) and
        whether a "choose your password" email was queued. That email carries a one-time
        link, never the password itself.
        """
        password = generate_password()
        password_hash = await hash_password(password)
        try:
            return await self._create(ctx, data, rctx, password, password_hash)
        except IntegrityError as exc:
            # Lost a race with another creation of the same address (another practice's
            # request: within one practice the row lock serialises creations).
            constraint = integrity_constraint(exc)
            if constraint == "uq_users_email_lower":
                raise _email_taken() from exc
            mapped = staff_integrity_error(exc)
            if mapped is not None:
                raise mapped from exc
            raise

    async def _create(
        self,
        ctx: OrgContext,
        data: PersonCreate,
        rctx: RequestContext,
        password: str,
        password_hash: str,
    ) -> tuple[OrganizationMember, str, bool]:
        async with unit_of_work(self.session):
            ctx = await lock_and_refresh(self.session, ctx, "member.invite")
            role = await self.roles.get(ctx.organization_id, data.role_id)
            if role is None:
                raise ValidationError.for_field("role_id", "This role does not exist.")
            policy.ensure_can_assign_role(ctx, role.system_key, (p.code for p in role.permissions))
            if data.manager_user_id is not None:
                manager = await self.members.get(ctx.organization_id, data.manager_user_id)
                if manager is None or manager.status != "active":
                    raise ValidationError.for_field(
                        "manager_user_id",
                        "The manager must be an active member of this practice.",
                    )
            await ensure_initials_free(self.members, ctx.organization_id, data.initials)
            if await self.users.get_by_email(data.email) is not None:
                raise _email_taken()
            if await StaffRepository(self.session).email_taken(ctx.organization_id, data.email):
                raise staff_email_taken_error()
            user = await self.users.add(
                User(
                    id=new_id(),
                    email=data.email,
                    full_name=data.full_name,
                    password_hash=password_hash,
                    email_verified_at=datetime.now(UTC),
                    must_change_password=True,
                )
            )
            await self.members.add(
                OrganizationMember(
                    organization_id=ctx.organization_id,
                    user_id=user.id,
                    role_id=role.id,
                    job_title=data.job_title,
                    initials=data.initials,
                    manager_user_id=data.manager_user_id,
                    weekly_capacity_minutes=data.weekly_capacity_minutes,
                    staff_code=await next_staff_code(self.session, ctx.organization_id),
                    work_email=data.email,
                )
            )
            if data.send_email:
                await self._queue_setup_email(ctx, user)
            member = await self.members.get(ctx.organization_id, user.id)
            assert member is not None
            await audit.record(
                self.session,
                "member.created",
                ctx=rctx,
                actor_user_id=ctx.user.id,
                organization_id=ctx.organization_id,
                target_type="member",
                target_id=user.id,
                # Never the password: the log must be safe to show to anyone who reads it.
                meta={
                    "member_name": user.full_name,
                    "member_email": user.email,
                    "role_id": str(role.id),
                    "role_name": role.name,
                },
            )
        return member, password, data.send_email

    async def _queue_setup_email(self, ctx: OrgContext, user: User) -> None:
        """Email a one-time link for choosing a password (valid for a week)."""
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

    async def reset_password(
        self, ctx: OrgContext, user_id: uuid.UUID, rctx: RequestContext
    ) -> str:
        """Give the person a new generated password and sign them out everywhere."""
        if user_id == ctx.user.id:
            raise ConflictError(
                "Use Change password for your own account.", code="USE_CHANGE_PASSWORD"
            )
        password = generate_password()
        password_hash = await hash_password(password)
        async with unit_of_work(self.session):
            ctx = await lock_and_refresh(self.session, ctx, "member.update")
            target = await self.members.get(ctx.organization_id, user_id)
            if target is None or target.status in GONE_STATUSES:
                raise NotFoundError("Member not found.")
            # Whoever holds the password holds the account, so this is as strong as handing
            # out the role: only an Owner touches an Owner, and nobody a role above them.
            policy.ensure_can_assign_role(
                ctx, target.role.system_key, (p.code for p in target.role.permissions)
            )
            if await self.members.other_organizations(ctx.organization_id, user_id):
                raise ConflictError(
                    "This person also belongs to another practice. "
                    "They must reset their own password.",
                    code="ACCOUNT_SHARED",
                )
            user = target.user
            now = datetime.now(UTC)
            user.password_hash = password_hash
            user.password_changed_at = now
            user.must_change_password = True
            await self.session.flush()
            await AuthTokenRepository(self.session).invalidate_for_user(
                PasswordResetToken, user.id, now
            )
            await self.sessions.revoke_all(user.id, "admin")
            await jobs.enqueue(
                self.session,
                "send_email",
                {"template": "password_changed", "to": user.email, "name": user.full_name},
            )
            await audit.record(
                self.session,
                "member.password_reset",
                ctx=rctx,
                actor_user_id=ctx.user.id,
                organization_id=ctx.organization_id,
                target_type="member",
                target_id=user.id,
                meta={"member_name": user.full_name, "member_email": user.email},
            )
        return password


def _email_taken() -> ConflictError:
    message = "This email address already belongs to an account."
    return ConflictError(
        message,
        code="EMAIL_TAKEN",
        details=[{"field": "email", "code": "duplicate", "message": message}],
    )
