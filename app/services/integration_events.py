"""Events from another system, applied once: what the central user and practice management feeds
share (docs/modules/integrations.md).

An event id is processed **once**: the row that records it in the sync log carries the id under a
unique index, so a second delivery (even two at the same moment) is logged ``skipped`` and changes
nothing. An event that the practice's own rules refuse (the last administrator would be
deactivated, a job status that is not an allowed transition) is not an error and does not stop the
feed: it is kept as a ``failed`` sync item with the event inside, for a person to see and to retry
once the cause is gone. An event for something this practice does not have is ``skipped``.
"""

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import unit_of_work
from app.repositories.integration_logs import CursorRepository, SyncLogRepository
from app.services.integration_logs import record_failure, record_sync
from app.services.integration_outbox import park_failed
from app.services.integrations import IntegrationUnavailableError
from app.services.members import integrity_constraint

EVENT_INDEX = "uq_integration_sync_logs_event"
FEED_BATCH = 100
EventOutcome = Literal["applied", "skipped", "failed"]


@dataclass(slots=True)
class Applied:
    """What applying one event did. ``failed`` means a rule of this practice refused it; nothing
    was changed (check before writing)."""

    outcome: EventOutcome
    message: str
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    details: dict[str, Any] = field(default_factory=dict)
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class Handled:
    event_id: str
    type: str
    outcome: EventOutcome
    duplicate: bool
    message: str
    entity_id: uuid.UUID | None
    item_id: uuid.UUID | None = None


class FeedEvent(Protocol):
    @property
    def event_id(self) -> str: ...

    @property
    def type(self) -> str: ...


