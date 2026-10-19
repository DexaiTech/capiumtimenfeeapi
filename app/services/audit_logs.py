"""Reading the audit trail of an organization: the list, the history of one record and the export.
Writing is ``app.services.audit.record``.

Who sees what:

* the list and the export need ``audit_log.read`` (the export also ``audit_log.export``);
* the history of a record needs ``audit_log.read``, or the read permission of the record's own
  type (``RECORD_READ_PERMISSIONS``: ``client.read`` for a client, ``invoice.read`` for an
  invoice ...). Without ``audit_log.read`` the addresses are left out, and so is every entry
  about a record type the caller may not read;
* cost figures are ``null`` for everybody without ``rate.read``.
"""

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import AuthorizationError
from app.db.models import AuditLog, User
from app.db.session import unit_of_work
from app.repositories.audit import AuditRepository
from app.schemas.audit import (
    AuditActor,
    AuditChange,
    AuditFilters,
    AuditHistoryActor,
    AuditHistoryEntryRead,
    AuditLogEntryRead,
)
from app.services import audit, audit_export
from app.services.audit_catalogue import (
    COST_PERMISSION,
    READ_PERMISSION,
    RELATED_KEYS,
    label_of,
    readable_types,
    redact_costs,
)
from app.services.audit_view import extract_changes, summarise
from app.services.policy import OrgContext, has_permission
from app.utils.pagination import Page, PageParams
from app.utils.practice_dates import today_in


