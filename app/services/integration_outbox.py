"""The outbox: queue, run and retry what must reach (or be applied from) another system.

An item is one document, change or event with an **idempotency key** that is unique per practice
and integration, so the same thing is never queued twice and a retry never duplicates it (the
gateways take the key as well, and book it once). ``pending`` items wait for ``next_attempt_at``;
a failed attempt is retried with a growing backoff (1 minute, then 5, 25 and 125) until
``MAX_ATTEMPTS`` attempts have been made, and an error that retrying cannot cure (an unmapped
account, a refusal) fails the item at once. ``failed`` items wait for a person: ``retry`` puts one
back to work, immediately. Every attempt writes a row to the sync log; nothing here ever raises
into the work that queued the item (billing, time, jobs).

One runner at a time handles an item: it locks the row, calls the handler in a savepoint (what the
handler wrote is undone when it fails) and records the outcome in the same transaction.
"""

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import IntegrationSyncItem
from app.db.session import unit_of_work
from app.repositories.integration_items import SyncItemRepository
from app.services.integration_connections import record_run
from app.services.integration_logs import record_sync
from app.services.integrations import IntegrationRejectedError, IntegrationUnavailableError
from app.utils.ids import new_id

log = get_logger(__name__)

MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 60
BACKOFF_FACTOR = 5
BACKOFF_MAX_SECONDS = 6 * 3600
DEFAULT_BATCH = 50
MAX_MESSAGE = 1000


def utcnow() -> datetime:
    return datetime.now(UTC)


def backoff(attempts: int) -> timedelta:
    """The wait after the ``attempts``-th failed attempt: 60 s, 300 s, 1500 s, 7500 s ..."""
    seconds = BACKOFF_BASE_SECONDS * BACKOFF_FACTOR ** max(attempts - 1, 0)
    return timedelta(seconds=min(seconds, BACKOFF_MAX_SECONDS))


