"""Transaction-scoped named locks (MySQL), in place of PostgreSQL's advisory locks.

A lock is a row of ``advisory_locks`` locked ``FOR UPDATE``: it is held until the transaction
commits or rolls back, exactly like ``pg_advisory_xact_lock``, and can never leak past the
transaction the way MySQL's session-level ``GET_LOCK`` can. Keys are any text; the row holds
its SHA-256 so every key fits the column.

Callers that take several locks take them in a stable order (sorted ids) so that two
transactions never wait on each other.
"""

import hashlib

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.locks import AdvisoryLock

# MySQL: "Statement aborted because lock(s) could not be acquired immediately and NOWAIT is set."
_LOCK_NOWAIT = 3572


def lock_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def transaction_lock(session: AsyncSession, key: str) -> None:
    """Wait for the lock named ``key`` and hold it until the transaction ends."""
    hashed = lock_key(key)
    # Creates the row the first time, and locks it either way (the duplicate path takes an
    # exclusive lock on the existing row).
    await session.execute(
        insert(AdvisoryLock).values(lock_key=hashed).on_duplicate_key_update(lock_key=hashed)
    )


async def try_transaction_lock(session: AsyncSession, key: str) -> bool:
    """Take the lock named ``key`` if nobody holds it; never waits. True when taken."""
    hashed = lock_key(key)
    try:
        found = await session.scalar(
            select(AdvisoryLock.lock_key)
            .where(AdvisoryLock.lock_key == hashed)
            .with_for_update(nowait=True)
        )
    except OperationalError as exc:
        if exc.orig is not None and exc.orig.args and exc.orig.args[0] == _LOCK_NOWAIT:
            return False
        raise
    if found is None:
        # First use of this key: create it (and so hold it).
        await transaction_lock(session, key)
    return True
