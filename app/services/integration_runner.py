"""Running the integrations: "run sync now" for each, retrying sync items, and the scheduled run.

* ``run_now``: central client (read the change feed and synchronise the linked clients), central
  user (read the change feed), practice management (import and link jobs, read the change feed, push
  the statuses queued) and bookkeeping (queue the issued documents that have no item, send what is
  due). A switched-off integration answers 409 ``INTEGRATION_DISABLED``; an unavailable one does not
  fail the call: ``available`` is false and the work stays queued.
* ``retry_item`` / ``retry_failed``: put ``failed`` items back to work **now** and report what
  happened; an item that is already ``ok`` is never sent again.
* ``run_scheduled``: the periodic worker job: queue the issued documents of every practice that has
  bookkeeping on, then send every item that is due. It never touches billing: failures end up on
  the items.
"""

import uuid
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import NotFoundError
from app.db.session import unit_of_work
from app.repositories.integration_items import ConnectionRepository, SyncItemRepository
from app.schemas.integration_connections import (
    IntegrationKey,
    RetryFailedResult,
    RetryOutcome,
    SyncItemRead,
    SyncItemRetried,
    SyncRunResult,
)
from app.services.bookkeeping_sync import scan_documents, send_item
from app.services.central_clients import get_central_gateway
from app.services.central_events import CentralEventService
from app.services.central_sync import CentralClientService
from app.services.central_user_sync import CentralUserService
from app.services.central_user_sync import apply_event_handler as central_user_handler
from app.services.common import record_audit
from app.services.integration_connections import ConnectionService, record_run, validate_config
from app.services.integration_outbox import (
    MAX_ATTEMPTS,
    ItemHandler,
    OutboxProcessor,
    RunSummary,
)
from app.services.integrations import (
    BOOKKEEPING,
    CENTRAL_CLIENT,
    CENTRAL_USER,
    PRACTICE_MANAGEMENT,
    get_central_user_gateway,
    get_practice_management_gateway,
)
from app.services.pm_sync import PmSyncService, push_status_item
from app.services.pm_sync import apply_event_handler as pm_handler
from app.services.policy import OrgContext

RETRY_FAILED_LIMIT = 200
SCHEDULED_BATCH = 100
MAX_IMPORT_PAGES = 10


def build_handlers(
    ctx: OrgContext | None = None, rctx: RequestContext | None = None
) -> dict[tuple[str, str], ItemHandler]:
    """What runs each kind of item. The handlers that retry a refused inbound event need the
    caller's context, so only a person-driven runner has them (the worker never retries those)."""
    handlers: dict[tuple[str, str], ItemHandler] = {
        (BOOKKEEPING, "send_invoice"): send_item,
        (BOOKKEEPING, "send_credit_note"): send_item,
        (PRACTICE_MANAGEMENT, "push_job_status"): push_status_item,
        (PRACTICE_MANAGEMENT, "push_task_status"): push_status_item,
    }
    if ctx is not None and rctx is not None:
        handlers[(PRACTICE_MANAGEMENT, "apply_event")] = pm_handler(ctx, rctx)
        handlers[(CENTRAL_USER, "apply_event")] = central_user_handler(
            ctx, rctx, get_central_user_gateway()
        )
    return handlers


