"""Fees billed in advance, with the time assigned to them later (ticket APT69).

Some practices bill the year's fees at its start ("Fees in advance 2026/27") and match the time
to that bill as the work is done. The advance invoice is an ordinary invoice whose manual line is
marked ``is_advance``: it is issued, sent, paid, credited and voided like any other.

Once it is issued, unbilled WIP of the same client (time and disbursements, by the rules every
billing flow uses: ``lock_wip_items``) can be **assigned** to the advance line. Each item gets a
WIP allocation on that line, dated ``assigned_on`` (today), and becomes ``billed``: it leaves WIP
and shows under the invoice's "Billed from WIP". The invoice, its lines, totals and VAT never
change. The advance is used up in item date order: an item bills its WIP value while the advance
lasts, the item that crosses the end bills what is left and later items bill 0. What the advance
does not cover is a write-off of those items (``billed_value < wip_value``), as for time on a fixed
fee that is already billed. Whatever is never used stays billed: nothing happens to it.

**Un-assigning** takes items off the line again: their allocations are deleted (as when a draft
line is removed) and the items are unbilled WIP once more. The advance is then used again, oldest
first, on the work left on the line, so work written off for want of it is covered again. Both
run under the invoice row lock, then the items (the order every billing flow uses), check the WIP
period lock for today (and, to un-assign, for the days the items were assigned, and of the work
whose billed value changes) and are audited.
"""

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import AuthorizationError, NotFoundError
from app.db.models import Disbursement, Invoice, InvoiceLine, TimeEntry, WipAllocation
from app.db.session import unit_of_work
from app.repositories.billing_common import BillingReferenceRepository
from app.repositories.invoices import InvoiceRepository
from app.repositories.wip import WipRepository
from app.schemas.billing import PersonRef, hours_of
from app.schemas.invoices import AdvanceAssign, AdvanceItemRead, AdvanceRead
from app.services import period_locks, policy
from app.services.billing_common import conflict, get_client_or_404, record, today_for
from app.services.policy import OrgContext
from app.services.wip import lock_allocation_items, lock_wip_items, set_items_status
from app.utils.ids import new_id


async def advance_reads(
    session: AsyncSession,
    organization_id: uuid.UUID,
    lines: Sequence[InvoiceLine],
    allocations: Sequence[WipAllocation],
) -> dict[uuid.UUID, AdvanceRead]:
    """For each advance line of an invoice: the work assigned to it and what is left of it."""
    advance_ids = {line.id for line in lines if line.is_advance}
    if not advance_ids:
        return {}
    live = [a for a in allocations if a.invoice_line_id in advance_ids and a.reversed_at is None]
    entries = {
        e.id: e
        for e in await _rows(session, TimeEntry, organization_id, [a.time_entry_id for a in live])
    }
    disbursements = {
        d.id: d
        for d in await _rows(
            session, Disbursement, organization_id, [a.disbursement_id for a in live]
        )
    }
    names = await BillingReferenceRepository(session).member_names(
        organization_id, [e.user_id for e in entries.values()]
    )
    found: dict[uuid.UUID, AdvanceRead] = {}
    for line in lines:
        if not line.is_advance:
            continue
        items: list[AdvanceItemRead] = []
        for allocation in live:
            if allocation.invoice_line_id != line.id:
                continue
            if allocation.time_entry_id and (entry := entries.get(allocation.time_entry_id)):
                items.append(
                    AdvanceItemRead(
                        kind="time",
                        id=entry.id,
                        date=entry.entry_date,
                        person=PersonRef(user_id=entry.user_id, full_name=names.get(entry.user_id)),
                        description=entry.description,
                        minutes=entry.minutes,
                        hours=hours_of(entry.minutes),
                        wip_value=allocation.wip_value,
                        billed_value=allocation.billed_value,
                        assigned_on=allocation.assigned_on,
                    )
                )
            elif allocation.disbursement_id and (
                disbursement := disbursements.get(allocation.disbursement_id)
            ):
                items.append(
                    AdvanceItemRead(
                        kind="disbursement",
                        id=disbursement.id,
                        date=disbursement.incurred_on,
                        person=None,
                        description=disbursement.description,
                        minutes=None,
                        hours=None,
                        wip_value=allocation.wip_value,
                        billed_value=allocation.billed_value,
                        assigned_on=allocation.assigned_on,
                    )
                )
        items.sort(key=lambda i: (i.date, i.kind != "time", str(i.id)))
        used = sum(i.billed_value for i in items)
        wip_value = sum(i.wip_value for i in items)
        found[line.id] = AdvanceRead(
            amount=line.net_amount,
            used=used,
            remaining=max(line.net_amount - used, 0),
            wip_value=wip_value,
            written_off=wip_value - used,
            items=items,
        )
    return found


