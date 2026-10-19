"""Central events: what the central client service tells the practice, applied to its clients.

One handler serves both ways in: ``POST /central-clients/events`` (the central service pushes an
event) and ``process-changes`` (the practice pulls the change feed). Handlers run as the system:
the audit entries have no actor (the IP address and the person who triggered the call are kept
when there is one).

* ``renamed``: the linked client takes the new name and address; the cache follows.
* ``archived`` / ``restored``: the linked client becomes ``archived`` / ``active`` again.
* ``merged`` (``central_id`` into ``into_central_id``): both enabled here -> the clients are
  merged (``client_merge``); only the merged-away one -> it is re-linked to the surviving central
  client; otherwise nothing to do.
* An event for a central client that is not enabled here is ``skipped``, never an error, and an
  event id is processed once: a repeat is logged ``skipped`` and changes nothing.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.db.models import CentralClientCache, Client
from app.db.session import unit_of_work
from app.repositories.central_clients import CentralClientRepository
from app.repositories.clients import ClientRepository
from app.repositories.integration_logs import CursorRepository, SyncLogRepository
from app.schemas.central_clients import CentralEventResult, ProcessedChanges
from app.schemas.clients import CENTRALLY_MANAGED_FIELDS
from app.services import audit
from app.services.central_clients import (
    CentralClientEvent,
    CentralClientGateway,
    CentralUnavailableError,
)
from app.services.client_merge import merge_clients
from app.services.integration_logs import CENTRAL_CLIENT, record_failure, record_sync
from app.services.members import integrity_constraint
from app.services.policy import OrgContext

CHANGE_BATCH = 100
EVENT_INDEX = "uq_integration_sync_logs_event"
MAX_CHAIN = 10
Source = Literal["webhook", "feed"]
# Writes the audit entry of one handler: the action, the client and the extra metadata.
type AuditClient = Callable[[str, Client, dict[str, Any]], Awaitable[None]]


def now_utc() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class Applied:
    outcome: Literal["applied", "skipped"]
    message: str
    client_id: uuid.UUID | None = None
    details: dict[str, Any] = field(default_factory=dict)


def _skipped(message: str, client_id: uuid.UUID | None = None) -> Applied:
    return Applied("skipped", message, client_id)


def _event_values(event: CentralClientEvent) -> dict[str, str | None]:
    """The name and address fields an event carries: ``""`` clears (``None``), a missing field
    is left out. The name and the country cannot be cleared."""
    values: dict[str, str | None] = {}
    for name in CENTRALLY_MANAGED_FIELDS:
        value: str | None = getattr(event, name)
        if value is None:
            continue
        if name in ("name", "country") and not value:
            continue
        values[name] = value or None
    return values


def _cache_values(row: CentralClientCache) -> dict[str, str | None]:
    """The name and address of a cached central client."""
    return {name: getattr(row, name) for name in CENTRALLY_MANAGED_FIELDS}


class CentralEventService:
    def __init__(self, session: AsyncSession, gateway: CentralClientGateway) -> None:
        self.session = session
        self.gateway = gateway
        self.central = CentralClientRepository(session)
        self.clients = ClientRepository(session)
        self.logs = SyncLogRepository(session)
        self.cursors = CursorRepository(session)

    # --- One event ------------------------------------------------------------------------

    async def handle(
        self,
        organization_id: uuid.UUID,
        event: CentralClientEvent,
        *,
        source: Source,
        rctx: RequestContext | None,
        requested_by: uuid.UUID | None,
    ) -> CentralEventResult:
        """Apply one event once. Raises when applying it failed (the failure is logged)."""
        if await self.logs.event_processed(organization_id, CENTRAL_CLIENT, event.event_id):
            return await self._duplicate(organization_id, event, source)
        applied: Applied | None = None
        locked = [event.central_id]
        if event.into_central_id:
            locked.append(event.into_central_id)
        try:
            async with unit_of_work(self.session):
                # Deliveries of one central client's events queue up; the second one of the
                # same event then finds it processed.
                await self.central.lock_central_ids(organization_id, locked)
                if not await self.logs.event_processed(
                    organization_id, CENTRAL_CLIENT, event.event_id
                ):
                    applied = await self._apply(organization_id, event, source, rctx, requested_by)
                    await record_sync(
                        self.session,
                        organization_id,
                        integration=CENTRAL_CLIENT,
                        operation=event.type,
                        status="ok" if applied.outcome == "applied" else "skipped",
                        message=applied.message,
                        entity_type="client" if applied.client_id else None,
                        entity_id=applied.client_id,
                        external_id=event.central_id,
                        external_event_id=event.event_id,
                        details={"event_id": event.event_id, "source": source, **applied.details},
                    )
        except IntegrityError as exc:
            if integrity_constraint(exc) == EVENT_INDEX:
                return await self._duplicate(organization_id, event, source)
            await self._failed(organization_id, event, source, exc)
            raise
        except Exception as exc:
            await self._failed(organization_id, event, source, exc)
            raise
        if applied is None:
            return await self._duplicate(organization_id, event, source)
        return CentralEventResult(
            event_id=event.event_id,
            type=event.type,
            central_id=event.central_id,
            outcome=applied.outcome,
            duplicate=False,
            message=applied.message,
            client_id=applied.client_id,
        )

    async def _duplicate(
        self, organization_id: uuid.UUID, event: CentralClientEvent, source: Source
    ) -> CentralEventResult:
        message = f"Event {event.event_id} was already processed: nothing changed."
        async with unit_of_work(self.session):
            await record_sync(
                self.session,
                organization_id,
                integration=CENTRAL_CLIENT,
                operation=event.type,
                status="skipped",
                message=message,
                external_id=event.central_id,
                details={"event_id": event.event_id, "source": source, "duplicate": True},
            )
        return CentralEventResult(
            event_id=event.event_id,
            type=event.type,
            central_id=event.central_id,
            outcome="skipped",
            duplicate=True,
            message=message,
            client_id=None,
        )

    @staticmethod
    async def _failed(
        organization_id: uuid.UUID, event: CentralClientEvent, source: Source, exc: Exception
    ) -> None:
        await record_failure(
            organization_id,
            integration=CENTRAL_CLIENT,
            operation=event.type,
            entity_type=None,
            external_id=event.central_id,
            message=f"Event {event.event_id} failed: {type(exc).__name__}.",
            details={"event_id": event.event_id, "source": source},
        )

    async def _apply(
        self,
        organization_id: uuid.UUID,
        event: CentralClientEvent,
        source: Source,
        rctx: RequestContext | None,
        requested_by: uuid.UUID | None,
    ) -> Applied:
        async def audit_client(action: str, client: Client, meta: dict[str, Any]) -> None:
            await audit.record(
                self.session,
                action,
                ctx=rctx,
                actor_user_id=None,
                organization_id=organization_id,
                target_type="client",
                target_id=client.id,
                meta={
                    "code": client.code,
                    "name": client.name,
                    "central_id": event.central_id,
                    "event_id": event.event_id,
                    "source": source,
                    "requested_by": str(requested_by) if requested_by else None,
                    **meta,
                },
            )

        match event.type:
            case "renamed":
                return await self._renamed(organization_id, event, audit_client)
            case "archived":
                return await self._archived(organization_id, event, audit_client)
            case "restored":
                return await self._restored(organization_id, event, audit_client)
            case "merged":
                return await self._merged(organization_id, event, audit_client)

    # --- The cache ------------------------------------------------------------------------

    async def _cache_event(
        self, organization_id: uuid.UUID, event: CentralClientEvent, *, status: str | None = None
    ) -> None:
        """Bring the cached copy of the event's central client up to date: the status it now
        has, and the name and address a rename carries."""
        row = await self.central.get_cache(organization_id, event.central_id)
        if row is None:
            return
        if event.type == "renamed":
            for name, value in _event_values(event).items():
                setattr(row, name, value)
        if status is not None:
            row.status = status
        row.source_updated_at = event.occurred_at
        row.fetched_at = now_utc()
        await self.session.flush()

    # --- Handlers -------------------------------------------------------------------------

    async def _renamed(
        self, organization_id: uuid.UUID, event: CentralClientEvent, audit_client: AuditClient
    ) -> Applied:
        await self._cache_event(organization_id, event)
        client = await self.central.client_by_central_id(
            organization_id, event.central_id, lock=True
        )
        if client is None:
            return _skipped("This central client is not enabled in this practice.")
        if client.merged_into_client_id is not None:
            return _skipped("This client was merged into another client.", client.id)
        changes: dict[str, list[str | None]] = {}
        for name, value in _event_values(event).items():
            old: str | None = getattr(client, name)
            if old != value:
                changes[name] = [old, value]
                setattr(client, name, value)
        if not changes and client.central_sync_state == "synced":
            return _skipped("Already up to date.", client.id)
        client.central_sync_state = "synced"
        client.central_synced_at = now_utc()
        await self.clients.save(client)
        await audit_client("client.central_renamed", client, {"changes": changes})
        message = "Name and address updated." if changes else "Confirmed by the central service."
        return Applied("applied", message, client.id, {"changes": changes})

    async def _archived(
        self, organization_id: uuid.UUID, event: CentralClientEvent, audit_client: AuditClient
    ) -> Applied:
        await self._cache_event(organization_id, event, status="archived")
        client = await self.central.client_by_central_id(
            organization_id, event.central_id, lock=True
        )
        if client is None:
            return _skipped("This central client is not enabled in this practice.")
        if client.merged_into_client_id is not None:
            return _skipped("This client was merged into another client.", client.id)
        if client.status == "archived":
            return _skipped("The client is already archived.", client.id)
        previous = client.status
        client.status = "archived"
        await self.clients.save(client)
        await audit_client("client.central_archived", client, {"previous_status": previous})
        return Applied("applied", "Client archived.", client.id, {"previous_status": previous})

    async def _restored(
        self, organization_id: uuid.UUID, event: CentralClientEvent, audit_client: AuditClient
    ) -> Applied:
        await self._cache_event(organization_id, event, status="active")
        client = await self.central.client_by_central_id(
            organization_id, event.central_id, lock=True
        )
        if client is None:
            return _skipped("This central client is not enabled in this practice.")
        if client.merged_into_client_id is not None:
            return _skipped("This client was merged into another client.", client.id)
        if client.status != "archived":
            return _skipped("The client is not archived.", client.id)
        client.status = "active"
        await self.clients.save(client)
        await audit_client("client.central_restored", client, {"previous_status": "archived"})
        return Applied("applied", "Client restored.", client.id, {"previous_status": "archived"})

    async def _survivor(self, organization_id: uuid.UUID, client: Client) -> Client | None:
        """Follow ``merged_into_client_id`` to the client that holds the work now."""
        for _ in range(MAX_CHAIN):
            if client.merged_into_client_id is None:
                return client
            found = await self.clients.get(
                organization_id, client.merged_into_client_id, lock="update"
            )
            if found is None:
                return None
            client = found
        return None

    async def _merged(
        self, organization_id: uuid.UUID, event: CentralClientEvent, audit_client: AuditClient
    ) -> Applied:
        assert event.into_central_id is not None
        await self._cache_event(organization_id, event, status="archived")
        source = await self.central.client_by_central_id(
            organization_id, event.central_id, lock=True
        )
        target = await self.central.client_by_central_id(
            organization_id, event.into_central_id, lock=True
        )
        if source is None:
            if target is None:
                return _skipped("Neither central client is enabled in this practice.")
            return _skipped("The merged central client is not enabled in this practice.", target.id)
        if source.merged_into_client_id is not None:
            return _skipped("This client was already merged into another client.", source.id)
        if target is None:
            return await self._relink(organization_id, event, source, audit_client)
        survivor = await self._survivor(organization_id, target)
        if survivor is None or survivor.id == source.id:
            return _skipped("The clients cannot be merged: the survivor is missing.", source.id)
        counts = await merge_clients(self.session, organization_id, source, survivor)
        details: dict[str, Any] = {
            "mode": "merged",
            "merged_client": {
                "id": str(source.id),
                "code": source.code,
                "name": source.name,
                "central_id": source.central_client_id,
            },
            "survivor": {
                "id": str(survivor.id),
                "code": survivor.code,
                "name": survivor.name,
                "central_id": survivor.central_client_id,
            },
            "counts": counts.moved,
            "left_behind": counts.left_behind,
            "contacts_demoted": counts.contacts_demoted,
            "merged_clients_repointed": counts.merged_clients_repointed,
            "rate_history_rows": counts.rate_history_rows,
        }
        # The audit entry belongs to the surviving client and names the one that went.
        await audit_client(
            "client.central_merged", survivor, {**details, "client_id": str(survivor.id)}
        )
        rows = sum(counts.moved.values())
        message = f"Merged {source.code} into {survivor.code}: {rows} rows moved."
        return Applied("applied", message, survivor.id, details)

    async def _relink(
        self,
        organization_id: uuid.UUID,
        event: CentralClientEvent,
        source: Client,
        audit_client: AuditClient,
    ) -> Applied:
        """Only the merged-away client is enabled here: it becomes the survivor's client."""
        assert event.into_central_id is not None
        previous_central_id = source.central_client_id
        values = _event_values(event)
        cached = await self.central.get_cache(organization_id, event.into_central_id)
        state = "synced"
        if not values:
            state = "stale"
            if cached is not None:
                values = _cache_values(cached)
        changes: dict[str, list[str | None]] = {}
        for name, value in values.items():
            old: str | None = getattr(source, name)
            if old != value:
                changes[name] = [old, value]
                setattr(source, name, value)
        source.central_client_id = event.into_central_id
        source.central_sync_state = state
        if state == "synced":
            source.central_synced_at = now_utc()
        await self.clients.save(source)
        details: dict[str, Any] = {
            "mode": "relinked",
            "from_central_id": previous_central_id,
            "to_central_id": event.into_central_id,
            "changes": changes,
        }
        await audit_client(
            "client.central_merged", source, {**details, "client_id": str(source.id)}
        )
        message = f"{source.code} now follows the surviving central client."
        return Applied("applied", message, source.id, details)

    # --- The change feed ------------------------------------------------------------------

    async def process_changes(self, ctx: OrgContext, rctx: RequestContext) -> ProcessedChanges:
        org_id = ctx.organization_id
        cursor = await self.cursors.get(org_id, CENTRAL_CLIENT)
        try:
            batch = await self.gateway.changes_since(cursor, CHANGE_BATCH)
        except CentralUnavailableError:
            async with unit_of_work(self.session):
                await record_sync(
                    self.session,
                    org_id,
                    integration=CENTRAL_CLIENT,
                    operation="poll",
                    status="failed",
                    message="The central client service is unavailable.",
                    details={"cursor": cursor},
                )
            return ProcessedChanges(
                available=False,
                fetched=0,
                applied=0,
                skipped=0,
                failed=0,
                has_more=False,
                cursor=cursor,
                events=[],
            )
        results: list[CentralEventResult] = []
        failed = False
        for event in batch.events:
            try:
                results.append(
                    await self.handle(
                        org_id, event, source="feed", rctx=rctx, requested_by=ctx.user.id
                    )
                )
            except Exception:
                # Logged by ``handle``. The cursor stays: the batch is read again, and the
                # events that went through are skipped as repeats.
                failed = True
                break
        new_cursor = cursor if failed or batch.cursor is None else batch.cursor
        applied = sum(1 for r in results if r.outcome == "applied")
        skipped = len(results) - applied
        async with unit_of_work(self.session):
            if new_cursor is not None and new_cursor != cursor:
                await self.cursors.save(org_id, CENTRAL_CLIENT, new_cursor)
            await record_sync(
                self.session,
                org_id,
                integration=CENTRAL_CLIENT,
                operation="poll",
                status="failed" if failed else "ok",
                message=(
                    f"Read {len(batch.events)} event(s): {applied} applied, {skipped} skipped"
                    + (", then one failed." if failed else ".")
                ),
                details={"fetched": len(batch.events), "applied": applied, "skipped": skipped},
            )
        return ProcessedChanges(
            available=True,
            fetched=len(batch.events),
            applied=applied,
            skipped=skipped,
            failed=1 if failed else 0,
            has_more=batch.has_more or failed,
            cursor=new_cursor,
            events=results,
        )
