"""Small helpers the practice-level services share: duplicate errors, applying a patch while
collecting what changed, and the audit row of an action taken inside a practice."""

import uuid
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import ConflictError
from app.services import audit
from app.services.policy import OrgContext


def duplicate_error(field: str, message: str, code: str) -> ConflictError:
    """409 for a value that must be unique, naming the field."""
    return ConflictError(
        message, code=code, details=[{"field": field, "code": "duplicate", "message": message}]
    )


def plain(value: Any) -> Any:
    """``value`` as JSON can hold it (audit rows and history keep these)."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, date | datetime):
        return value.isoformat()
    return value


AUDIT_TEXT_LENGTH = 500


def audit_excerpt(value: str | None) -> str | None:
    """A long text is cut in the audit log: the row says that it changed, not the whole essay."""
    if value is not None and len(value) > AUDIT_TEXT_LENGTH:
        return value[:AUDIT_TEXT_LENGTH] + "…"
    return value


def apply_changes(obj: object, values: dict[str, Any]) -> dict[str, list[Any]]:
    """Set ``values`` on ``obj``; return ``{field: [old, new]}`` for what actually changed."""
    changed: dict[str, list[Any]] = {}
    for field, value in values.items():
        old = getattr(obj, field)
        if old != value:
            changed[field] = [plain(old), plain(value)]
            setattr(obj, field, value)
    return changed


async def record_audit(
    session: AsyncSession,
    ctx: OrgContext,
    rctx: RequestContext,
    action: str,
    target_type: str,
    target_id: uuid.UUID | None,
    meta: dict[str, Any],
) -> None:
    await audit.record(
        session,
        action,
        ctx=rctx,
        actor_user_id=ctx.user.id,
        organization_id=ctx.organization_id,
        target_type=target_type,
        target_id=target_id,
        meta=meta,
    )


async def record_audit_many(
    session: AsyncSession, ctx: OrgContext, rctx: RequestContext, rows: Sequence[audit.AuditRow]
) -> None:
    """The audit rows of one request that touched several records: one flush for all."""
    await audit.record_many(
        session, rows, ctx=rctx, actor_user_id=ctx.user.id, organization_id=ctx.organization_id
    )