async def _rows[T: TimeEntry | Disbursement](
    session: AsyncSession,
    model: type[T],
    organization_id: uuid.UUID,
    ids: Sequence[uuid.UUID | None],
) -> list[T]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return []
    stmt = select(model).where(model.organization_id == organization_id, model.id.in_(wanted))
    return list((await session.execute(stmt)).scalars())


class AdvanceService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.invoices = InvoiceRepository(session)
        self.wip = WipRepository(session)
        self.refs = BillingReferenceRepository(session)

    async def assign(
        self,
        ctx: OrgContext,
        invoice_id: uuid.UUID,
        line_id: uuid.UUID,
        data: AdvanceAssign,
        rctx: RequestContext,
    ) -> None:
        """Bill the chosen WIP against the advance line, without changing the invoice."""
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            invoice, line = await self._advance_line(ctx, invoice_id, line_id)
            client = await get_client_or_404(self.refs, org_id, invoice.client_id)
            today = today_for(ctx.organization)
            await period_locks.assert_open(self.session, org_id, today, "wip")
            items = await lock_wip_items(
                self.session, ctx.organization, client, data.time_entry_ids, data.disbursement_ids
            )
            jobs = await self.refs.jobs_by_ids(org_id, [e.job_id for e in items.time_entries])
            fixed = [
                {"time_entry_id": str(e.id), "reason": "fixed_fee_job"}
                for e in items.time_entries
                if e.job_id in jobs and jobs[e.job_id].billing_basis == "fixed"
            ]
            if fixed:
                raise conflict(
                    "WIP_NOT_AVAILABLE",
                    "Time on a fixed-fee job is billed through its fixed fee, not an advance.",
                    details=fixed,
                )
            assigned = await self._live(org_id, line)
            remaining = max(line.net_amount - sum(a.billed_value for a in assigned), 0)
            # Oldest work first: the advance covers it while it lasts.
            ordered: list[tuple[Any, uuid.UUID, int, str]] = [
                (e.entry_date, e.id, e.charge_value, "time_entry_id") for e in items.time_entries
            ] + [(d.incurred_on, d.id, d.amount, "disbursement_id") for d in items.disbursements]
            ordered.sort(key=lambda o: (o[0], o[3] != "time_entry_id", o[1]))
            allocations = []
            for _day, item_id, value, column in ordered:
                billed = min(value, remaining)
                remaining -= billed
                allocations.append(
                    WipAllocation(
                        id=new_id(),
                        organization_id=org_id,
                        invoice_id=invoice.id,
                        invoice_line_id=line.id,
                        wip_value=value,
                        billed_value=billed,
                        assigned_on=today,
                        **{column: item_id},
                    )
                )
            if not policy.has_permission(ctx, "wip.write_off") and any(
                a.billed_value < a.wip_value for a in allocations
            ):
                raise AuthorizationError(
                    "The advance does not cover all of this work: what it does not cover is "
                    "written off, which needs the permission to write off WIP."
                )
            await self.wip.add_allocations(allocations)
            set_items_status(items, "billed")
            await self.session.flush()
            billed_total = sum(a.billed_value for a in allocations)
            await record(
                self.session,
                ctx,
                rctx,
                "invoice.time_assigned",
                target_type="invoice",
                target_id=invoice.id,
                meta={
                    **_meta(invoice, client.name, line),
                    "time_entry_ids": [str(e.id) for e in items.time_entries],
                    "disbursement_ids": [str(d.id) for d in items.disbursements],
                    "item_count": items.count,
                    "wip_value": items.wip_value,
                    "billed_value": billed_total,
                    "written_off": items.wip_value - billed_total,
                    "remaining": remaining,
                },
            )

    async def unassign(
        self,
        ctx: OrgContext,
        invoice_id: uuid.UUID,
        line_id: uuid.UUID,
        data: AdvanceAssign,
        rctx: RequestContext,
    ) -> None:
        """Take items off the advance line: they are unbilled WIP again."""
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            invoice, line = await self._advance_line(ctx, invoice_id, line_id)
            assigned = await self._live(org_id, line)
            times, disbursements = set(data.time_entry_ids), set(data.disbursement_ids)
            chosen = [
                a
                for a in assigned
                if a.time_entry_id in times or a.disbursement_id in disbursements
            ]
            missing = (times - {a.time_entry_id for a in chosen}) | (
                disbursements - {a.disbursement_id for a in chosen}
            )
            if missing:
                raise conflict(
                    "NOT_ASSIGNED",
                    "Some of the items are not assigned to this advance.",
                    details={"items": sorted(str(i) for i in missing)},
                )
            # What the advance no longer covers goes to the work left on it, oldest first.
            kept = [a for a in assigned if a not in chosen]
            rebilled = await self._reallocate(org_id, line, kept)
            days = {today_for(ctx.organization)} | {
                a.assigned_on for a in [*chosen, *(r for r, _ in rebilled)] if a.assigned_on
            }
            await period_locks.assert_open_many(self.session, org_id, days, "wip")
            items = await lock_allocation_items(self.session, org_id, chosen)
            set_items_status(items, "unbilled")
            await self.session.flush()
            await self.wip.delete_allocations(org_id, [a.id for a in chosen])
            client = await self.refs.get_client(org_id, invoice.client_id)
            released = sum(a.billed_value for a in chosen)
            await record(
                self.session,
                ctx,
                rctx,
                "invoice.time_unassigned",
                target_type="invoice",
                target_id=invoice.id,
                meta={
                    **_meta(invoice, client.name if client else None, line),
                    "time_entry_ids": [str(a.time_entry_id) for a in chosen if a.time_entry_id],
                    "disbursement_ids": [
                        str(a.disbursement_id) for a in chosen if a.disbursement_id
                    ],
                    "item_count": len(chosen),
                    "wip_value": sum(a.wip_value for a in chosen),
                    "billed_value": released,
                    "remaining": max(
                        line.net_amount - sum(a.billed_value for a in assigned) + released, 0
                    ),
                    "rebilled": {
                        str(a.time_entry_id or a.disbursement_id): [old, a.billed_value]
                        for a, old in rebilled
                    },
                },
            )

    async def _advance_line(
        self, ctx: OrgContext, invoice_id: uuid.UUID, line_id: uuid.UUID
    ) -> tuple[Invoice, InvoiceLine]:
        """The invoice (row locked) and its advance line; time goes on an issued one only."""
        invoice = await self.invoices.get(ctx.organization_id, invoice_id, for_update=True)
        if invoice is None:
            raise NotFoundError("Invoice not found.")
        line = await self.invoices.get_line(ctx.organization_id, invoice.id, line_id)
        if line is None:
            raise NotFoundError("Invoice line not found.")
        if not line.is_advance:
            raise conflict(
                "NOT_AN_ADVANCE",
                "Time can only be assigned to a line of fees billed in advance.",
            )
        if invoice.status != "issued":
            raise conflict(
                "INVOICE_NOT_ISSUED",
                "Time is assigned to fees billed in advance once the invoice is issued.",
                details={"status": invoice.status},
            )
        return invoice, line

    async def _reallocate(
        self, organization_id: uuid.UUID, line: InvoiceLine, allocations: Sequence[WipAllocation]
    ) -> list[tuple[WipAllocation, int]]:
        """Uses the advance on ``allocations`` again oldest work first, as assigning does: each
        item bills its WIP value while the advance lasts. Returns the allocations whose billed
        value changed, each with the value it had."""
        entries = {
            e.id: e.entry_date
            for e in await _rows(
                self.session, TimeEntry, organization_id, [a.time_entry_id for a in allocations]
            )
        }
        disbursements = {
            d.id: d.incurred_on
            for d in await _rows(
                self.session,
                Disbursement,
                organization_id,
                [a.disbursement_id for a in allocations],
            )
        }

        def oldest_first(a: WipAllocation) -> tuple[date, bool, uuid.UUID]:
            if a.time_entry_id is not None:
                return entries[a.time_entry_id], False, a.time_entry_id
            item_id = cast(uuid.UUID, a.disbursement_id)
            return disbursements[item_id], True, item_id

        remaining = line.net_amount
        changed: list[tuple[WipAllocation, int]] = []
        for allocation in sorted(allocations, key=oldest_first):
            billed = min(allocation.wip_value, remaining)
            remaining -= billed
            if billed != allocation.billed_value:
                changed.append((allocation, allocation.billed_value))
                allocation.billed_value = billed
        return changed

    async def _live(self, organization_id: uuid.UUID, line: InvoiceLine) -> list[WipAllocation]:
        return [
            a
            for a in await self.wip.allocations_for_line(organization_id, line.id)
            if a.reversed_at is None
        ]


def _meta(invoice: Invoice, client_name: str | None, line: InvoiceLine) -> dict[str, Any]:
    return {
        "number": invoice.number,
        "client_id": str(invoice.client_id),
        "client_name": client_name,
        "line_id": str(line.id),
        "line": line.description,
        "advance": line.net_amount,
    }
