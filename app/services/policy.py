"""Authorization decisions. The only place that answers "may this caller do that?"."""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from app.core.exceptions import AuthorizationError
from app.core.permissions import OWNER_KEY
from app.db.models import Organization, OrganizationMember, Role, User


@dataclass(frozen=True, slots=True)
class OrgContext:
    """Who is calling, in which practice, with which permissions."""

    user: User
    session_id: uuid.UUID
    organization: Organization
    member: OrganizationMember
    role: Role
    permissions: frozenset[str]

    @property
    def organization_id(self) -> uuid.UUID:
        return self.organization.id


def has_permission(ctx: OrgContext, code: str) -> bool:
    return code in ctx.permissions


def ensure(ctx: OrgContext, *codes: str) -> None:
    """Raise ``AuthorizationError`` unless the caller holds every permission."""
    missing = [code for code in codes if code not in ctx.permissions]
    if missing:
        raise AuthorizationError()


def ensure_any(ctx: OrgContext, *codes: str) -> None:
    """Raise ``AuthorizationError`` unless the caller holds at least one of the permissions."""
    if not any(code in ctx.permissions for code in codes):
        raise AuthorizationError()


def is_owner(ctx: OrgContext) -> bool:
    return ctx.role.system_key == OWNER_KEY


def ensure_can_grant(ctx: OrgContext, codes: Iterable[str]) -> None:
    """Nobody hands out permissions they do not hold themselves."""
    if not set(codes) <= ctx.permissions:
        raise AuthorizationError("You cannot grant permissions you do not have.")


def ensure_can_assign_role(ctx: OrgContext, system_key: str | None, codes: Iterable[str]) -> None:
    """Only an Owner assigns the Owner role; otherwise the grant rule applies."""
    if system_key == OWNER_KEY and not is_owner(ctx):
        raise AuthorizationError("Only an owner can assign the Owner role.")
    ensure_can_grant(ctx, codes)
