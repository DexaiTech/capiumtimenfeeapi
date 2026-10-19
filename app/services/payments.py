"""Payments received and their allocation to invoices.

Every allocation change locks the invoices involved (in id order) and then the payment,
re-reads them under the locks and only then checks the balances, so concurrent
allocations can never over-allocate an invoice or a payment.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import NotFoundError, ValidationError
from app.db.models import Client, Invoice, Payment, PaymentAllocation
from app.db.session import unit_of_work
from app.repositories.billing_common import BillingReferenceRepository
from app.repositories.invoices import InvoiceRepository
from app.repositories.payments import PaymentFilters, PaymentRepository
from app.schemas.payments import (
    AllocationIn,
    AllocationsCreate,
    PaymentCreate,
    PaymentRead,
    PaymentReverse,
)
from app.services import billing_notifications, invoice_pdf
from app.services.billing_common import conflict, get_client_or_404, now_utc, record
from app.services.policy import OrgContext
from app.utils.pagination import Page, PageParams


def _ensure_not_void(payment: Payment) -> None:
    """A void payment, and a reversed one (which is void too), takes no more changes."""
    if payment.reversed_at is not None:
        raise conflict("PAYMENT_REVERSED", "This payment was reversed.")
    if payment.voided_at is not None:
        raise conflict("PAYMENT_VOID", "This payment is void.")


def _payment_meta(payment: Payment, client: Client | None) -> dict[str, object]:
    return {
        "client_id": str(payment.client_id),
        "client_name": client.name if client else None,
        "received_on": payment.received_on.isoformat(),
        "amount": payment.amount,
        "method": payment.method,
        "reference": payment.reference,
        "allocated_total": payment.allocated_total,
    }


class PaymentService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.payments = PaymentRepository(session)
        self.invoices = InvoiceRepository(session)
        self.refs = BillingReferenceRepository(session)

    # --- Reads -------------------------------------------------------------------------

    async def list_payments(
        self, ctx: OrgContext, filters: PaymentFilters, params: PageParams
    ) -> tuple[Page[Payment], list[PaymentRead]]:
        page = await self.payments.list_page(ctx.organization_id, filters, params)
        clients = await self.refs.clients_by_ids(
            ctx.organization_id, [p.client_id for p in page.items]
        )
        return page, [PaymentRead.build(p, clients.get(p.client_id), None) for p in page.items]

    async def get(self, ctx: OrgContext, payment_id: uuid.UUID) -> PaymentRead:
        payment = await self._get(ctx, payment_id)
        return await self._read(ctx, payment)

    async def _get(self, ctx: OrgContext, payment_id: uuid.UUID, lock: bool = False) -> Payment:
        payment = await self.payments.get(ctx.organization_id, payment_id, for_update=lock)
        if payment is None:
            raise NotFoundError("Payment not found.")
        return payment

    async def _read(self, ctx: OrgContext, payment: Payment) -> PaymentRead:
        client = await self.refs.get_client(ctx.organization_id, payment.client_id)
        allocations = await self.payments.allocations(ctx.organization_id, payment.id)
        return PaymentRead.build(payment, client, allocations)

    # --- Writes ------------------------------------------------------------------------

    async def create(
        self, ctx: OrgContext, data: PaymentCreate, rctx: RequestContext
    ) -> PaymentRead:
        async with unit_of_work(self.session):
            client = await get_client_or_404(self.refs, ctx.organization_id, data.client_id)
            invoice_ids = await self._target_ids(ctx, client, data.allocations, data.auto_allocate)
            invoices = await self.invoices.lock_many(ctx.organization_id, invoice_ids)
            payment = await self.payments.add(
                Payment(
                    organization_id=ctx.organization_id,
                    client_id=client.id,
                    received_on=data.received_on,
                    amount=data.amount,
                    method=data.method,
                    reference=data.reference,
                    notes=data.notes,
                    allocated_total=0,
                    created_by=ctx.user.id,
                )
            )
            await record(
                self.session,
                ctx,
                rctx,
                "payment.recorded",
                target_type="payment",
                target_id=payment.id,
                meta=_payment_meta(payment, client),
            )
            if data.allocations or data.auto_allocate:
                await self._allocate(ctx, rctx, payment, client, invoices, data.allocations)
            read = await self._read(ctx, payment)
        return read

    async def allocate(
        self,
        ctx: OrgContext,
        payment_id: uuid.UUID,
        data: AllocationsCreate,
        rctx: RequestContext,
    ) -> PaymentRead:
        async with unit_of_work(self.session):
            payment = await self._get(ctx, payment_id)
            _ensure_not_void(payment)
            client = await get_client_or_404(self.refs, ctx.organization_id, payment.client_id)
            invoice_ids = await self._target_ids(ctx, client, data.allocations, data.auto_allocate)
            # Lock order: invoices (id order), then the payment.
            invoices = await self.invoices.lock_many(ctx.organization_id, invoice_ids)
            payment = await self._get(ctx, payment_id, lock=True)
            _ensure_not_void(payment)
            allocated = await self._allocate(ctx, rctx, payment, client, invoices, data.allocations)
            if not allocated:
                raise conflict(
                    "NOTHING_TO_ALLOCATE",
                    "There is nothing to allocate: no unallocated money or no open invoices.",
                )
            read = await self._read(ctx, payment)
        return read

    async def remove_allocation(
        self,
        ctx: OrgContext,
        payment_id: uuid.UUID,
        allocation_id: uuid.UUID,
        rctx: RequestContext,
    ) -> PaymentRead:
        async with unit_of_work(self.session):
            found = await self.payments.get_allocation(
                ctx.organization_id, payment_id, allocation_id
            )
            if found is None:
                raise NotFoundError("Allocation not found.")
            invoices = await self.invoices.lock_many(ctx.organization_id, [found.invoice_id])
            payment = await self._get(ctx, payment_id, lock=True)
            allocation = await self.payments.get_allocation(
                ctx.organization_id, payment_id, allocation_id
            )
            if allocation is None or not invoices:
                raise NotFoundError("Allocation not found.")
            invoice = invoices[0]
            invoice.paid_total -= allocation.amount
            payment.allocated_total -= allocation.amount
            amount = allocation.amount
            await self.payments.delete_allocation(allocation)
            await self.session.flush()
            client = await self.refs.get_client(ctx.organization_id, payment.client_id)
            await record(
                self.session,
                ctx,
                rctx,
                "payment.unallocated",
                target_type="payment",
                target_id=payment.id,
                meta={
                    **_payment_meta(payment, client),
                    "invoice_id": str(invoice.id),
                    "invoice_number": invoice.number,
                    "amount_unallocated": amount,
                    "invoice_balance": invoice.balance,
                },
            )
            read = await self._read(ctx, payment)
        return read

    async def void(
        self, ctx: OrgContext, payment_id: uuid.UUID, reason: str | None, rctx: RequestContext
    ) -> PaymentRead:
        async with unit_of_work(self.session):
            payment = await self._get(ctx, payment_id, lock=True)
            _ensure_not_void(payment)
            if payment.allocated_total > 0:
                raise conflict(
                    "PAYMENT_ALLOCATED",
                    "Remove the payment's allocations before voiding it.",
                )
            payment.voided_at = now_utc()
            await self.session.flush()
            client = await self.refs.get_client(ctx.organization_id, payment.client_id)
            await record(
                self.session,
                ctx,
                rctx,
                "payment.voided",
                target_type="payment",
                target_id=payment.id,
                meta={**_payment_meta(payment, client), "reason": reason},
            )
            read = await self._read(ctx, payment)
        return read

    async def reverse(
        self, ctx: OrgContext, payment_id: uuid.UUID, data: PaymentReverse, rctx: RequestContext
    ) -> PaymentRead:
        """Reverse a payment (for example a bounced cheque): every allocation goes, the invoices
        it paid owe again, and the payment is marked reversed (and void) with the reason."""
        async with unit_of_work(self.session):
            payment = await self._get(ctx, payment_id)
            _ensure_not_void(payment)
            known = await self.payments.allocations(ctx.organization_id, payment.id)
            # Lock order: invoices (id order), then the payment.
            invoices = await self.invoices.lock_many(
                ctx.organization_id, [a.invoice_id for a, _ in known]
            )
            payment = await self._get(ctx, payment_id, lock=True)
            _ensure_not_void(payment)
            allocations = await self.payments.allocations(ctx.organization_id, payment.id)
            by_id = {i.id: i for i in invoices}
            if any(a.invoice_id not in by_id for a, _ in allocations):
                # Allocated to another invoice since we looked: its row is not locked, and
                # taking it now would break the lock order. Nothing changed; ask to retry.
                raise conflict(
                    "PAYMENT_CHANGED",
                    "The payment was allocated while it was being reversed: try again.",
                )
            client = await self.refs.get_client(ctx.organization_id, payment.client_id)
            removed: list[dict[str, object]] = []
            for allocation, _ in allocations:
                invoice = by_id[allocation.invoice_id]
                invoice.paid_total -= allocation.amount
                removed.append(
                    {
                        "invoice_id": str(invoice.id),
                        "invoice_number": invoice.number,
                        "amount": allocation.amount,
                        "invoice_balance": invoice.balance,
                    }
                )
                await self.payments.delete_allocation(allocation)
            now = now_utc()
            before = _payment_meta(payment, client)
            payment.allocated_total = 0
            payment.voided_at = now
            payment.reversed_at = now
            payment.reversed_by = ctx.user.id
            payment.reversal_reason = data.reason
            payment.bounced = data.bounced
            await self.session.flush()
            await record(
                self.session,
                ctx,
                rctx,
                "payment.reversed",
                target_type="payment",
                target_id=payment.id,
                meta={
                    **before,
                    "reason": data.reason,
                    "bounced": data.bounced,
                    "allocations_removed": removed,
                },
            )
            await billing_notifications.payment_reversed(
                self.session,
                organization_id=ctx.organization_id,
                payment_id=payment.id,
                reversed_by_id=ctx.user.id,
                reversed_by_name=ctx.user.full_name,
                client_name=client.name if client else "a client",
                amount=invoice_pdf.money(payment.amount, ctx.organization.currency),
                received_on=payment.received_on,
                reason=data.reason,
            )
            read = await self._read(ctx, payment)
        return read

    # --- Internals ---------------------------------------------------------------------

    async def _target_ids(
        self,
        ctx: OrgContext,
        client: Client,
        allocations: Sequence[AllocationIn] | None,
        auto: bool,
    ) -> list[uuid.UUID]:
        if allocations:
            return [a.invoice_id for a in allocations]
        if auto:
            return await self.invoices.open_for_client(ctx.organization_id, client.id)
        return []

    async def _allocate(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        payment: Payment,
        client: Client,
        locked: Sequence[Invoice],
        requested: Sequence[AllocationIn] | None,
    ) -> int:
        """Allocate under the locks already held. Returns the amount allocated."""
        by_id = {i.id: i for i in locked}
        plan: list[tuple[Invoice, int]] = []
        if requested:
            for index, item in enumerate(requested):
                invoice = by_id.get(item.invoice_id)
                if invoice is None:
                    raise NotFoundError("Invoice not found.")
                if invoice.client_id != client.id:
                    raise ValidationError(
                        "The invoice belongs to another client.",
                        code="CLIENT_MISMATCH",
                        details=[
                            {
                                "field": f"allocations.{index}.invoice_id",
                                "code": "client_mismatch",
                                "message": "The invoice belongs to another client.",
                            }
                        ],
                    )
                if invoice.status != "issued":
                    raise conflict(
                        "INVOICE_NOT_ISSUED",
                        "Payments can be allocated to issued invoices only.",
                        details={"invoice_id": str(invoice.id)},
                    )
                if item.amount > invoice.balance:
                    raise conflict(
                        "ALLOCATION_EXCEEDS_BALANCE",
                        "The allocation is more than the invoice's outstanding balance.",
                        details={
                            "invoice_id": str(invoice.id),
                            "amount": item.amount,
                            "balance": invoice.balance,
                        },
                    )
                plan.append((invoice, item.amount))
            total = sum(amount for _, amount in plan)
            if total > payment.unallocated:
                raise conflict(
                    "ALLOCATION_EXCEEDS_PAYMENT",
                    "The allocations are more than the payment's unallocated amount.",
                    details={"amount": total, "unallocated": payment.unallocated},
                )
        else:
            remaining = payment.unallocated
            ordered = sorted(
                (i for i in locked if i.status == "issued" and i.client_id == client.id),
                key=lambda i: (i.issue_date, i.number or "", i.id),
            )
            for invoice in ordered:
                if remaining <= 0:
                    break
                amount = min(invoice.balance, remaining)
                if amount > 0:
                    plan.append((invoice, amount))
                    remaining -= amount
        if not plan:
            return 0

        existing = await self.payments.allocations_by_invoice(
            ctx.organization_id, payment.id, [i.id for i, _ in plan]
        )
        for invoice, amount in plan:
            allocation = existing.get(invoice.id)
            if allocation is not None:
                allocation.amount += amount
            else:
                await self.payments.add_allocation(
                    PaymentAllocation(
                        organization_id=ctx.organization_id,
                        client_id=client.id,
                        payment_id=payment.id,
                        invoice_id=invoice.id,
                        amount=amount,
                    )
                )
            invoice.paid_total += amount
        total = sum(amount for _, amount in plan)
        payment.allocated_total += total
        await self.session.flush()
        await record(
            self.session,
            ctx,
            rctx,
            "payment.allocated",
            target_type="payment",
            target_id=payment.id,
            meta={
                **_payment_meta(payment, client),
                "allocations": [
                    {
                        "invoice_id": str(i.id),
                        "invoice_number": i.number,
                        "amount": amount,
                        "invoice_balance": i.balance,
                    }
                    for i, amount in plan
                ],
            },
        )
        return total
