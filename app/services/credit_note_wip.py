"""What happens to the WIP of a credit note: returned to WIP, or written off. Once per credit
note.

A credit note reduces what a client owes on an issued invoice; its lines are free text, so it does
not say which WIP items it credits. ``return`` therefore reinstates **whole invoice items**, in the
order of the invoice's lines, while their billed value fits in the credited net (a credit that
completes the invoice's credit returns every item). Their allocations are released
(``reversed_at``) and they are unbilled WIP again; a credit that fits no whole item returns
nothing (409). ``write_off`` leaves the items billed and records the credited net as a WIP
write-off. Either way the ledger gets memo rows (``scope = 'items'``) and the invoice stays
issued.
"""

import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import NotFoundError
from app.db.models import CreditNote, CreditNoteWip, Invoice, Job, WipAllocation
from app.db.session import unit_of_work
from app.repositories.billing_common import BillingReferenceRepository
from app.repositories.wip import WipRepository
from app.repositories.wip_ledger import WipLedgerRepository
from app.schemas.wip import CreditNoteWipCreate, CreditNoteWipItem, CreditNoteWipRead
from app.services import period_locks
from app.services.billing_common import conflict, now_utc, record
from app.services.policy import OrgContext
from app.services.reports import practice_today
from app.services.wip import LockedItems, lock_allocation_items, set_items_status
from app.services.wip_ledger import WipLedger, default_component, lookup_refs
from app.utils.ids import new_id
from app.utils.money import apportion

_Group = tuple[uuid.UUID | None, str]


def _item_of(allocation: WipAllocation) -> tuple[str, uuid.UUID]:
    if allocation.time_entry_id:
        return "time", allocation.time_entry_id
    if allocation.expense_id:
        return "expense", allocation.expense_id
    assert allocation.disbursement_id is not None
    return "disbursement", allocation.disbursement_id


def _job_of(items: LockedItems, allocation: WipAllocation) -> uuid.UUID | None:
    kind, item_id = _item_of(allocation)
    if kind == "time":
        return next(t.job_id for t in items.time_entries if t.id == item_id)
    if kind == "expense":
        return next(e.job_id for e in items.expenses if e.id == item_id)
    return next(d.job_id for d in items.disbursements if d.id == item_id)


def returned_items(
    allocations: Sequence[WipAllocation], net_credited: int, completes_credit: bool
) -> list[WipAllocation]:
    """The allocations a credit note returns to WIP: all of them when the credit completes the
    invoice's credit, else the whole items, in line order, that fit in the credited net."""
    if completes_credit:
        return list(allocations)
    chosen: list[WipAllocation] = []
    covered = 0
    for allocation in allocations:
        if covered + allocation.billed_value <= net_credited:
            chosen.append(allocation)
            covered += allocation.billed_value
    return chosen


def _component(kind: str, job: Job | None) -> str:
    return default_component(job) if kind == "time" else "expense"