class IntegrationRunner:
    def __init__(self, session: AsyncSession, ctx: OrgContext, rctx: RequestContext) -> None:
        self.session = session
        self.ctx = ctx
        self.rctx = rctx
        self.connections = ConnectionService(session)
        self.items = SyncItemRepository(session)
        self.processor = OutboxProcessor(session, build_handlers(ctx, rctx))

    # --- Run now --------------------------------------------------------------------------

    async def run_now(self, key: str) -> SyncRunResult:
        org_id = self.ctx.organization_id
        await self.connections.ensure_enabled(org_id, key)
        queued = 0
        details: dict[str, int] = {}
        available = True
        summary = RunSummary()
        if key == CENTRAL_CLIENT:
            available, details = await self._run_central_client()
        elif key == CENTRAL_USER:
            available, details = await self._run_central_user()
        elif key == PRACTICE_MANAGEMENT:
            available, details, summary = await self._run_practice_management()
        else:
            queued, summary = await self._run_bookkeeping()
            available = not summary.unavailable
        pending = (await self.items.counts(org_id)).get(key, {}).get("pending", 0)
        message = self._message(key, available, queued, summary, details)
        return SyncRunResult(
            integration=cast(IntegrationKey, key),
            available=available,
            queued=queued,
            processed=summary.processed,
            ok=summary.ok,
            retrying=summary.retrying,
            failed=summary.failed,
            skipped=summary.skipped,
            pending=pending,
            details=details,
            message=message,
        )

    async def _run_central_client(self) -> tuple[bool, dict[str, int]]:
        gateway = get_central_gateway()
        events = await CentralEventService(self.session, gateway).process_changes(
            self.ctx, self.rctx
        )
        synced = await CentralClientService(self.session, gateway).sync_all(self.ctx, self.rctx)
        return events.available and synced.available, {
            "events_applied": events.applied,
            "events_skipped": events.skipped,
            "events_failed": events.failed,
            "clients_synced": synced.synced,
            "clients_changed": synced.changed,
            "clients_stale": synced.stale,
            "clients_failed": synced.failed,
        }

    async def _run_central_user(self) -> tuple[bool, dict[str, int]]:
        polled = await CentralUserService(self.session, get_central_user_gateway()).process_changes(
            self.ctx, self.rctx
        )
        await self._record(
            CENTRAL_USER,
            polled.available and polled.failed == 0,
            "The central user service failed.",
        )
        return polled.available, {
            "events_applied": polled.applied,
            "events_skipped": polled.skipped,
            "events_failed": polled.failed,
        }

    async def _run_practice_management(self) -> tuple[bool, dict[str, int], RunSummary]:
        service = PmSyncService(self.session, get_practice_management_gateway())
        totals = {
            "created": 0,
            "linked": 0,
            "updated": 0,
            "unchanged": 0,
            "skipped": 0,
            "failed": 0,
        }
        available, cursor = True, None
        for _ in range(MAX_IMPORT_PAGES):
            page = await service.import_jobs(self.ctx, self.rctx, cursor=cursor, limit=100)
            available = available and page.available
            for name in totals:
                totals[name] += getattr(page, name)
            if not page.available or not page.has_more:
                break
            cursor = page.next_cursor
        polled = await service.process_changes(self.ctx, self.rctx)
        available = available and polled.available
        summary = await self.processor.run_due(
            organization_id=self.ctx.organization_id, integration=PRACTICE_MANAGEMENT
        )
        await self._record(
            PRACTICE_MANAGEMENT,
            available and totals["failed"] == 0 and polled.failed == 0 and summary.failed == 0,
            "Practice management could not be synchronised.",
        )
        details = {
            **{f"jobs_{name}": count for name, count in totals.items()},
            "events_applied": polled.applied,
            "events_skipped": polled.skipped,
            "events_failed": polled.failed,
        }
        return available and not summary.unavailable, details, summary

    async def _run_bookkeeping(self) -> tuple[int, RunSummary]:
        org_id = self.ctx.organization_id
        config = await self.connections.config_of(org_id, BOOKKEEPING)
        queued = await scan_documents(self.session, org_id, config)
        summary = await self.processor.run_due(organization_id=org_id, integration=BOOKKEEPING)
        return queued, summary

    async def _record(self, key: str, succeeded: bool, error: str) -> None:
        async with unit_of_work(self.session):
            await record_run(
                self.session,
                self.ctx.organization_id,
                key,
                succeeded=succeeded,
                error=None if succeeded else error,
            )

    @staticmethod
    def _message(
        key: str, available: bool, queued: int, summary: RunSummary, details: dict[str, int]
    ) -> str:
        if not available:
            return "The other system is unavailable: nothing was lost, the work stays queued."
        if key == BOOKKEEPING:
            return (
                f"Queued {queued} document(s); {summary.processed} handled: {summary.ok} sent, "
                f"{summary.retrying} to retry, {summary.failed} failed."
            )
        if key == PRACTICE_MANAGEMENT:
            return (
                f"Jobs: {details['jobs_created']} created, {details['jobs_linked']} linked, "
                f"{details['jobs_updated']} updated, {details['jobs_skipped']} skipped; "
                f"{summary.ok} status change(s) sent."
            )
        applied = details.get("events_applied", 0)
        return f"{applied} change(s) applied."

    # --- Retries --------------------------------------------------------------------------

    async def retry_item(self, item_id: uuid.UUID) -> SyncItemRetried:
        org_id = self.ctx.organization_id
        item = await self.items.get(org_id, item_id)
        if item is None:
            raise NotFoundError("Sync item not found.")
        await self.connections.ensure_enabled(org_id, item.integration)
        result = await self.processor.retry(org_id, item_id)
        if result is None:
            raise NotFoundError("Sync item not found.")
        item, summary = result
        outcome = self._retry_outcome(item.status, summary)
        async with unit_of_work(self.session):
            await record_audit(
                self.session,
                self.ctx,
                self.rctx,
                "integration.item_retried",
                "integration",
                item.id,
                {
                    "integration": item.integration,
                    "operation": item.operation,
                    "label": item.payload.get("label"),
                    "outcome": outcome,
                },
            )
        return SyncItemRetried(
            outcome=outcome, item=SyncItemRead.from_model(item, max_attempts=MAX_ATTEMPTS)
        )

    @staticmethod
    def _retry_outcome(status: str, summary: RunSummary) -> RetryOutcome:
        if not summary.processed:
            return "already_synced" if status == "ok" else "skipped"
        if summary.ok:
            return "sent"
        if summary.skipped:
            return "skipped"
        return "retry_scheduled" if summary.retrying else "failed"

    async def retry_failed(self, key: str) -> RetryFailedResult:
        org_id = self.ctx.organization_id
        await self.connections.ensure_enabled(org_id, key)
        ids = await self.items.failed_ids(org_id, key, limit=RETRY_FAILED_LIMIT)
        ok = failed = 0
        for item_id in ids:
            result = await self.processor.retry(org_id, item_id)
            if result is None:
                continue
            if result[1].ok or result[1].skipped:
                ok += 1
            else:
                failed += 1
        remaining = (await self.items.counts(org_id)).get(key, {}).get("failed", 0)
        async with unit_of_work(self.session):
            await record_audit(
                self.session,
                self.ctx,
                self.rctx,
                "integration.failed_items_retried",
                "integration",
                None,
                {"integration": key, "requeued": len(ids), "ok": ok, "failed": failed},
            )
        return RetryFailedResult(requeued=len(ids), ok=ok, failed=failed, remaining=remaining)


async def run_scheduled(session: AsyncSession) -> dict[str, Any]:
    """The periodic worker job: queue the issued documents of every practice that has bookkeeping on
    and send every item that is due (bookkeeping and practice management statuses). Failures stay
    on the items; nothing here can reach billing."""
    queued = 0
    for connection in await ConnectionRepository(session).enabled(BOOKKEEPING):
        config = validate_config(BOOKKEEPING, connection.config)
        queued += await scan_documents(session, connection.organization_id, config)
    summary = await OutboxProcessor(session, build_handlers()).run_due(limit=SCHEDULED_BATCH)
    return {
        "queued": queued,
        "processed": summary.processed,
        "ok": summary.ok,
        "retrying": summary.retrying,
        "failed": summary.failed,
    }


__all__ = [
    "IntegrationRunner",
    "build_handlers",
    "run_scheduled",
]