def _as_utc(value: datetime | None) -> datetime | None:
    """A timestamp without an offset is read as UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _utc(filters: AuditFilters) -> AuditFilters:
    return replace(
        filters,
        created_from=_as_utc(filters.created_from),
        created_to=_as_utc(filters.created_to),
    )


def _filters_meta(filters: AuditFilters) -> dict[str, str]:
    """The filters of an export as the audit entry of the export records them."""
    found = {
        "action": filters.action,
        "actor_user_id": str(filters.actor_user_id) if filters.actor_user_id else None,
        "from": filters.created_from.isoformat() if filters.created_from else None,
        "to": filters.created_to.isoformat() if filters.created_to else None,
        "target_type": filters.target_type,
        "target_id": str(filters.target_id) if filters.target_id else None,
        "q": filters.q,
    }
    return {key: value for key, value in found.items() if value}


class _Presented:
    """What a person reads about one stored entry: details, changes, label and summary."""

    __slots__ = ("changes", "label", "meta", "summary")

    def __init__(self, entry: AuditLog, show_cost: bool) -> None:
        self.meta: dict[str, Any] = entry.meta if show_cost else redact_costs(entry.meta)
        self.changes: list[AuditChange] = extract_changes(entry.action, self.meta)
        self.label = label_of(entry.action)
        self.summary = summarise(self.label, self.meta)


class AuditLogService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit = AuditRepository(session)

    async def _actors(self, entries: list[AuditLog]) -> dict[uuid.UUID, User]:
        return await self.audit.get_actors({e.actor_user_id for e in entries if e.actor_user_id})

    async def list(
        self, ctx: OrgContext, filters: AuditFilters, params: PageParams
    ) -> Page[AuditLogEntryRead]:
        """One page of the caller's organization, each entry with its actor and its changes."""
        page = await self.audit.list_page(ctx.organization_id, _utc(filters), params)
        actors = await self._actors(page.items)
        show_cost = has_permission(ctx, COST_PERMISSION)
        items = []
        for entry in page.items:
            seen = _Presented(entry, show_cost)
            actor = actors.get(entry.actor_user_id) if entry.actor_user_id else None
            items.append(
                AuditLogEntryRead(
                    id=entry.id,
                    created_at=entry.created_at,
                    action=entry.action,
                    label=seen.label,
                    summary=seen.summary,
                    actor=AuditActor(id=actor.id, full_name=actor.full_name, email=actor.email)
                    if actor
                    else None,
                    target_type=entry.target_type,
                    target_id=entry.target_id,
                    ip_address=str(entry.ip_address) if entry.ip_address else None,
                    user_agent=entry.user_agent,
                    changes=seen.changes,
                    metadata=seen.meta,
                )
            )
        return Page(items=items, next_cursor=page.next_cursor)

    def _record_types(self, ctx: OrgContext, target_type: str) -> frozenset[str] | None:
        """The record types the caller's history may show; ``None`` for everything.

        403 when the caller holds neither ``audit_log.read`` nor the read permission of the type.
        """
        if has_permission(ctx, READ_PERMISSION):
            return None
        readable = readable_types(ctx.permissions)
        if target_type not in readable:
            raise AuthorizationError()
        return readable

    async def history(
        self,
        ctx: OrgContext,
        target_type: str,
        target_id: uuid.UUID,
        filters: AuditFilters,
        params: PageParams,
        *,
        include_related: bool = False,
    ) -> Page[AuditHistoryEntryRead]:
        """The entries about one record, newest first (another practice's record has none)."""
        record_types = self._record_types(ctx, target_type)
        page = await self.audit.history_page(
            ctx.organization_id,
            target_type,
            target_id,
            _utc(filters),
            params,
            related_key=RELATED_KEYS.get(target_type) if include_related else None,
            record_types=record_types,
        )
        actors = await self._actors(page.items)
        show_cost = has_permission(ctx, COST_PERMISSION)
        show_ip = has_permission(ctx, READ_PERMISSION)
        items = []
        for entry in page.items:
            seen = _Presented(entry, show_cost)
            actor = actors.get(entry.actor_user_id) if entry.actor_user_id else None
            items.append(
                AuditHistoryEntryRead(
                    id=entry.id,
                    action=entry.action,
                    label=seen.label,
                    actor=AuditHistoryActor(id=actor.id, name=actor.full_name) if actor else None,
                    at=entry.created_at,
                    ip_address=str(entry.ip_address) if show_ip and entry.ip_address else None,
                    target_type=entry.target_type,
                    target_id=entry.target_id,
                    changes=seen.changes,
                    summary=seen.summary,
                )
            )
        return Page(items=items, next_cursor=page.next_cursor)

    async def export(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        filters: AuditFilters,
        fmt: audit_export.ExportFormat,
    ) -> audit_export.AuditExport:
        """The newest ``MAX_EXPORT_ROWS`` matching entries as a file; the export is audited."""
        filters = _utc(filters)
        limit = audit_export.MAX_EXPORT_ROWS
        found = await self.audit.export_rows(ctx.organization_id, filters, limit + 1)
        truncated = len(found) > limit
        entries = found[:limit]
        actors = await self._actors(entries)
        show_cost = has_permission(ctx, COST_PERMISSION)
        rows = []
        for entry in entries:
            seen = _Presented(entry, show_cost)
            actor = actors.get(entry.actor_user_id) if entry.actor_user_id else None
            rows.append(
                audit_export.build_row(
                    at=entry.created_at,
                    user=actor.full_name if actor else None,
                    email=actor.email if actor else None,
                    action=entry.action,
                    event=seen.label,
                    summary=seen.summary,
                    record_type=entry.target_type,
                    record_id=str(entry.target_id) if entry.target_id else None,
                    changes=seen.changes,
                    ip_address=str(entry.ip_address) if entry.ip_address else None,
                    device=entry.user_agent,
                    request_id=entry.request_id,
                    details=seen.meta,
                )
            )

        filtered_actor = (
            (await self.audit.get_actors({filters.actor_user_id})).get(filters.actor_user_id)
            if filters.actor_user_id
            else None
        )
        described = audit_export.describe_filters(
            filters, filtered_actor.full_name if filtered_actor else None
        )
        notes = [
            f"Practice: {ctx.organization.name}",
            f"Exported {datetime.now(UTC):%d/%m/%Y %H:%M} UTC by {ctx.user.full_name}",
            f"Filters: {described}",
            f"Entries: {len(rows)}"
            + (
                f" (the newest {limit:,} of more: narrow the filters to export the rest)"
                if truncated
                else ""
            ),
        ]
        content, media_type = await asyncio.to_thread(audit_export.render, fmt, rows, notes=notes)

        async with unit_of_work(self.session):
            await audit.record(
                self.session,
                "audit_log.exported",
                ctx=rctx,
                actor_user_id=ctx.user.id,
                organization_id=ctx.organization_id,
                target_type="audit_log",
                meta={
                    "format": fmt,
                    "row_count": len(rows),
                    "truncated": truncated,
                    "filters": _filters_meta(filters),
                },
            )
        return audit_export.AuditExport(
            content=content,
            media_type=media_type,
            filename=audit_export.filename(fmt, today_in(ctx.organization.timezone).isoformat()),
            row_count=len(rows),
            truncated=truncated,
        )
