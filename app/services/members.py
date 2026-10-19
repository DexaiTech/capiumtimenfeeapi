"""Staff of a practice (organization members): listing, changes, removal and leaving."""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import AuthorizationError, ConflictError, NotFoundError, ValidationError
from app.core.permissions import OWNER_KEY
from app.db.models import OrganizationMember, Role
from app.db.models.organization import GONE_STATUSES
from app.db.session import unit_of_work
from app.repositories.members import MemberRepository
from app.repositories.roles import RoleRepository
from app.schemas.organization import MemberUpdate
from app.services import audit, policy
from app.services.organizations import lock_and_refresh
from app.services.policy import OrgContext
from app.utils.pagination import Page, PageParams

DEFAULT_STATUSES: tuple[str, ...] = ("active", "suspended", "inactive")


def _role_meta(role: Role) -> dict[str, Any]:
    return {"role_id": str(role.id), "role_name": role.name, "system_key": role.system_key}


INITIALS_INDEX = "uq_organization_members_organization_id_initials"


def integrity_constraint(exc: IntegrityError) -> str | None:
    """The name of the violated constraint, from the asyncpg error SQLAlchemy wraps."""
    cause = exc.orig.__cause__ if exc.orig is not None else None
    name = getattr(cause, "constraint_name", None)
    return name if isinstance(name, str) else None


def initials_taken_error() -> ConflictError:
    message = "These initials are already used by someone else in the practice."
    return ConflictError(
        message,
        code="INITIALS_TAKEN",
        details=[{"field": "initials", "code": "duplicate", "message": message}],
    )


async def ensure_initials_free(
    members: MemberRepository,
    organization_id: uuid.UUID,
    initials: str | None,
    *,
    exclude_user_id: uuid.UUID | None = None,
) -> None:
    """Initials are unique (case-insensitively) among the practice's non-removed staff.

    Call with the practice row lock held (``lock_and_refresh``): every change to members
    takes it, so the check cannot be raced. The unique index is the backstop.
    """
    if initials is not None and await members.initials_taken(
        organization_id, initials, exclude_user_id=exclude_user_id
    ):
        raise initials_taken_error()


async def ensure_not_last_owner(members: MemberRepository, organization_id: uuid.UUID) -> None:
    """Call with the organization lock held, for a target that is an active Owner who can
    sign in: the practice must keep another one."""
    if await members.count_active_owners(organization_id) <= 1:
        raise ConflictError(
            "A practice must keep at least one active owner.",
            details={"code": "last_owner"},
        )


