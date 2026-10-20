"""WIP transfers: move work in progress from one job of a client to another job of the same client.

A transfer moves *value*, not the items: it writes a ``transfer_out`` row for the source job and a
``transfer_in`` row for the target job in the immutable ledger (the time entries and expenses
stay as they were recorded). Both jobs must be the client's and open. A reversal writes the two
rows back. Like an adjustment, the moved amount counts until the job is next billed.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import NotFoundError
from app.db.models import Job, WipTransfer
from app.db.session import unit_of_work
from app.repositories.billing_common import BillingReferenceRepository
from app.repositories.wip_atoms import WipAtom, WipAtomRepository, WipFilters
from app.repositories.wip_ledger import TransferFilters, WipLedgerRepository
from app.schemas.wip import (
    TransferCreate,
    TransferItemRead,
    TransferRead,
    TransferReverse,
)
from app.services import period_locks
from app.services.billing_common import conflict, invalid, now_utc, record
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
from app.utils.job_rules import OPEN_JOB_STATUSES
from app.utils.pagination import Page, PageParams

_MOVABLE = ("time", "disbursement", "expense")


class WipTransferService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = WipLedgerRepository(session)
        self.refs = BillingReferenceRepository(session)
        self.ledger = WipLedger(session)
        self.atoms = WipAtomRepository(session)

    # --- Reads -------------------------------------------------------------------------

    @staticmethod
    def _read(row: WipTransfer, lookup: Lookup) -> TransferRead:
        created_by = lookup.person(row.created_by)
        assert created_by is not None
        return TransferRead(
            id=row.id,
            client=lookup.client(row.client_id),
            from_job=lookup.job(row.from_job_id),
            to_job=lookup.job(row.to_job_id),
            component=row.component,
            amount=row.amount,
            effective_date=row.effective_date,
            reason=row.reason,
            items=[
                TransferItemRead(kind=i["kind"], id=uuid.UUID(str(i["id"])), value=int(i["value"]))
                for i in row.items
            ],
            status=row.status,
            created_by=created_by,
            created_at=row.created_at,
            reversed_by=lookup.person(row.reversed_by),
            reversed_at=row.reversed_at,
            reversal_reason=row.reversal_reason,
        )

    async def _reads(self, ctx: OrgContext, rows: list[WipTransfer]) -> list[TransferRead]:
        lookup = await lookup_refs(
            self.refs,
            ctx.organization_id,
            client_ids={r.client_id for r in rows},
            job_ids=[j for r in rows for j in (r.from_job_id, r.to_job_id)],
            user_ids=[u for r in rows for u in (r.created_by, r.reversed_by)],
        )
        return [self._read(r, lookup) for r in rows]

    async def get(self, ctx: OrgContext, transfer_id: uuid.UUID) -> TransferRead:
        row = await self.repo.get_transfer(ctx.organization_id, transfer_id)
        if row is None:
            raise NotFoundError("Transfer not found.")
        return (await self._reads(ctx, [row]))[0]

    async def list_transfers(
        self, ctx: OrgContext, f: TransferFilters, params: PageParams
    ) -> Page[TransferRead]:
        page = await self.repo.list_transfers(ctx.organization_id, f, params)
        return Page(items=await self._reads(ctx, page.items), next_cursor=page.next_cursor)

    # --- Create ------------------------------------------------------------------------

    async def _job(self, ctx: OrgContext, job_id: uuid.UUID, field: str) -> Job:
        job = await self.refs.get_job(ctx.organization_id, job_id)
        if job is None:
            raise NotFoundError("Job not found.", details={"field": field})
        return job

    @staticmethod
    def _ensure_open(job: Job) -> None:
        if job.status not in OPEN_JOB_STATUSES:
            raise conflict(
                "JOB_NOT_OPEN",
                "WIP can only be moved between open jobs.",
                details={"job_id": str(job.id), "status": job.status},
            )

    async def _chosen(
        self, ctx: OrgContext, source: Job, data: TransferCreate
    ) -> tuple[int, str, list[dict[str, Any]]]:
        """The amount, the component and the item references of what the person chose."""
        organization = ctx.organization
        if data.items is None:
            assert data.amount is not None
            component = data.component or default_component(source)
            available = await self.ledger.scope_value(
                organization, source.client_id, source.id, component
            )
            if data.amount > available:
                raise conflict(
                    "INSUFFICIENT_WIP",
                    "The job has less work in progress than the amount to transfer.",
                    details={"available": max(available, 0), "requested": data.amount},
                )
            return data.amount, component, []
        today = practice_today(organization)
        atoms = await self.atoms.load_atoms(
            organization.id,
            as_at=today,
            today=today,
            timezone=organization.timezone,
            require_approval=organization.require_timesheet_approval,
            fixed_fee_mode=organization.fixed_fee_wip_mode,
            filters=WipFilters(client_id=source.client_id, job_id=source.id),
        )
        by_key: dict[tuple[str, uuid.UUID], WipAtom] = {
            (a.kind, a.id): a for a in atoms if a.kind in _MOVABLE and a.job_id == source.id
        }
        moved = await self.repo.live_transferred_items(ctx.organization_id, source.id)
        missing: list[dict[str, str]] = []
        chosen: list[WipAtom] = []
        for ref in data.items:
            atom = by_key.get((ref.kind, ref.id))
            if atom is None:
                missing.append({"kind": ref.kind, "id": str(ref.id), "reason": "not_wip_of_job"})
            elif atom.id in moved:
                raise conflict(
                    "ITEM_ALREADY_TRANSFERRED",
                    "Some of these items have been transferred already.",
                    details=[{"kind": ref.kind, "id": str(ref.id)}],
                )
            else:
                chosen.append(atom)
        if missing:
            raise conflict(
                "WIP_NOT_AVAILABLE",
                "Some items are not unbilled work in progress of the source job.",
                details=missing,
            )
        if len({a.id for a in chosen}) != len(chosen):
            raise invalid("DUPLICATE_ITEM", "items", "Each item may appear only once.")
        components = {a.component for a in chosen}
        if len(components) != 1:
            raise invalid(
                "MIXED_COMPONENTS",
                "items",
                "Transfer time, fixed fee and expense items separately.",
            )
        amount = sum(a.value for a in chosen)
        if amount <= 0:
            raise conflict("NOTHING_TO_TRANSFER", "The chosen items have no value to move.")
        refs = [{"kind": a.kind, "id": str(a.id), "value": a.value} for a in chosen]
        return amount, components.pop(), refs

    async def create(
        self, ctx: OrgContext, data: TransferCreate, rctx: RequestContext
    ) -> TransferRead:
        organization = ctx.organization
        async with unit_of_work(self.session):
            source = await self._job(ctx, data.from_job_id, "from_job_id")
            target = await self._job(ctx, data.to_job_id, "to_job_id")
            if source.id == target.id:
                raise invalid("SAME_JOB", "to_job_id", "Choose a different job to move it to.")
            if source.client_id != target.client_id:
                raise invalid(
                    "CLIENT_MISMATCH",
                    "to_job_id",
                    "WIP can only be transferred between jobs of the same client.",
                )
            self._ensure_open(source)
            self._ensure_open(target)
            effective = effective_date_of(organization, data.effective_date)
            await period_locks.assert_open(self.session, ctx.organization_id, effective, "wip")
            amount, component, item_refs = await self._chosen(ctx, source, data)
            client = await self.refs.get_client(ctx.organization_id, source.client_id)
            transfer_id = new_id()
            shared: dict[str, Any] = {
                "scope": "amount",
                "component": component,
                "client_id": source.client_id,
                "effective_date": effective,
                "reason": data.reason,
                "actor_user_id": ctx.user.id,
                "source_type": "transfer",
                "source_id": transfer_id,
                "item_count": len(item_refs),
                "item_refs": item_refs,
            }
            out = await self.ledger.post(
                ctx.organization_id, kind="transfer_out", job_id=source.id, amount=-amount, **shared
            )
            into = await self.ledger.post(
                ctx.organization_id, kind="transfer_in", job_id=target.id, amount=amount, **shared
            )
            row = await self.repo.add_transfer(
                WipTransfer(
                    id=transfer_id,
                    organization_id=ctx.organization_id,
                    client_id=source.client_id,
                    from_job_id=source.id,
                    to_job_id=target.id,
                    component=component,
                    amount=amount,
                    effective_date=effective,
                    reason=data.reason,
                    items=item_refs,
                    status="applied",
                    out_movement_id=out.id,
                    in_movement_id=into.id,
                    created_by=ctx.user.id,
                )
            )
            await record(
                self.session,
                ctx,
                rctx,
                "wip.transferred",
                target_type="wip_transfer",
                target_id=row.id,
                meta=self._meta(row, client.name if client else None),
            )
        return (await self._reads(ctx, [row]))[0]

    # --- Reverse -----------------------------------------------------------------------

    async def reverse(
        self,
        ctx: OrgContext,
        transfer_id: uuid.UUID,
        data: TransferReverse,
        rctx: RequestContext,
    ) -> TransferRead:
        async with unit_of_work(self.session):
            row = await self.repo.get_transfer(ctx.organization_id, transfer_id, for_update=True)
            if row is None:
                raise NotFoundError("Transfer not found.")
            if row.status != "applied":
                raise conflict(
                    "TRANSFER_NOT_REVERSIBLE",
                    "This transfer has been reversed already.",
                    details={"status": row.status},
                )
            today = practice_today(ctx.organization)
            await period_locks.assert_open(self.session, ctx.organization_id, today, "wip")
            live = await self.ledger.in_force(ctx.organization)
            if row.out_movement_id not in live or row.in_movement_id not in live:
                raise conflict(
                    "TRANSFER_SETTLED",
                    (
                        "One of the jobs has been billed since: the transfer can no longer be "
                        "reversed."
                    ),
                )
            for original_id, job_id, signed in (
                (row.out_movement_id, row.from_job_id, row.amount),
                (row.in_movement_id, row.to_job_id, -row.amount),
            ):
                await self.ledger.post(
                    ctx.organization_id,
                    kind="reversal",
                    scope="amount",
                    component=row.component,
                    client_id=row.client_id,
                    job_id=job_id,
                    amount=signed,
                    effective_date=today,
                    reason=data.reason,
                    actor_user_id=ctx.user.id,
                    reversal_of_id=original_id,
                    source_type="transfer",
                    source_id=row.id,
                )
            row.status = "reversed"
            row.reversed_by = ctx.user.id
            row.reversed_at = now_utc()
            row.reversal_reason = data.reason
            await self.session.flush()
            await record(
                self.session,
                ctx,
                rctx,
                "wip.transfer_reversed",
                target_type="wip_transfer",
                target_id=row.id,
                meta={**self._meta(row, None), "reason": data.reason},
            )
        return (await self._reads(ctx, [row]))[0]

    @staticmethod
    def _meta(row: WipTransfer, client_name: str | None) -> dict[str, Any]:
        return {
            "client_id": str(row.client_id),
            "client_name": client_name,
            "from_job_id": str(row.from_job_id),
            "to_job_id": str(row.to_job_id),
            "component": row.component,
            "amount": row.amount,
            "item_count": len(row.items),
            "effective_date": row.effective_date.isoformat(),
            "reason": row.reason,
        }
