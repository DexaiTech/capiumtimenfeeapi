"""Gapless document numbers (clients, jobs, invoices, credit notes, staff, tickets, estimates).

``next_number`` locks the practice's sequence row ``FOR UPDATE`` and increments it inside
the caller's transaction. Two concurrent issuers queue on the lock; a rolled-back
transaction releases its number, so issued invoice numbers have no gaps (HMRC expects a
unique, sequential invoice number).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DocumentSequence

DEFAULT_SEQUENCES: dict[str, tuple[str, int]] = {
    # kind: (prefix, padding)
    "client": ("C", 4),
    "job": ("J", 5),
    "invoice": ("INV-", 5),
    "credit_note": ("CN-", 5),
    "staff": ("S", 3),
    "ticket": ("APT", 1),
    "estimate": ("EST", 5),
}


def format_number(prefix: str, number: int, padding: int, suffix: str = "") -> str:
    return f"{prefix}{number:0{padding}d}{suffix}"


async def take_number(
    session: AsyncSession, organization_id: uuid.UUID, kind: str
) -> tuple[int, str]:
    """Take the next number of ``kind``: the integer and its formatted form (``APT271``).

    Must run inside the transaction that uses it.
    """
    stmt = (
        select(DocumentSequence)
        .where(DocumentSequence.organization_id == organization_id, DocumentSequence.kind == kind)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    sequence = (await session.execute(stmt)).scalar_one_or_none()
    if sequence is None:
        prefix, padding = DEFAULT_SEQUENCES[kind]
        sequence = DocumentSequence(
            organization_id=organization_id,
            kind=kind,
            prefix=prefix,
            next_number=1,
            padding=padding,
        )
        session.add(sequence)
        await session.flush()
        # Re-read under the lock: a concurrent creator may have won the insert.
        sequence = (await session.execute(stmt)).scalar_one()
    number = sequence.next_number
    formatted = format_number(sequence.prefix, number, sequence.padding, sequence.suffix)
    sequence.next_number += 1
    await session.flush()
    return number, formatted


async def next_number(session: AsyncSession, organization_id: uuid.UUID, kind: str) -> str:
    """Take the next number of ``kind`` as text. Must run inside the transaction that uses it."""
    return (await take_number(session, organization_id, kind))[1]


async def list_sequences(
    session: AsyncSession, organization_id: uuid.UUID
) -> list[DocumentSequence]:
    stmt = (
        select(DocumentSequence)
        .where(DocumentSequence.organization_id == organization_id)
        .order_by(DocumentSequence.kind)
    )
    return list((await session.execute(stmt)).scalars())
