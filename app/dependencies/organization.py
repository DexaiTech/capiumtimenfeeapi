"""Practice (organization) scope and permission checks.

The organization always comes from the URL path and is verified against an active
membership on every request; it is never read from the token or the body.
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

import structlog
from fastapi import Depends, Query

from app.core.exceptions import AuthorizationError, NotFoundError
from app.core.permissions import effective_permissions
from app.dependencies.auth import VerifiedPrincipalDep
from app.dependencies.common import SessionDep
from app.repositories.memberships import MembershipRepository
from app.services.policy import OrgContext
from app.services.subscription import ensure_subscription_active
from app.utils.pagination import DEFAULT_LIMIT, MAX_LIMIT, PageParams


async def get_member_context(
    organization_id: uuid.UUID, principal: VerifiedPrincipalDep, session: SessionDep
) -> OrgContext:
    """The caller's active membership of the practice, whatever its subscription says. Only the
    routes that must answer for a practice that cannot be used (its subscription status, the
    development control of the mock) depend on this; every other route uses ``get_org_context``."""
    found = await MembershipRepository(session).get_active(organization_id, principal.user.id)
    if found is None:
        # Unknown organization and "not a member" are indistinguishable to the caller.
        raise NotFoundError("Organization not found.")
    organization, member = found
    structlog.contextvars.bind_contextvars(organization_id=str(organization.id))
    return OrgContext(
        user=principal.user,
        session_id=principal.session_id,
        organization=organization,
        member=member,
        role=member.role,
        permissions=effective_permissions(
            (p.code for p in member.role.permissions),
            ((o.permission_code, o.effect) for o in member.permission_overrides),
        ),
    )


MemberContextDep = Annotated[OrgContext, Depends(get_member_context)]


async def get_org_context(ctx: MemberContextDep) -> OrgContext:
    """The practice context of every practice route: the membership (404 for anyone else), then
    the practice's subscription (403 ``SUBSCRIPTION_UNAVAILABLE``; cached, no query)."""
    await ensure_subscription_active(ctx.organization)
    return ctx


OrgContextDep = Annotated[OrgContext, Depends(get_org_context)]


def require(*codes: str) -> Callable[[OrgContext], Awaitable[OrgContext]]:
    """Dependency factory: the caller must hold every listed permission.

    Usage: ``ctx: Annotated[OrgContext, Depends(require("client.update"))]``.
    """

    async def dependency(ctx: OrgContextDep) -> OrgContext:
        if any(code not in ctx.permissions for code in codes):
            raise AuthorizationError()
        return ctx

    return dependency


def get_page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT, description="Page size")] = DEFAULT_LIMIT,
    cursor: Annotated[
        str | None, Query(max_length=500, description="Cursor from meta.next_cursor")
    ] = None,
) -> PageParams:
    return PageParams(limit=limit, cursor=cursor)


PageParamsDep = Annotated[PageParams, Depends(get_page_params)]
