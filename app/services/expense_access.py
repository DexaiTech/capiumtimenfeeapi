"""Who may see, change and decide on which expense claims (docs/modules/expenses.md, "Access").

* Anyone with ``expense.read_all`` or ``expense.manage`` sees every claim; others see their own,
  and with ``expense.approve`` also the claims of the people they manage.
* A claim is written by its claimant with ``expense.log``, and by anyone with ``expense.manage``
  (for staff without a login, for instance).
* A claim is decided (approved or returned) with ``expense.approve`` by the claimant's manager or
  by anyone with ``expense.read_all``; nobody decides on their own claim unless they hold
  ``organization.update``.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthorizationError
from app.db.models import ExpenseClaim
from app.repositories.time_refs import TimeReferenceRepository
from app.schemas.expenses import ClaimActions
from app.services import policy
from app.services.expense_rules import SelfApprovalError
from app.services.policy import OrgContext

MODULE_CODES = ("expense.log", "expense.read_all", "expense.manage", "expense.approve")
EDITABLE_STATUSES = ("draft", "returned")


def ensure_module_access(ctx: OrgContext) -> None:
    policy.ensure_any(ctx, *MODULE_CODES)


def sees_all(ctx: OrgContext) -> bool:
    return policy.has_permission(ctx, "expense.read_all") or policy.has_permission(
        ctx, "expense.manage"
    )


def can_write(ctx: OrgContext, claimant_id: uuid.UUID) -> bool:
    """The claimant with ``expense.log``, or anyone with ``expense.manage``."""
    if policy.has_permission(ctx, "expense.manage"):
        return True
    return claimant_id == ctx.user.id and policy.has_permission(ctx, "expense.log")


def can_decide(ctx: OrgContext, claimant_id: uuid.UUID, manager_id: uuid.UUID | None) -> bool:
    """``expense.approve``, as the claimant's manager or with ``expense.read_all``, and not on
    one's own claim (unless the caller holds ``organization.update``)."""
    if not policy.has_permission(ctx, "expense.approve"):
        return False
    if claimant_id == ctx.user.id and not policy.has_permission(ctx, "organization.update"):
        return False
    return policy.has_permission(ctx, "expense.read_all") or (
        manager_id is not None and manager_id == ctx.user.id
    )


def claim_actions(
    ctx: OrgContext, claim: ExpenseClaim, manager_id: uuid.UUID | None
) -> ClaimActions:
    writable = can_write(ctx, claim.user_id)
    editable = writable and claim.status in EDITABLE_STATUSES
    decidable = claim.status == "submitted" and can_decide(ctx, claim.user_id, manager_id)
    return ClaimActions(
        can_edit=editable,
        can_delete=writable and claim.status == "draft",
        can_submit=editable,
        can_recall=writable and claim.status == "submitted",
        can_approve=decidable,
        can_return=decidable,
        can_correct=claim.status == "approved" and policy.has_permission(ctx, "expense.manage"),
    )


class ExpenseAccess:
    """The rules that need the people the caller manages."""

    def __init__(self, session: AsyncSession) -> None:
        self.refs = TimeReferenceRepository(session)

    async def managed_ids(self, ctx: OrgContext) -> list[uuid.UUID]:
        return sorted(await self.refs.managed_user_ids(ctx.organization_id, ctx.user.id))

    async def visible_user_ids(self, ctx: OrgContext) -> list[uuid.UUID] | None:
        """Whose claims the caller may see (``None``: everyone's). 403 without any expense
        permission."""
        ensure_module_access(ctx)
        if sees_all(ctx):
            return None
        visible = [ctx.user.id]
        if policy.has_permission(ctx, "expense.approve"):
            visible.extend(await self.managed_ids(ctx))
        return visible

    async def approval_scope(self, ctx: OrgContext) -> list[uuid.UUID] | None:
        """Whose claims the caller decides on (``None``: everyone's). 403 without
        ``expense.approve``."""
        policy.ensure(ctx, "expense.approve")
        if policy.has_permission(ctx, "expense.read_all"):
            return None
        return await self.managed_ids(ctx)

    async def manager_of(self, ctx: OrgContext, user_id: uuid.UUID) -> uuid.UUID | None:
        member = await self.refs.get_member(ctx.organization_id, user_id)
        return member.manager_user_id if member else None

    async def ensure_can_view(self, ctx: OrgContext, claimant_id: uuid.UUID) -> None:
        ensure_module_access(ctx)
        if claimant_id == ctx.user.id or sees_all(ctx):
            return
        if (
            policy.has_permission(ctx, "expense.approve")
            and await self.manager_of(ctx, claimant_id) == ctx.user.id
        ):
            return
        raise AuthorizationError()

    @staticmethod
    def ensure_can_write(ctx: OrgContext, claimant_id: uuid.UUID) -> None:
        if not can_write(ctx, claimant_id):
            raise AuthorizationError()

    async def ensure_can_decide(self, ctx: OrgContext, claim: ExpenseClaim) -> None:
        """403 unless the caller may approve or return this claim."""
        policy.ensure(ctx, "expense.approve")
        if claim.user_id == ctx.user.id and not policy.has_permission(ctx, "organization.update"):
            raise SelfApprovalError()
        if policy.has_permission(ctx, "expense.read_all"):
            return
        if await self.manager_of(ctx, claim.user_id) != ctx.user.id:
            raise AuthorizationError()
