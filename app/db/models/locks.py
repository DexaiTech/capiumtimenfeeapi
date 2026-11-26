"""Rows that stand for named locks (``app/db/locks.py``). Not business data: never audited,
never scoped to a practice, never deleted."""

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AdvisoryLock(Base):
    __tablename__ = "advisory_locks"

    # SHA-256 of the lock's name, in hex.
    lock_key: Mapped[str] = mapped_column(String(64), primary_key=True)