class FeedBatch[E](Protocol):
    @property
    def events(self) -> Sequence[E]: ...

    @property
    def cursor(self) -> str | None: ...

    @property
    def has_more(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class Polled:
    available: bool
    fetched: int
    applied: int
    skipped: int
    failed: int
    has_more: bool
    cursor: str | None
    results: list[Handled]


async def handle_event_once(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    integration: str,
    event_id: str,
    event_type: str,
    external_id: str,
    source: str,
    apply: Callable[[], Awaitable[Applied]],
    park_payload: dict[str, Any],
    label: str,
) -> Handled:
    """Apply one event once. Raises when applying it failed unexpectedly (the failure is logged
    in a transaction of its own)."""
    logs = SyncLogRepository(session)
    if await logs.event_processed(organization_id, integration, event_id):
        return await _duplicate(session, organization_id, integration, event_id, event_type, external_id, source)
    applied: Applied | None = None
    item_id: uuid.UUID | None = None
    try:
        async with unit_of_work(session):
            if not await logs.event_processed(organization_id, integration, event_id):
                applied = await apply()
                details = {"event_id": event_id, "source": source, **applied.details}
                if applied.outcome == "failed":
                    item = await park_failed(
                        session,
                        organization_id,
                        integration,
                        operation="apply_event",
                        idempotency_key=f"event:{event_id}",
                        message=applied.message,
                        code=applied.failure_code or "REFUSED",
                        entity_type=applied.entity_type,
                        entity_id=applied.entity_id,
                        payload={"label": label, "event": park_payload},
                    )
                    item_id = item.id
                    await record_sync(
                        session,
                        organization_id,
                        integration=integration,
                        operation=event_type,
                        status="failed",
                        message=applied.message,
                        entity_type=applied.entity_type,
                        entity_id=applied.entity_id,
                        external_id=external_id,
                        details={**details, "item_id": str(item.id), "code": applied.failure_code},
                    )
                else:
                    await record_sync(
                        session,
                        organization_id,
                        integration=integration,
                        operation=event_type,
                        status="ok" if applied.outcome == "applied" else "skipped",
                        message=applied.message,
                        entity_type=applied.entity_type,
                        entity_id=applied.entity_id,
                        external_id=external_id,
                        external_event_id=event_id,
                        details=details,
                    )
    except IntegrityError as exc:
        if integrity_constraint(exc) == EVENT_INDEX:
            return await _duplicate(session, organization_id, integration, event_id, event_type, external_id, source)
        await _failed(organization_id, integration, event_id, event_type, external_id, source, exc)
        raise
    except Exception as exc:
        await _failed(organization_id, integration, event_id, event_type, external_id, source, exc)
        raise
    if applied is None:
        return await _duplicate(
            session, organization_id, integration, event_id, event_type, external_id, source
        )
    return Handled(
        event_id=event_id,
        type=event_type,
        outcome=applied.outcome,
        duplicate=False,
        message=applied.message,
        entity_id=applied.entity_id,
        item_id=item_id,
    )


async def _duplicate(
    session: AsyncSession,
    organization_id: uuid.UUID,
    integration: str,
    event_id: str,
    event_type: str,
    external_id: str,
    source: str,
) -> Handled:
    message = f"Event {event_id} was already processed: nothing changed."
    async with unit_of_work(session):
        await record_sync(
            session,
            organization_id,
            integration=integration,
            operation=event_type,
            status="skipped",
            message=message,
            external_id=external_id,
            details={"event_id": event_id, "source": source, "duplicate": True},
        )
    return Handled(event_id, event_type, "skipped", True, message, None)


async def _failed(
    organization_id: uuid.UUID,
    integration: str,
    event_id: str,
    event_type: str,
    external_id: str,
    source: str,
    exc: Exception,
) -> None:
    await record_failure(
        organization_id,
        integration=integration,
        operation=event_type,
        external_id=external_id,
        message=f"Event {event_id} failed: {type(exc).__name__}.",
        details={"event_id": event_id, "source": source},
    )


async def poll_feed[E: FeedEvent](
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    integration: str,
    fetch: Callable[[str | None, int], Awaitable[FeedBatch[E]]],
    handle: Callable[[E], Awaitable[Handled]],
    service_name: str,
) -> Polled[E]:
    """Read the events after the practice's cursor and apply each through ``handle`` (the handler
    a pushed event goes through too). The cursor moves forward only when the whole batch was
    handled: an event whose handling raised stops the batch, the cursor stays, and the next call
    reads the same events again (the ones that went through are skipped as repeats). A refused
    event is kept as a failed item and does not stop the feed. While the service is down the
    answer is ``available: false`` and the failure is logged."""
    cursors = CursorRepository(session)
    cursor = await cursors.get(organization_id, integration)
    try:
        batch = await fetch(cursor, FEED_BATCH)
    except IntegrationUnavailableError:
        async with unit_of_work(session):
            await record_sync(
                session,
                organization_id,
                integration=integration,
                operation="poll",
                status="failed",
                message=f"The {service_name} is unavailable.",
                details={"cursor": cursor},
            )
        return Polled(False, 0, 0, 0, 0, False, cursor, [])
    results: list[Handled] = []
    stopped = False
    for event in batch.events:
        try:
            results.append(await handle(event))
        except Exception:
            stopped = True
            break
    new_cursor = cursor if stopped or batch.cursor is None else batch.cursor
    applied = sum(1 for r in results if r.outcome == "applied")
    refused = sum(1 for r in results if r.outcome == "failed")
    skipped = len(results) - applied - refused
    failed = refused + (1 if stopped else 0)
    async with unit_of_work(session):
        if new_cursor is not None and new_cursor != cursor:
            await cursors.save(organization_id, integration, new_cursor)
        await record_sync(
            session,
            organization_id,
            integration=integration,
            operation="poll",
            status="failed" if failed else "ok",
            message=(
                f"Read {len(batch.events)} event(s): {applied} applied, {skipped} skipped"
                + (f", {failed} failed." if failed else ".")
            ),
            details={
                "fetched": len(batch.events),
                "applied": applied,
                "skipped": skipped,
                "failed": failed,
            },
        )
    return Polled(
        available=True,
        fetched=len(batch.events),
        applied=applied,
        skipped=skipped,
        failed=failed,
        has_more=batch.has_more or stopped,
        cursor=new_cursor,
        results=results,
    )
