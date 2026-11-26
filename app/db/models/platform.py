import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Computed,
    Index,
    Integer,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin
from app.db.types import JSON_EMPTY_OBJECT, NOW, UTCDateTime, Uuid36
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
        ),
        Index(
            "ix_background_jobs_running_locked_until",
            "locked_until",
        ),
        Index(
            "uq_background_jobs_dedupe_key",
            text(
                "(CASE WHEN dedupe_key IS NOT NULL AND status IN ('queued', 'running')"
                " THEN `dedupe_key` END)"
            ),
            unique=True,
        ),
    )

    organization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid36())
    type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, server_default=JSON_EMPTY_OBJECT
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="queued")
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("100"))
    run_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=NOW)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    locked_by: Mapped[str | None] = mapped_column(Text)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str | None] = mapped_column(String(255))
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class RateLimitCounter(Base):
    __tablename__ = "rate_limit_counters"
    __table_args__ = (PrimaryKeyConstraint("key", "window_start"),)

    key: Mapped[str] = mapped_column(String(255), nullable=False)
    window_start: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class AuditLog(Base):
    """Append-only (a trigger refuses updates and deletes, ``app/db/triggers.py``)."""

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
        ),
        Index(
            "ix_audit_logs_organization_id_client_ref_created_at",
            "organization_id",
            "client_ref",
            "created_at",
        ),
        # The entries that name one ticket in ``ticket_id`` (its comments and files).
        Index(
            "ix_audit_logs_organization_id_ticket_ref_created_at",
            "organization_id",
            "ticket_ref",
            "created_at",
        ),
        # The entries about one record of any type (record history, entity filters).
        Index(
            "ix_audit_logs_organization_id_target_created_at",
            "organization_id",
            "target_type",
            "target_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid36(), default=new_id)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=NOW)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid36())
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid36())
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(255))
    target_id: Mapped[uuid.UUID | None] = mapped_column(Uuid36())
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    # "metadata" is reserved on declarative classes, hence the attribute name.
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, server_default=JSON_EMPTY_OBJECT
    )
    # The client and the ticket the entry names in its metadata, indexed for the Client 360 and
    # ticket histories (MySQL indexes a JSON value only through a generated column).
    client_ref: Mapped[str | None] = mapped_column(
        String(64), Computed("json_unquote(json_extract(`metadata`, '$.client_id'))")
    )
    ticket_ref: Mapped[str | None] = mapped_column(
        String(64), Computed("json_unquote(json_extract(`metadata`, '$.ticket_id'))")
    )