class MemberService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.members = MemberRepository(session)
        self.roles = RoleRepository(session)

    async def list_members(
        self,
        ctx: OrgContext,
        *,
        statuses: Sequence[str] | None,
        role_id: uuid.UUID | None,
        q: str | None,
        params: PageParams,
    ) -> Page[OrganizationMember]:
        return await self.members.list_page(
            ctx.organization_id,
            statuses=statuses or DEFAULT_STATUSES,
            role_id=role_id,
            q=q.strip() if q else None,
            params=params,
        )

    async def get(self, ctx: OrgContext, user_id: uuid.UUID) -> OrganizationMember:
        member = await self.members.get(ctx.organization_id, user_id)
        if member is None:
            raise NotFoundError("Member not found.")
        return member

    async def update(
        self, ctx: OrgContext, user_id: uuid.UUID, data: MemberUpdate, rctx: RequestContext
    ) -> OrganizationMember:
        try:
            return await self._update(ctx, user_id, data, rctx)
        except IntegrityError as exc:
            # The unique index backs up the locked check: report it the same way.
            if integrity_constraint(exc) == INITIALS_INDEX:
                raise initials_taken_error() from exc
            raise

    async def _update(
        self, ctx: OrgContext, user_id: uuid.UUID, data: MemberUpdate, rctx: RequestContext
    ) -> OrganizationMember:
        fields = data.model_fields_set
        async with unit_of_work(self.session):
            ctx = await lock_and_refresh(self.session, ctx, "member.update")
            target = await self.members.get(ctx.organization_id, user_id)
            if target is None or target.status in GONE_STATUSES:
                raise NotFoundError("Member not found.")

            old_role = target.role
            new_role = old_role
            if "role_id" in fields and data.role_id != target.role_id:
                assert data.role_id is not None
                found = await self.roles.get(ctx.organization_id, data.role_id)
                if found is None:
                    raise ValidationError.for_field("role_id", "This role does not exist.")
                new_role = found
            old_status = target.status
            new_status = data.status if "status" in fields and data.status else old_status
            role_changed = new_role.id != old_role.id
            status_changed = new_status != old_status

            if (role_changed or status_changed) and target.user_id == ctx.user.id:
                raise AuthorizationError("You cannot change your own role or status.")

            was_active_owner = old_role.system_key == OWNER_KEY and old_status == "active"
            stays_active_owner = new_role.system_key == OWNER_KEY and new_status == "active"
            if was_active_owner and not stays_active_owner:
                await self._ensure_not_last_owner(ctx)
            if (
                (role_changed or status_changed)
                and old_role.system_key == OWNER_KEY
                and not policy.is_owner(ctx)
            ):
                raise AuthorizationError("Only an owner can change another owner.")
            if role_changed:
                policy.ensure_can_assign_role(
                    ctx, new_role.system_key, (p.code for p in new_role.permissions)
                )

            other_changes: dict[str, list[Any]] = {}
            if "manager_user_id" in fields and data.manager_user_id != target.manager_user_id:
                await self._validate_manager(ctx, target, data.manager_user_id)
                other_changes["manager_user_id"] = [
                    str(target.manager_user_id) if target.manager_user_id else None,
                    str(data.manager_user_id) if data.manager_user_id else None,
                ]
                target.manager_user_id = data.manager_user_id
            if (
                "weekly_capacity_minutes" in fields
                and data.weekly_capacity_minutes is not None
                and data.weekly_capacity_minutes != target.weekly_capacity_minutes
            ):
                other_changes["weekly_capacity_minutes"] = [
                    target.weekly_capacity_minutes,
                    data.weekly_capacity_minutes,
                ]
                target.weekly_capacity_minutes = data.weekly_capacity_minutes
            if (
                "chargeable_target_bp" in fields
                and data.chargeable_target_bp is not None
                and data.chargeable_target_bp != target.chargeable_target_bp
            ):
                other_changes["chargeable_target_bp"] = [
                    target.chargeable_target_bp,
                    data.chargeable_target_bp,
                ]
                target.chargeable_target_bp = data.chargeable_target_bp
            if "initials" in fields and data.initials != target.initials:
                await ensure_initials_free(
                    self.members, ctx.organization_id, data.initials, exclude_user_id=user_id
                )
            for field in ("job_title", "initials"):
                if field in fields and getattr(data, field) != getattr(target, field):
                    other_changes[field] = [getattr(target, field), getattr(data, field)]
                    setattr(target, field, getattr(data, field))

            if role_changed:
                target.role_id = new_role.id
                await self._audit(
                    ctx,
                    rctx,
                    "member.role_changed",
                    target,
                    {"old": _role_meta(old_role), "new": _role_meta(new_role)},
                )

            if status_changed:
                target.status = new_status
                await self._audit(
                    ctx,
                    rctx,
                    "member.suspended" if new_status == "suspended" else "member.reactivated",
                    target,
                    {"old": {"status": old_status}, "new": {"status": new_status}},
                )

            if other_changes:
                await self._audit(ctx, rctx, "member.updated", target, {"changes": other_changes})

            await self.session.flush()
            # ``role`` is a view-only relationship: re-read so the response shows the new role.
            updated = await self.members.get(ctx.organization_id, user_id)
            assert updated is not None
        return updated

    async def remove(self, ctx: OrgContext, user_id: uuid.UUID, rctx: RequestContext) -> None:
        """Remove a member, or leave the organization when ``user_id`` is the caller."""
        is_self = user_id == ctx.user.id
        async with unit_of_work(self.session):
            ctx = await lock_and_refresh(
                self.session, ctx, *(() if is_self else ("member.remove",))
            )
            target = await self.members.get(ctx.organization_id, user_id)
            if target is None or target.status in GONE_STATUSES:
                raise NotFoundError("Member not found.")
            if target.role.system_key == OWNER_KEY:
                if target.status == "active":
                    await self._ensure_not_last_owner(ctx)
                if not is_self and not policy.is_owner(ctx):
                    raise AuthorizationError("Only an owner can remove another owner.")

            old_status = target.status
            target.status = "removed"
            target.manager_user_id = None
            await self.session.flush()
            # Nobody keeps reporting to someone who is gone. Their time, rates and invoices
            # stay as they are: the financial history must not change.
            await self.members.clear_manager(ctx.organization_id, target.user_id)
            await self._audit(
                ctx,
                rctx,
                "member.removed",
                target,
                {
                    "old": {"status": old_status, **_role_meta(target.role)},
                    "new": {"status": "removed"},
                    "left": is_self,
                },
            )

    async def _ensure_not_last_owner(self, ctx: OrgContext) -> None:
        """Call with the organization lock held, for a target that is an active Owner."""
        await ensure_not_last_owner(self.members, ctx.organization_id)

    async def _validate_manager(
        self, ctx: OrgContext, target: OrganizationMember, manager_user_id: uuid.UUID | None
    ) -> None:
        if manager_user_id is None:
            return
        if manager_user_id == target.user_id:
            raise ValidationError.for_field(
                "manager_user_id", "A member cannot be their own manager."
            )
        manager = await self.members.get(ctx.organization_id, manager_user_id)
        if manager is None or manager.status != "active":
            raise ValidationError.for_field(
                "manager_user_id", "The manager must be an active member of this practice."
            )

    async def _audit(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        action: str,
        target: OrganizationMember,
        meta: dict[str, Any],
    ) -> None:
        await audit.record(
            self.session,
            action,
            ctx=rctx,
            actor_user_id=ctx.user.id,
            organization_id=ctx.organization_id,
            target_type="member",
            target_id=target.user_id,
            # The name is recorded as it was at the time, so the log stays readable later.
            meta={"member_name": target.user.full_name, "member_email": target.user.email, **meta},
        )
