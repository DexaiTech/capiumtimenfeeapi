"""Central users: what the central user service tells the practice, applied to its staff.

One handler serves both ways in: ``POST /integrations/central-user/events`` (the service pushes
an event) and ``process-changes`` (the practice pulls the change feed). A person is found by
their email address among the members of the practice; handlers run as the system (the audit
entry has no actor; the person who triggered the call, the event id and the source are in its
metadata).

* ``renamed``: the person's name changes (``users.full_name``, the display data of every list).
* ``deactivated``: an active member becomes ``inactive`` (their records stay, they can no longer
  open the practice) and, when they belong to no other practice, their sessions end. **Never the
  practice's last administrator**: the event is refused (and kept as a failed sync item with the
  reason) while nobody else who can sign in holds the permissions to manage staff and roles.
* ``reactivated``: an ``inactive`` member becomes ``active`` again (not one who ``left``, and not
  when another active member already uses their work email).
* An event about someone who is not a member of the practice is ``skipped``, never an error.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.db.models import OrganizationMember, User
from app.db.models.organization import GONE_STATUSES
from app.repositories.central_user_sync import CentralUserRepository
from app.repositories.members import MemberRepository
from app.repositories.organizations import OrganizationRepository
from app.repositories.sessions import SessionRepository
from app.repositories.staff import StaffRepository, normalised_status
from app.services import audit
from app.services.integration_events import Applied, Handled, Polled, handle_event_once, poll_feed
from app.services.integration_outbox import ItemFailure, ItemHandler, ItemOutcome
from app.services.integrations import (
    CENTRAL_USER,
    CentralUserEvent,
    CentralUserGateway,
)
from app.services.policy import OrgContext

MAX_NAME = 120


def _event_dict(event: CentralUserEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "type": event.type,
        "central_user_id": event.central_user_id,
        "email": event.email,
        "full_name": event.full_name,
        "occurred_at": event.occurred_at.isoformat(),
    }


def event_from_payload(raw: dict[str, Any]) -> CentralUserEvent:
    occurred = raw.get("occurred_at")
    return CentralUserEvent(
        event_id=str(raw["event_id"]),
        type=raw["type"],
        central_user_id=str(raw.get("central_user_id") or ""),
        email=str(raw["email"]),
        occurred_at=datetime.fromisoformat(occurred) if occurred else datetime.now(UTC),
        full_name=raw.get("full_name"),
    )


class CentralUserService:
    def __init__(self, session: AsyncSession, gateway: CentralUserGateway) -> None:
        self.session = session
        self.gateway = gateway
        self.users = CentralUserRepository(session)
        self.members = MemberRepository(session)

    async def handle(
        self, ctx: OrgContext, rctx: RequestContext, event: CentralUserEvent, *, source: str
    ) -> Handled:
        return await handle_event_once(
            self.session,
            ctx.organization_id,
            integration=CENTRAL_USER,
            event_id=event.event_id,
            event_type=event.type,
            external_id=event.central_user_id or event.email,
            source=source,
            apply=lambda: self.apply_event(ctx, rctx, event, source=source),
            park_payload=_event_dict(event),
            label=f"{event.type} {event.email}",
        )

    async def apply_event(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        event: CentralUserEvent,
        *,
        source: str = "retry",
    ) -> Applied:
        org_id = ctx.organization_id
        user = await self.users.user_by_email(event.email)
        member = await self.members.get(org_id, user.id) if user else None
        if user is None or member is None or member.status in GONE_STATUSES:
            return Applied("skipped", f"{event.email} is not a member of this practice.")
        meta = {
            "event_id": event.event_id,
            "source": source,
            "central_user_id": event.central_user_id,
            "requested_by": str(ctx.user.id),
        }
        match event.type:
            case "renamed":
                return await self._renamed(org_id, rctx, user, member, event, meta)
            case "deactivated":
                return await self._deactivated(org_id, rctx, user, member, meta)
            case "reactivated":
                return await self._reactivated(org_id, rctx, user, member, meta)

    # --- Handlers -------------------------------------------------------------------------

    async def _renamed(
        self,
        org_id: uuid.UUID,
        rctx: RequestContext,
        user: User,
        member: OrganizationMember,
        event: CentralUserEvent,
        meta: dict[str, Any],
    ) -> Applied:
        name = (event.full_name or "").strip()
        if not name or len(name) > MAX_NAME:
            return Applied("skipped", "The event carries no usable name.", "member", user.id)
        if user.full_name == name:
            return Applied("skipped", "Already up to date.", "member", user.id)
        old = user.full_name
        user.full_name = name
        await self.session.flush()
        await self._audit(
            org_id, rctx, "staff.central_renamed", user, member, meta, {"full_name": [old, name]}
        )
        return Applied(
            "applied",
            f"{old} is now {name}.",
            "member",
            user.id,
            {"changes": {"full_name": [old, name]}},
        )

    async def _deactivated(
        self,
        org_id: uuid.UUID,
        rctx: RequestContext,
        user: User,
        member: OrganizationMember,
        meta: dict[str, Any],
    ) -> Applied:
        # Serialise with every other change to the practice's staff and roles (the same lock the
        # staff and role services take), then read the member again: the check below is only
        # right with nobody else changing who the administrators are.
        await OrganizationRepository(self.session).lock(org_id)
        fresh = await self.members.get(org_id, user.id)
        if fresh is None or fresh.status in GONE_STATUSES:
            return Applied("skipped", f"{user.email} is not a member of this practice.")
        member = fresh
        if normalised_status(member.status) == "inactive":
            return Applied("skipped", "The person is already inactive.", "member", user.id)
        if (
            member.status == "active"
            and member.login_enabled
            and await self.users.is_administrator(member)
            and await self.users.other_administrators(org_id, user.id) == 0
        ):
            return Applied(
                "failed",
                f"{user.full_name} is the practice's last administrator: appoint another before "
                "they are deactivated.",
                "member",
                user.id,
                failure_code="LAST_ADMIN",
            )
        old_status = member.status
        member.status = "inactive"
        await self.session.flush()
        if member.login_enabled and not await self.members.other_organizations(org_id, user.id):
            await SessionRepository(self.session).revoke_all_for_user(
                user.id, "admin", datetime.now(UTC)
            )
        await self._audit(
            org_id,
            rctx,
            "staff.central_deactivated",
            user,
            member,
            meta,
            {"status": [old_status, "inactive"]},
        )
        return Applied(
            "applied",
            f"{user.full_name} was deactivated.",
            "member",
            user.id,
            {"changes": {"status": [old_status, "inactive"]}},
        )

    async def _reactivated(
        self,
        org_id: uuid.UUID,
        rctx: RequestContext,
        user: User,
        member: OrganizationMember,
        meta: dict[str, Any],
    ) -> Applied:
        if normalised_status(member.status) != "inactive":
            return Applied("skipped", "The person is not inactive.", "member", user.id)
        if member.work_email and await StaffRepository(self.session).email_taken(
            org_id, member.work_email, exclude_user_id=user.id
        ):
            return Applied(
                "failed",
                "Another active staff member already uses this email address.",
                "member",
                user.id,
                failure_code="EMAIL_TAKEN",
            )
        old_status = member.status
        member.status = "active"
        await self.session.flush()
        await self._audit(
            org_id,
            rctx,
            "staff.central_reactivated",
            user,
            member,
            meta,
            {"status": [old_status, "active"]},
        )
        return Applied(
            "applied",
            f"{user.full_name} was reactivated.",
            "member",
            user.id,
            {"changes": {"status": [old_status, "active"]}},
        )

    async def _audit(
        self,
        org_id: uuid.UUID,
        rctx: RequestContext,
        action: str,
        user: User,
        member: OrganizationMember,
        meta: dict[str, Any],
        changes: dict[str, list[Any]],
    ) -> None:
        await audit.record(
            self.session,
            action,
            ctx=rctx,
            actor_user_id=None,
            organization_id=org_id,
            target_type="member",
            target_id=user.id,
            meta={
                "staff_name": user.full_name,
                "staff_code": member.staff_code,
                "changes": changes,
                **meta,
            },
        )

    # --- The change feed ------------------------------------------------------------------

    async def process_changes(self, ctx: OrgContext, rctx: RequestContext) -> Polled:
        return await poll_feed(
            self.session,
            ctx.organization_id,
            integration=CENTRAL_USER,
            fetch=self.gateway.changes_since,
            handle=lambda event: self.handle(ctx, rctx, event, source="feed"),
            service_name="central user service",
        )


def apply_event_handler(
    ctx: OrgContext, rctx: RequestContext, gateway: CentralUserGateway
) -> ItemHandler:
    """The handler that retries a refused central user event kept as a failed item."""

    async def handler(session: AsyncSession, item: Any) -> ItemOutcome:
        raw = item.payload.get("event")
        if not isinstance(raw, dict):
            raise ItemFailure("The item carries no event.", "INVALID")
        applied = await CentralUserService(session, gateway).apply_event(
            ctx, rctx, event_from_payload(raw)
        )
        if applied.outcome == "failed":
            raise ItemFailure(applied.message, applied.failure_code or "REFUSED")
        return ItemOutcome(
            "ok" if applied.outcome == "applied" else "skipped",
            message=None if applied.outcome == "applied" else applied.message,
        )

    return handler
