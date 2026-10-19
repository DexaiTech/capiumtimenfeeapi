import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin
from app.utils.ids import new_id

JOB_STATUSES = ("queued", "running", "succeeded", "failed", "dead")


class BackgroundJob(Base, IdMixin, TimestampMixin):
    __tablename__ = "background_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'dead')", name="status"
        ),
        Index(
            "ix_background_jobs_queued",
            "priority",
            "run_at",
            postgresql_where=text("status = 'queued'"),
        ),
        Index(
            "ix_background_jobs_running_locked_until",
            "locked_until",
            postgresql_where=text("status = 'running'"),
        ),
        Index(
            "uq_background_jobs_dedupe_key",
            "dedupe_key",
            unique=True,
            postgresql_where=text("dedupe_key IS NOT NULL AND status IN ('queued', 'running')"),
        ),
    )

    organization_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("100"))
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    locked_by: Mapped[str | None] = mapped_column(Text)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RateLimitCounter(Base):
    __tablename__ = "rate_limit_counters"
    __table_args__ = (
        PrimaryKeyConstraint("key", "window_start"),
        {"prefixes": ["UNLOGGED"]},
    )

    key: Mapped[str] = mapped_column(Text, nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class AuditLog(Base):
    """Append-only. Range-partitioned by month on ``created_at``."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        PrimaryKeyConstraint("id", "created_at"),
        Index("ix_audit_logs_organization_id_created_at", "organization_id", "created_at"),
        Index(
            "ix_audit_logs_organization_id_actor_user_id_created_at",
            "organization_id",
            "actor_user_id",
            "created_at",
        ),
        # The entries about one client: the client itself, and what names it in ``client_id``.
        Index(
            "ix_audit_logs_organization_id_client_target_created_at",
            "organization_id",
            "target_id",
            "created_at",
            postgresql_where=text("target_type = 'client'"),
        ),
        Index(
            "ix_audit_logs_organization_id_client_ref_created_at",
            "organization_id",
            text("(metadata ->> 'client_id')"),
            "created_at",
            postgresql_where=text("(metadata ->> 'client_id') IS NOT NULL"),
        ),
        # The entries about one record of any type (record history, entity filters).
        Index(
            "ix_audit_logs_organization_id_target_created_at",
            "organization_id",
            "target_type",
            "target_id",
            "created_at",
        ),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=new_id)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str | None] = mapped_column(Text)
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    # "metadata" is reserved on declarative classes, hence the attribute name.
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