class CreditNoteWipService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = WipLedgerRepository(session)
        self.refs = BillingReferenceRepository(session)
        self.wip = WipRepository(session)
        self.ledger = WipLedger(session)

    # --- Reads -------------------------------------------------------------------------

    async def get(self, ctx: OrgContext, credit_note_id: uuid.UUID) -> CreditNoteWipRead:
        note = await self.repo.get_credit_note(ctx.organization_id, credit_note_id)
        if note is None:
            raise NotFoundError("Credit note not found.")
        row = await self.repo.get_credit_note_wip(ctx.organization_id, credit_note_id)
        if row is None:
            raise NotFoundError("This credit note has no WIP record yet.")
        return await self._read(ctx, row)

    async def _read(self, ctx: OrgContext, row: CreditNoteWip) -> CreditNoteWipRead:
        movements = await self.repo.movements_of_source(
            ctx.organization_id, "credit_note", row.credit_note_id
        )
        items = [
            CreditNoteWipItem(
                kind=ref["kind"], id=uuid.UUID(str(ref["id"])), wip_value=int(ref["wip_value"])
            )
            for m in movements
            for ref in m.item_refs
        ]
        lookup = await lookup_refs(
            self.refs,
            ctx.organization_id,
            client_ids={row.client_id},
            job_ids=[],
            user_ids=[row.created_by],
        )
        created_by = lookup.person(row.created_by)
        client = lookup.client(row.client_id)
        assert created_by is not None and client is not None
        return CreditNoteWipRead(
            id=row.id,
            credit_note_id=row.credit_note_id,
            invoice_id=row.invoice_id,
            client=client,
            mode=row.mode,
            reason=row.reason,
            net_credited=row.net_credited,
            wip_value=row.wip_value,
            item_count=row.item_count,
            items=items,
            created_by=created_by,
            created_at=row.created_at,
        )

    # --- Create ------------------------------------------------------------------------

    async def create(
        self,
        ctx: OrgContext,
        credit_note_id: uuid.UUID,
        data: CreditNoteWipCreate,
        rctx: RequestContext,
    ) -> CreditNoteWipRead:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            note = await self.repo.get_credit_note(org_id, credit_note_id)
            if note is None:
                raise NotFoundError("Credit note not found.")
            invoice = await self.repo.lock_invoice(org_id, note.invoice_id)
            if invoice is None:
                raise NotFoundError("Invoice not found.")
            if invoice.status != "issued":
                raise conflict(
                    "INVOICE_NOT_ISSUED", "Only a credit note on an issued invoice can be used."
                )
            if await self.repo.get_credit_note_wip(org_id, note.id) is not None:
                raise conflict(
                    "CREDIT_NOTE_WIP_DONE",
                    "The WIP of this credit note has been dealt with already.",
                )
            today = practice_today(ctx.organization)
            await period_locks.assert_open(self.session, org_id, today, "wip")
            allocations = await self.repo.live_invoice_allocations(org_id, invoice.id)
            if data.mode == "return":
                value, count = await self._return(ctx, note, invoice, allocations, data, today)
            else:
                value, count = await self._write_off(ctx, note, allocations, data, today)
            row = await self.repo.add_credit_note_wip(
                CreditNoteWip(
                    id=new_id(),
                    organization_id=org_id,
                    credit_note_id=note.id,
                    client_id=note.client_id,
                    invoice_id=invoice.id,
                    mode=data.mode,
                    reason=data.reason,
                    net_credited=note.net_total,
                    wip_value=value,
                    item_count=count,
                    created_by=ctx.user.id,
                )
            )
            action = (
                "credit_note.wip_returned"
                if data.mode == "return"
                else "credit_note.wip_written_off"
            )
            await record(
                self.session,
                ctx,
                rctx,
                action,
                target_type="credit_note",
                target_id=note.id,
                meta={
                    "number": note.number,
                    "invoice_id": str(invoice.id),
                    "invoice_number": invoice.number,
                    "client_id": str(note.client_id),
                    "mode": data.mode,
                    "reason": data.reason,
                    "net_credited": note.net_total,
                    "wip_value": value,
                    "item_count": count,
                },
            )
        return await self._read(ctx, row)

    async def _groups(
        self,
        organization_id: uuid.UUID,
        allocations: Sequence[WipAllocation],
        items: LockedItems,
    ) -> dict[_Group, list[WipAllocation]]:
        """The allocations by the job and the WIP component they belong to."""
        job_ids = [_job_of(items, a) for a in allocations]
        jobs = await self.refs.jobs_by_ids(organization_id, job_ids)
        groups: dict[_Group, list[WipAllocation]] = defaultdict(list)
        for allocation, job_id in zip(allocations, job_ids, strict=True):
            job = jobs.get(job_id) if job_id else None
            groups[(job_id, _component(_item_of(allocation)[0], job))].append(allocation)
        return groups

    async def _return(
        self,
        ctx: OrgContext,
        note: CreditNote,
        invoice: Invoice,
        allocations: Sequence[WipAllocation],
        data: CreditNoteWipCreate,
        today: date,
    ) -> tuple[int, int]:
        org_id = ctx.organization_id
        credited = await self.repo.credited_net(org_id, invoice.id)
        chosen = returned_items(allocations, note.net_total, credited >= invoice.net_total)
        if not chosen:
            raise conflict(
                "NOTHING_TO_RETURN",
                "The credit does not cover a whole WIP item of the invoice, so nothing can be "
                "returned to WIP. Write it off instead.",
                details={"net_credited": note.net_total, "items_on_invoice": len(allocations)},
            )
        items = await lock_allocation_items(self.session, org_id, chosen)
        set_items_status(items, "unbilled")
        await self.session.flush()
        await self.wip.reverse_allocations(org_id, [a.id for a in chosen], now_utc())
        movements = []
        for (job_id, component), members in (await self._groups(org_id, chosen, items)).items():
            refs: list[dict[str, Any]] = [
                {"kind": _item_of(a)[0], "id": str(_item_of(a)[1]), "wip_value": a.wip_value}
                for a in members
            ]
            amount = sum(a.wip_value for a in members)
            if amount == 0:
                continue
            movements.append(
                self.ledger.build(
                    org_id,
                    kind="credit_return",
                    scope="items",
                    component=component,
                    client_id=note.client_id,
                    job_id=job_id,
                    amount=amount,
                    effective_date=today,
                    reason=data.reason,
                    actor_user_id=ctx.user.id,
                    source_type="credit_note",
                    source_id=note.id,
                    item_count=len(members),
                    item_refs=refs,
                )
            )
        await self.repo.add_movements(movements)
        return sum(a.wip_value for a in chosen), len(chosen)

    async def _write_off(
        self,
        ctx: OrgContext,
        note: CreditNote,
        allocations: Sequence[WipAllocation],
        data: CreditNoteWipCreate,
        today: date,
    ) -> tuple[int, int]:
        """The credited net as a write-off, shared over the jobs the invoice billed by value."""
        org_id = ctx.organization_id
        weights: dict[_Group, int] = {}
        if allocations:
            items = await lock_allocation_items(self.session, org_id, allocations)
            for group, members in (await self._groups(org_id, allocations, items)).items():
                weights[group] = sum(a.billed_value for a in members)
        else:
            weights[(None, "time")] = 1
        groups = list(weights)
        parts = apportion(note.net_total, [weights[g] for g in groups])
        movements = [
            self.ledger.build(
                org_id,
                kind="credit_write_off",
                scope="items",
                component=component,
                client_id=note.client_id,
                job_id=job_id,
                amount=-part,
                effective_date=today,
                reason=data.reason,
                actor_user_id=ctx.user.id,
                source_type="credit_note",
                source_id=note.id,
            )
            for (job_id, component), part in zip(groups, parts, strict=True)
            if part > 0
        ]
        await self.repo.add_movements(movements)
        return note.net_total, 0
