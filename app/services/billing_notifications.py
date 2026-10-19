"""Notifications of the billing workflow: invoice approval and payment reversal.

Built on the helpers of ``notification_events``: each one raises its notification inside a
savepoint, so a problem there can never fail the business action that triggered it.
"""

import uuid
from collections.abc import Iterable
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.notification_types import NotificationType
from app.repositories.notifications import NotificationSettingsRepository
from app.services.notification_events import link_invoice, notify_safely, uk_date
from app.services.notifications import NotifyResult, clip, dedupe_key

LINK_PAYMENTS = "/billing/payments"


async def invoice_approvers(
    session: AsyncSession, organization_id: uuid.UUID, exclude: Iterable[uuid.UUID]
) -> list[uuid.UUID]:
    """Active members who can approve invoices, but not the people who prepared or submitted it."""
    skipped = set(exclude)
    holders = await NotificationSettingsRepository(session).members_with_any_permission(
        organization_id, ("invoice.approve",)
    )
    return [user_id for user_id in holders if user_id not in skipped]


async def invoice_approval_pending(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    invoice_id: uuid.UUID,
    client_name: str,
    invoice_total: str,
    submitter_id: uuid.UUID,
    submitter_name: str,
    creator_id: uuid.UUID | None,
    submitted_at: datetime,
) -> NotifyResult | None:
    """Tell the approvers an invoice waits for them. Once per submission."""
    skipped = [submitter_id] + ([creator_id] if creator_id else [])
    return await notify_safely(
        session,
        organization_id=organization_id,
        type=NotificationType.INVOICE_APPROVAL_PENDING,
        recipients_from=lambda: invoice_approvers(session, organization_id, skipped),
        title="Invoice waiting for approval",
        body=(
            f"{submitter_name} submitted an invoice for {client_name} ({invoice_total}) "
            "for approval."
        ),
        link=link_invoice(invoice_id),
        entity_type="invoice",
        entity_id=invoice_id,
        payload={"invoice_total": invoice_total},
        dedupe_key=dedupe_key(
            NotificationType.INVOICE_APPROVAL_PENDING, invoice_id, submitted_at.isoformat()
        ),
        merge={
            "client_name": client_name,
            "invoice_total": invoice_total,
            "invoice_submitter": submitter_name,
        },
    )


async def invoice_approved(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    invoice_id: uuid.UUID,
    recipients: Iterable[uuid.UUID],
    client_name: str,
    invoice_total: str,
    decider_name: str,
    decided_at: datetime,
) -> NotifyResult | None:
    """Tell the people who prepared the invoice it was approved. Once per approval."""
    return await notify_safely(
        session,
        organization_id=organization_id,
        type=NotificationType.INVOICE_APPROVED,
        recipients=list(recipients),
        title="Invoice approved",
        body=(
            f"{decider_name} approved your invoice for {client_name} ({invoice_total}). "
            "It can now be issued."
        ),
        link=link_invoice(invoice_id),
        entity_type="invoice",
        entity_id=invoice_id,
        payload={"invoice_total": invoice_total},
        dedupe_key=dedupe_key(
            NotificationType.INVOICE_APPROVED, invoice_id, decided_at.isoformat()
        ),
        merge={
            "client_name": client_name,
            "invoice_total": invoice_total,
            "invoice_decider": decider_name,
        },
    )


async def invoice_rejected(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    invoice_id: uuid.UUID,
    recipients: Iterable[uuid.UUID],
    client_name: str,
    invoice_total: str,
    decider_name: str,
    reason: str,
    decided_at: datetime,
) -> NotifyResult | None:
    """Tell the people who prepared the invoice it was rejected, with the reason. Once per
    rejection."""
    return await notify_safely(
        session,
        organization_id=organization_id,
        type=NotificationType.INVOICE_REJECTED,
        recipients=list(recipients),
        title="Invoice rejected",
        body=(
            f"{decider_name} rejected your invoice for {client_name} ({invoice_total}): "
            f"{clip(reason, 300)}"
        ),
        link=link_invoice(invoice_id),
        entity_type="invoice",
        entity_id=invoice_id,
        payload={"invoice_total": invoice_total},
        dedupe_key=dedupe_key(
            NotificationType.INVOICE_REJECTED, invoice_id, decided_at.isoformat()
        ),
        merge={
            "client_name": client_name,
            "invoice_total": invoice_total,
            "invoice_decider": decider_name,
            "invoice_reason": reason,
        },
    )


async def payment_reversed(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    payment_id: uuid.UUID,
    reversed_by_id: uuid.UUID,
    reversed_by_name: str,
    client_name: str,
    amount: str,
    received_on: date,
    reason: str,
) -> NotifyResult | None:
    """Tell the practice's payment staff (not the person who did it) a payment was reversed.
    A payment is reversed once, so the notification is too."""

    async def recipients() -> list[uuid.UUID]:
        holders = await NotificationSettingsRepository(session).members_with_any_permission(
            organization_id, ("payment.record",)
        )
        return [user_id for user_id in holders if user_id != reversed_by_id]

    return await notify_safely(
        session,
        organization_id=organization_id,
        type=NotificationType.PAYMENT_REVERSED,
        recipients_from=recipients,
        title=f"Payment from {client_name} reversed",
        body=(
            f"{reversed_by_name} reversed the payment of {amount} received on "
            f"{uk_date(received_on)}: {clip(reason, 300)}"
        ),
        link=LINK_PAYMENTS,
        entity_type="payment",
        entity_id=payment_id,
        payload={"amount": amount},
        dedupe_key=dedupe_key(NotificationType.PAYMENT_REVERSED, payment_id),
        merge={
            "client_name": client_name,
            "payment_amount": amount,
            "payment_date": uk_date(received_on),
            "payment_reversed_by": reversed_by_name,
            "payment_reason": reason,
        },
    )
