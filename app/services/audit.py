"""Audit trail: security-relevant actions, written in the caller's transaction."""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.db.models import AuditLog
from app.repositories.audit import AuditRepository

# One entry of ``record_many``: the action, the target type and id, and the meta.
type AuditRow = tuple[str, str | None, uuid.UUID | None, dict[str, Any]]


async def record(
    session: AsyncSession,
    action: str,
    *,
    ctx: RequestContext | None = None,
    actor_user_id: uuid.UUID | None = None,
    organization_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: uuid.UUID | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    await AuditRepository(session).add(
        AuditLog(
            action=action,
            actor_user_id=actor_user_id,
            organization_id=organization_id,
            target_type=target_type,
            target_id=target_id,
            ip_address=ctx.ip_address if ctx else None,
            user_agent=ctx.user_agent if ctx else None,
            request_id=ctx.request_id if ctx else None,
            meta=meta or {},
        )
    )


async def record_many(
    session: AsyncSession,
    rows: Sequence[AuditRow],
    *,
    ctx: RequestContext | None = None,
    actor_user_id: uuid.UUID | None = None,
    organization_id: uuid.UUID | None = None,
) -> None:
    """Several entries by one caller in one request, written with a single flush."""
    if not rows:
        return
    await AuditRepository(session).add_many(
        [
            AuditLog(
                action=action,
                actor_user_id=actor_user_id,
                organization_id=organization_id,
                target_type=target_type,
                target_id=target_id,
                ip_address=ctx.ip_address if ctx else None,
                user_agent=ctx.user_agent if ctx else None,
                request_id=ctx.request_id if ctx else None,
                meta=meta,
            )
            for action, target_type, target_id, meta in rows
        ]
    )
