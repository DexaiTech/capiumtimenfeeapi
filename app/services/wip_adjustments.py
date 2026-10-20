"""WIP adjustments: write a job's WIP on or off by an amount, with a reason, an approval above the
practice's threshold, and a reversal.

An adjustment is a request (``wip_adjustments``) that becomes a movement of the immutable ledger
when it takes effect: at once when the amount is within ``wip_adjustment_approval_threshold``,
otherwise when somebody with ``wip.approve`` approves it. Nobody approves their own request
without ``organization.update``. A reversal is a new ledger row; the original stays. The amount
counts in the job's WIP until the job is next billed (docs/modules/wip.md).
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import AuthorizationError, NotFoundError, ValidationError
from app.db.models import WipAdjustment
from app.db.session import unit_of_work
from app.repositories.billing_common import BillingReferenceRepository
from app.repositories.wip_ledger import AdjustmentFilters, WipLedgerRepository
from app.schemas.wip import (
    AdjustmentApprove,
    AdjustmentCounts,
    AdjustmentCreate,
    AdjustmentRead,
    AdjustmentReject,
    AdjustmentReverse,
)
from app.services import period_locks, policy
from app.services.billing_common import conflict, get_client_or_404, now_utc, record
from app.services.policy import OrgContext
from app.services.reports import practice_today
from app.services.wip_ledger import (
    Lookup,
    WipLedger,
    default_component,
    effective_date_of,
    lookup_refs,
)
from app.utils.ids import new_id
from app.utils.pagination import Page, PageParams


def _signed(kind: str, amount: int) -> int:
    return amount if kind == "write_on" else -amount


class WipAdjustmentService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = WipLedgerRepository(session)
        self.refs = BillingReferenceRepository(session)
        self.ledger = WipLedger(session)

    # --- Reads -------------------------------------------------------------------------

    def _read(self, row: WipAdjustment, lookup: Lookup, in_force: set[uuid.UUID]) -> AdjustmentRead:
        requested_by = lookup.person(row.requested_by)
        assert requested_by is not None
        return AdjustmentRead(
            id=row.id,
            client=lookup.client(row.client_id),
            job=lookup.job(row.job_id),
            kind=row.kind,
            component=row.component,
            amount=row.amount,
            signed_amount=_signed(row.kind, row.amount),
            effective_date=row.effective_date,
            reason=row.reason,
            status=row.status,
            approval_required=row.approval_required,
            threshold=row.threshold,
            requested_by=requested_by,
            requested_at=row.created_at,
            decided_by=lookup.person(row.decided_by),
            decided_at=row.decided_at,
            decision_note=row.decision_note,
            reversed_by=lookup.person(row.reversed_by),
            reversed_at=row.reversed_at,
            reversal_reason=row.reversal_reason,
            in_force=row.movement_id in in_force if row.movement_id else False,
            movement_id=row.movement_id,
        )

    async def _reads(self, ctx: OrgContext, rows: list[WipAdjustment]) -> list[AdjustmentRead]:
        lookup = await lookup_refs(
            self.refs,
            ctx.organization_id,
            client_ids={r.client_id for r in rows},
            job_ids=[r.job_id for r in rows],
            user_ids=[u for r in rows for u in (r.requested_by, r.decided_by, r.reversed_by)],
        )
        in_force = await self.ledger.in_force(ctx.organization)
        return [self._read(r, lookup, in_force) for r in rows]

    async def get(self, ctx: OrgContext, adjustment_id: uuid.UUID) -> AdjustmentRead:
        row = await self.repo.get_adjustment(ctx.organization_id, adjustment_id)
        if row is None:
            raise NotFoundError("Adjustment not found.")
        return (await self._reads(ctx, [row]))[0]

    async def list(
        self, ctx: OrgContext, f: AdjustmentFilters, params: PageParams
    ) -> Page[AdjustmentRead]:
        page = await self.repo.list_adjustments(ctx.organization_id, f, params)
        return Page(items=await self._reads(ctx, page.items), next_cursor=page.next_cursor)

    async def counts(self, ctx: OrgContext) -> AdjustmentCounts:
        counts, pending_value = await self.repo.adjustment_counts(ctx.organization_id)
        return AdjustmentCounts(
            pending=counts.get("pending", 0),
            approved=counts.get("approved", 0),
            rejected=counts.get("rejected", 0),
            reversed=counts.get("reversed", 0),
            pending_value=pending_value,
        )

    # --- Writes ------------------------------------------------------------------------

    async def _ensure_available(self, ctx: OrgContext, row: WipAdjustment) -> None:
        """A write-off cannot take more than the WIP the job has."""
        if row.kind != "write_off":
            return
        available = await self.ledger.scope_value(
            ctx.organization, row.client_id, row.job_id, row.component
        )
        if row.amount > available:
            raise conflict(
                "INSUFFICIENT_WIP",
                "The write-off is more than the work in progress there is to write off.",
                details={"available": max(available, 0), "requested": row.amount},
            )

    async def _post(self, ctx: OrgContext, row: WipAdjustment, *, approved: bool) -> uuid.UUID:
        movement = await self.ledger.post(
            ctx.organization_id,
            kind=row.kind,
            scope="amount",
            component=row.component,
            client_id=row.client_id,
            job_id=row.job_id,
            amount=_signed(row.kind, row.amount),
            effective_date=row.effective_date,
            reason=row.reason,
            actor_user_id=row.requested_by,
            approved_by=ctx.user.id if approved else None,
            approved_at=now_utc() if approved else None,
            source_type="adjustment",
            source_id=row.id,
        )
        return movement.id

    async def create(
        self, ctx: OrgContext, data: AdjustmentCreate, rctx: RequestContext
    ) -> AdjustmentRead:
        organization = ctx.organization
        async with unit_of_work(self.session):
            client = await get_client_or_404(self.refs, ctx.organization_id, data.client_id)
            job = None
            if data.job_id is not None:
                job = await self.refs.get_job(ctx.organization_id, data.job_id)
                if job is None or job.client_id != client.id:
                    raise ValidationError.for_field(
                        "job_id", "The job must be one of the client's jobs."
                    )
            effective = effective_date_of(organization, data.effective_date)
            await period_locks.assert_open(self.session, ctx.organization_id, effective, "wip")
            component = data.component or default_component(job)
            threshold = organization.wip_adjustment_approval_threshold
            required = data.amount > threshold
            percent_threshold = organization.wip_write_off_approval_threshold_percent
            if data.kind == "write_off" and percent_threshold is not None:
                available = await self.ledger.scope_value(
                    organization, client.id, job.id if job else None, component
                )
                # basis points: amount/available > percent_threshold/10000, cross-multiplied to
                # avoid floating point.
                if available > 0 and data.amount * 10000 > available * percent_threshold:
                    required = True
            row = WipAdjustment(
                id=new_id(),
                organization_id=ctx.organization_id,
                client_id=client.id,
                job_id=job.id if job else None,
                kind=data.kind,
                component=component,
                amount=data.amount,
                effective_date=effective,
                reason=data.reason,
                status="pending" if required else "approved",
                approval_required=required,
                threshold=threshold,
                requested_by=ctx.user.id,
            )
            await self._ensure_available(ctx, row)
            if not required:
                row.movement_id = await self._post(ctx, row, approved=False)
            row = await self.repo.add_adjustment(row)
            await record(
                self.session,
                ctx,
                rctx,
                "wip.adjustment_created",
                target_type="wip_adjustment",
                target_id=row.id,
                meta=self._meta(row, client.name),
            )
        return (await self._reads(ctx, [row]))[0]

    async def approve(
        self,
        ctx: OrgContext,
        adjustment_id: uuid.UUID,
        data: AdjustmentApprove,
        rctx: RequestContext,
    ) -> AdjustmentRead:
        async with unit_of_work(self.session):
            row = await self._pending(ctx, adjustment_id)
            if row.requested_by == ctx.user.id and not policy.has_permission(
                ctx, "organization.update"
            ):
                raise AuthorizationError(
                    "You cannot approve your own adjustment.", code="SELF_APPROVAL_FORBIDDEN"
                )
            await period_locks.assert_open(
                self.session, ctx.organization_id, row.effective_date, "wip"
            )
            await self._ensure_available(ctx, row)
            row.movement_id = await self._post(ctx, row, approved=True)
            row.status = "approved"
            row.decided_by = ctx.user.id
            row.decided_at = now_utc()
            row.decision_note = data.note
            await self.session.flush()
            await record(
                self.session,
                ctx,
                rctx,
                "wip.adjustment_approved",
                target_type="wip_adjustment",
                target_id=row.id,
                meta={**self._meta(row, None), "note": data.note},
            )
        return (await self._reads(ctx, [row]))[0]

    async def reject(
        self,
        ctx: OrgContext,
        adjustment_id: uuid.UUID,
        data: AdjustmentReject,
        rctx: RequestContext,
    ) -> AdjustmentRead:
        async with unit_of_work(self.session):
            row = await self._pending(ctx, adjustment_id)
            row.status = "rejected"
            row.decided_by = ctx.user.id
            row.decided_at = now_utc()
            row.decision_note = data.reason
            await self.session.flush()
            await record(
                self.session,
                ctx,
                rctx,
                "wip.adjustment_rejected",
                target_type="wip_adjustment",
                target_id=row.id,
                meta={**self._meta(row, None), "reason": data.reason},
            )
        return (await self._reads(ctx, [row]))[0]

    async def reverse(
        self,
        ctx: OrgContext,
        adjustment_id: uuid.UUID,
        data: AdjustmentReverse,
        rctx: RequestContext,
    ) -> AdjustmentRead:
        async with unit_of_work(self.session):
            row = await self.repo.get_adjustment(
                ctx.organization_id, adjustment_id, for_update=True
            )
            if row is None:
                raise NotFoundError("Adjustment not found.")
            if row.status != "approved" or row.movement_id is None:
                raise conflict(
                    "ADJUSTMENT_NOT_REVERSIBLE",
                    "Only an approved adjustment can be reversed.",
                    details={"status": row.status},
                )
            today = practice_today(ctx.organization)
            await period_locks.assert_open(self.session, ctx.organization_id, today, "wip")
            if row.movement_id not in await self.ledger.in_force(ctx.organization):
                raise conflict(
                    "ADJUSTMENT_SETTLED",
                    "The job has been billed since: this adjustment is part of that billing "
                    "and can no longer be reversed.",
                )
            reversal = await self.ledger.post(
                ctx.organization_id,
                kind="reversal",
                scope="amount",
                component=row.component,
                client_id=row.client_id,
                job_id=row.job_id,
                amount=-_signed(row.kind, row.amount),
                effective_date=today,
                reason=data.reason,
                actor_user_id=ctx.user.id,
                reversal_of_id=row.movement_id,
                source_type="adjustment",
                source_id=row.id,
            )
            row.status = "reversed"
            row.reversed_by = ctx.user.id
            row.reversed_at = now_utc()
            row.reversal_reason = data.reason
            row.reversal_movement_id = reversal.id
            await self.session.flush()
            await record(
                self.session,
                ctx,
                rctx,
                "wip.adjustment_reversed",
                target_type="wip_adjustment",
                target_id=row.id,
                meta={**self._meta(row, None), "reason": data.reason},
            )
        return (await self._reads(ctx, [row]))[0]

    async def _pending(self, ctx: OrgContext, adjustment_id: uuid.UUID) -> WipAdjustment:
        row = await self.repo.get_adjustment(ctx.organization_id, adjustment_id, for_update=True)
        if row is None:
            raise NotFoundError("Adjustment not found.")
        if row.status != "pending":
            raise conflict(
                "ADJUSTMENT_NOT_PENDING",
                "This adjustment has been decided already.",
                details={"status": row.status},
            )
        return row

    @staticmethod
    def _meta(row: WipAdjustment, client_name: str | None) -> dict[str, Any]:
        return {
            "kind": row.kind,
            "component": row.component,
            "amount": row.amount,
            "client_id": str(row.client_id),
            "client_name": client_name,
            "job_id": str(row.job_id) if row.job_id else None,
            "effective_date": row.effective_date.isoformat(),
            "reason": row.reason,
            "status": row.status,
            "approval_required": row.approval_required,
            "threshold": row.threshold,
        }