class ItemFailure(Exception):  # noqa: N818  (the documented name)
    """A handler's way to fail an item: ``code`` is a stable word for the failure
    (``UNMAPPED``, ``INVALID_TRANSITION`` ...), ``retryable`` says whether trying again later
    can help."""

    def __init__(self, message: str, code: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ItemOutcome:
    """What a handler returns when the item is done: ``ok`` (sent or applied) or ``skipped``
    (nothing to do, ``message`` says why)."""

    status: Literal["ok", "skipped"] = "ok"
    external_ref: str | None = None
    message: str | None = None


type ItemHandler = Callable[[AsyncSession, IntegrationSyncItem], Awaitable[ItemOutcome]]


@dataclass(frozen=True, slots=True)
class Enqueued:
    item: IntegrationSyncItem
    created: bool


@dataclass(slots=True)
class RunSummary:
    processed: int = 0
    ok: int = 0
    retrying: int = 0
    failed: int = 0
    skipped: int = 0
    unavailable: bool = False
    errors: list[str] = field(default_factory=list)

    def add(self, other: "RunSummary") -> None:
        self.processed += other.processed
        self.ok += other.ok
        self.retrying += other.retrying
        self.failed += other.failed
        self.skipped += other.skipped
        self.unavailable = self.unavailable or other.unavailable
        self.errors.extend(other.errors)


async def enqueue_once(
    session: AsyncSession,
    organization_id: uuid.UUID,
    integration: str,
    *,
    operation: str,
    idempotency_key: str,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
    direction: str = "outbound",
) -> Enqueued:
    """Queue the item unless one with this key exists (then that one is returned, unchanged)."""
    items = SyncItemRepository(session)
    created = await items.insert_once(
        {
            "id": new_id(),
            "organization_id": organization_id,
            "integration": integration,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "direction": direction,
            "operation": operation,
            "idempotency_key": idempotency_key,
            "status": "pending",
            "next_attempt_at": utcnow(),
            "payload": payload or {},
        }
    )
    found = (
        await items.get(organization_id, created)
        if created is not None
        else await items.get_by_key(organization_id, integration, idempotency_key)
    )
    assert found is not None
    return Enqueued(found, created is not None)


async def enqueue_latest(
    session: AsyncSession,
    organization_id: uuid.UUID,
    integration: str,
    *,
    operation: str,
    idempotency_key: str,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> IntegrationSyncItem:
    """Queue the item, or put the existing one with this key back to ``pending`` with the new
    payload: for a change where only the latest value matters (a job's status)."""
    items = SyncItemRepository(session)
    item_id = await items.upsert_latest(
        {
            "id": new_id(),
            "organization_id": organization_id,
            "integration": integration,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "direction": "outbound",
            "operation": operation,
            "idempotency_key": idempotency_key,
            "status": "pending",
            "next_attempt_at": utcnow(),
            "payload": payload or {},
        }
    )
    found = await items.get(organization_id, item_id)
    assert found is not None
    return found


async def park_failed(
    session: AsyncSession,
    organization_id: uuid.UUID,
    integration: str,
    *,
    operation: str,
    idempotency_key: str,
    message: str,
    code: str,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> IntegrationSyncItem:
    """Keep an inbound change that could not be applied as a ``failed`` item, so a person sees
    it and can retry it once the cause is gone. A second refusal of the same change updates
    the item instead of adding another."""
    items = SyncItemRepository(session)
    now = utcnow()
    created = await items.insert_once(
        {
            "id": new_id(),
            "organization_id": organization_id,
            "integration": integration,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "direction": "inbound",
            "operation": operation,
            "idempotency_key": idempotency_key,
            "status": "failed",
            "attempts": 1,
            "next_attempt_at": None,
            "last_attempt_at": now,
            "last_error": message[:MAX_MESSAGE],
            "last_error_code": code,
            "payload": payload or {},
        }
    )
    found = (
        await items.get(organization_id, created)
        if created is not None
        else await items.get_by_key(organization_id, integration, idempotency_key)
    )
    assert found is not None
    if created is None and found.status != "ok":
        found.status = "failed"
        found.next_attempt_at = None
        found.attempts += 1
        found.last_attempt_at = now
        found.last_error = message[:MAX_MESSAGE]
        found.last_error_code = code
        found.payload = payload or found.payload
        await session.flush()
    return found


class OutboxProcessor:
    """Runs items with the handler registered for their ``(integration, operation)``."""

    def __init__(
        self,
        session: AsyncSession,
        handlers: Mapping[tuple[str, str], ItemHandler],
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.session = session
        self.handlers = handlers
        self.clock = clock
        self.items = SyncItemRepository(session)

    # --- Many items -----------------------------------------------------------------------

    async def run_due(
        self,
        *,
        organization_id: uuid.UUID | None = None,
        integration: str | None = None,
        limit: int = DEFAULT_BATCH,
    ) -> RunSummary:
        """Run the pending items whose time has come, oldest first, for the practices where the
        integration is on; an item another runner holds is left to it."""
        due = await self.items.due(
            limit=limit,
            now=self.clock(),
            organization_id=organization_id,
            integration=integration,
        )
        total = RunSummary()
        by_run: dict[tuple[uuid.UUID, str], RunSummary] = {}
        for org_id, item_id in due:
            result = await self.run_item(org_id, item_id)
            if result is None:
                continue
            item, summary = result
            total.add(summary)
            by_run.setdefault((org_id, item.integration), RunSummary()).add(summary)
        for (org_id, key), summary in by_run.items():
            await self._record_run(org_id, key, summary)
        return total

    # --- One item -------------------------------------------------------------------------

    async def retry(
        self, organization_id: uuid.UUID, item_id: uuid.UUID
    ) -> tuple[IntegrationSyncItem, RunSummary] | None:
        """Run a ``failed`` or waiting item now, with its attempts counted from zero. An item
        that is already ``ok`` or ``skipped`` is returned untouched: nothing is sent twice."""
        result = await self.run_item(organization_id, item_id, manual=True)
        if result is not None and result[1].processed:
            await self._record_run(organization_id, result[0].integration, result[1])
        return result

    async def run_item(
        self, organization_id: uuid.UUID, item_id: uuid.UUID, *, manual: bool = False
    ) -> tuple[IntegrationSyncItem, RunSummary] | None:
        """One attempt at one item, in a transaction of its own. ``None`` when the item does not
        exist or (automatic runs) another runner holds it."""
        summary = RunSummary()
        async with unit_of_work(self.session):
            item = await self.items.lock_for_processing(
                organization_id, item_id, skip_locked=not manual
            )
            if item is None:
                return None
            now = self.clock()
            if manual:
                if item.status in ("ok", "skipped"):
                    return item, summary
                item.status = "pending"
                item.attempts = 0
                item.next_attempt_at = now
            elif item.status != "pending" or (
                item.next_attempt_at is not None and item.next_attempt_at > now
            ):
                return item, summary

            attempt = item.attempts + 1
            item.last_attempt_at = now
            summary.processed = 1
            outcome, failure = await self._call(item)
            if failure is None:
                assert outcome is not None
                self._succeed(item, outcome, attempt, now)
                summary.ok, summary.skipped = (1, 0) if outcome.status == "ok" else (0, 1)
            else:
                retry = failure.retryable and attempt < MAX_ATTEMPTS
                self._fail(item, failure, attempt, now, retry=retry)
                if retry:
                    summary.retrying = 1
                else:
                    summary.failed = 1
                summary.unavailable = failure.code == "UNAVAILABLE"
                summary.errors.append(failure.message)
            await self.session.flush()
            await self._log(item, attempt, failure)
        return item, summary

    async def _call(
        self, item: IntegrationSyncItem
    ) -> tuple[ItemOutcome | None, ItemFailure | None]:
        handler = self.handlers.get((item.integration, item.operation))
        if handler is None:
            failure = ItemFailure(f"Nothing handles {item.operation}.", "NO_HANDLER")
            return None, failure
        try:
            async with self.session.begin_nested():
                return await handler(self.session, item), None
        except ItemFailure as failure:
            return None, failure
        except IntegrationUnavailableError as exc:
            message = str(exc) or "The other system is not available."
            return None, ItemFailure(message, "UNAVAILABLE", retryable=True)
        except IntegrationRejectedError as exc:
            message = str(exc) or "The other system refused this."
            return None, ItemFailure(message, "REJECTED")
        except Exception as exc:
            log.exception("sync_item_error", item_id=str(item.id), integration=item.integration)
            return None, ItemFailure(
                f"Unexpected error ({type(exc).__name__}); it will be tried again.",
                "ERROR",
                retryable=True,
            )

    @staticmethod
    def _succeed(
        item: IntegrationSyncItem, outcome: ItemOutcome, attempt: int, now: datetime
    ) -> None:
        item.status = outcome.status
        item.attempts = attempt
        item.next_attempt_at = None
        item.completed_at = now
        item.external_ref = outcome.external_ref
        item.last_error = outcome.message[:MAX_MESSAGE] if outcome.message else None
        item.last_error_code = "SKIPPED" if outcome.status == "skipped" else None

    @staticmethod
    def _fail(
        item: IntegrationSyncItem, failure: ItemFailure, attempt: int, now: datetime, *, retry: bool
    ) -> None:
        item.attempts = attempt
        item.last_error = failure.message[:MAX_MESSAGE]
        item.last_error_code = failure.code
        item.completed_at = None
        if retry:
            item.status = "pending"
            item.next_attempt_at = now + backoff(attempt)
        else:
            item.status = "failed"
            item.next_attempt_at = None

    async def _log(
        self, item: IntegrationSyncItem, attempt: int, failure: ItemFailure | None
    ) -> None:
        if failure is None:
            status = "ok" if item.status == "ok" else "skipped"
            message = item.last_error or f"{item.operation} done."
        else:
            status = "failed"
            message = failure.message
        await record_sync(
            self.session,
            item.organization_id,
            integration=item.integration,
            operation=item.operation,
            status=status,
            message=message,
            entity_type=item.entity_type,
            entity_id=item.entity_id,
            external_id=item.external_ref,
            direction=item.direction,
            details={
                "item_id": str(item.id),
                "attempt": attempt,
                "idempotency_key": item.idempotency_key,
                "code": item.last_error_code,
                "will_retry": item.status == "pending",
                "next_attempt_at": (
                    item.next_attempt_at.isoformat() if item.next_attempt_at else None
                ),
            },
        )

    async def _record_run(
        self, organization_id: uuid.UUID, integration: str, summary: RunSummary
    ) -> None:
        failed = summary.retrying + summary.failed
        async with unit_of_work(self.session):
            await record_run(
                self.session,
                organization_id,
                integration,
                succeeded=failed == 0,
                error=summary.errors[-1] if summary.errors else None,
            )
