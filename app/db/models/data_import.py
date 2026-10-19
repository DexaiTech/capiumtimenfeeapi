"""Bulk import of staff, rates and jobs: the batch a file becomes and the rows it holds.

Uploading a file validates it at once and keeps the result (``data_import_batches`` and
``data_import_rows``), so the person can read every message, map unmatched clients or staff to
existing records and commit later. A row keeps what the file said (``raw``) and, when it can be
imported, the request body the existing service will receive (``payload``). A batch is committed
at most once; committed batches stay as the record of what was imported.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, OrgScopedMixin, TimestampMixin

ImportEntity = Literal["staff", "rates", "jobs"]
ImportFormat = Literal["csv", "xlsx"]
BatchStatus = Literal["validated", "committed", "failed"]
RowState = Literal["valid", "warning", "rejected"]
RowAction = Literal["create", "update", "unchanged"]


class DataImportBatch(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """One uploaded file and the outcome of checking, and then importing, it."""

    __tablename__ = "data_import_batches"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_data_import_batches_organization_id_id"),
        CheckConstraint("entity IN ('staff', 'rates', 'jobs')", name="entity"),
        CheckConstraint("file_format IN ('csv', 'xlsx')", name="file_format"),
        CheckConstraint("status IN ('validated', 'committed', 'failed')", name="status"),
        CheckConstraint("length(filename) BETWEEN 1 AND 255", name="filename_length"),
        CheckConstraint(
            "total_rows >= 0 AND valid_rows >= 0 AND warning_rows >= 0 AND rejected_rows >= 0 "
            "AND created_rows >= 0 AND updated_rows >= 0 AND unchanged_rows >= 0",
            name="counts",
        ),
        CheckConstraint("status <> 'committed' OR committed_at IS NOT NULL", name="committed_at"),
        Index(
            "ix_data_import_batches_organization_id_created_at",
            "organization_id",
            "created_at",
            "id",
        ),
    )

    entity: Mapped[ImportEntity] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    file_format: Mapped[ImportFormat] = mapped_column(Text, nullable=False)
    status: Mapped[BatchStatus] = mapped_column(Text, nullable=False)
    # What the person chose when uploading (rates: ``close_previous``).
    options: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Unmatched client codes and staff references the person pointed at existing records:
    # ``{"clients": {"acme1": "<client id>"}, "staff": {"jo@old.example": "<user id>"}}``.
    mapping: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # The file's header cells: ``{"recognised": [...], "ignored": [...]}``.
    columns: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    valid_rows: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    warning_rows: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    rejected_rows: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # What a commit did to the importable rows.
    created_rows: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    updated_rows: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    unchanged_rows: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # Why a commit failed (the batch is then ``failed`` and nothing was written).
    error_message: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    committed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DataImportRow(Base, IdMixin, OrgScopedMixin):
    """One data row of an uploaded file (row 1 is the first row below the header)."""

    __tablename__ = "data_import_rows"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "batch_id"],
            ["data_import_batches.organization_id", "data_import_batches.id"],
            name="fk_data_import_rows_organization_id_data_import_batches",
            ondelete="CASCADE",
        ),
        UniqueConstraint("batch_id", "row_number", name="uq_data_import_rows_batch_id_row_number"),
        CheckConstraint("row_number >= 1", name="row_number"),
        CheckConstraint("state IN ('valid', 'warning', 'rejected')", name="state"),
        CheckConstraint(
            "action IS NULL OR action IN ('create', 'update', 'unchanged')", name="action"
        ),
        CheckConstraint("state = 'rejected' OR action IS NOT NULL", name="importable_has_action"),
    )

    batch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # The cells as the file had them, by column key.
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state: Mapped[RowState] = mapped_column(Text, nullable=False)
    # What importing the row does; null for a rejected row.
    action: Mapped[RowAction | None] = mapped_column(Text)
    # The request body the staff, rate-rule or job service receives; null for a rejected row.
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # The record an update changes, or (after a commit) the record the row created or updated.
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # ``[{severity, field, code, message, value}]``; empty for a clean row.
    messages: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )


__all__ = [
    "BatchStatus",
    "DataImportBatch",
    "DataImportRow",
    "ImportEntity",
    "ImportFormat",
    "RowAction",
    "RowState",
]
