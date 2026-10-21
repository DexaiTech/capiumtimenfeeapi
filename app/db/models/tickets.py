"""Application Testing: the practice's own bug and enhancement tracker.

* ``ticket_tags``: the practice's labels (a name and one of a few colours).
* ``tickets``: one row per ticket. ``number`` comes from the gapless ``ticket`` sequence and
  ``code`` (``APT271``) is that number with the practice's prefix, stored so that a later change
  of the prefix does not rename old tickets. ``closed_at`` is set exactly while the status is
  ``closed`` (a CHECK keeps the two in step).
* ``ticket_tag_links``: the many-to-many between tickets and tags.
* ``ticket_comments`` and ``ticket_attachments``: the conversation and the files. An attachment
  belongs to a ticket and optionally to one of its comments; the file itself lives in the file
  storage under ``storage_key`` (random, never derived from the name).
* ``ticket_status_history``: one row when the ticket is created and one for every status change,
  written in the same transaction. It is the audit of the workflow and what the dashboard's
  status trend is reconstructed from.

Every reference to a person is a composite key to ``organization_members``, and every reference
between these tables carries ``organization_id``, so a row can never point into another practice.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, OrgScopedMixin, TimestampMixin

TICKET_STATUSES = ("open", "hold", "in_progress", "reopen", "closed")
TICKET_PRIORITIES = ("high", "medium", "low", "not_set")
TICKET_CATEGORIES = ("application",)
TAG_COLORS = ("violet", "blue", "green", "red", "orange", "grey")
MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024
# How much of a description the ticket list carries.
EXCERPT_LENGTH = 200


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _member_fk(column: str, name: str) -> ForeignKeyConstraint:
    """The person must belong to the practice."""
    return ForeignKeyConstraint(
        ["organization_id", column],
        ["organization_members.organization_id", "organization_members.user_id"],
        name=name,
    )


class TicketTag(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    __tablename__ = "ticket_tags"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_ticket_tags_organization_id_id"),
        CheckConstraint("length(name) BETWEEN 1 AND 40", name="name_length"),
        CheckConstraint(_in("color", TAG_COLORS), name="color"),
        Index(
            "uq_ticket_tags_organization_id_name_lower",
            "organization_id",
            text("lower(name)"),
            unique=True,
        ),
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    color: Mapped[str] = mapped_column(Text, nullable=False, server_default="grey")


class Ticket(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    __tablename__ = "tickets"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_tickets_organization_id_id"),
        _member_fk("assignee_user_id", "fk_tickets_organization_id_assignee_members"),
        _member_fk("created_by", "fk_tickets_organization_id_created_by_members"),
        CheckConstraint("number >= 1", name="number"),
        CheckConstraint("length(code) BETWEEN 1 AND 30", name="code_length"),
        CheckConstraint(_in("category", TICKET_CATEGORIES), name="category"),
        CheckConstraint("length(subject) BETWEEN 1 AND 200", name="subject_length"),
        CheckConstraint(
            "description IS NULL OR length(description) <= 10000", name="description_length"
        ),
        CheckConstraint(
            "description_html IS NULL OR length(description_html) <= 40000",
            name="description_html_length",
        ),
        CheckConstraint(_in("status", TICKET_STATUSES), name="status"),
        CheckConstraint(_in("priority", TICKET_PRIORITIES), name="priority"),
        CheckConstraint("(status = 'closed') = (closed_at IS NOT NULL)", name="closed_at"),
        Index("uq_tickets_organization_id_number", "organization_id", "number", unique=True),
        Index("uq_tickets_organization_id_code", "organization_id", "code", unique=True),
        Index("ix_tickets_organization_id_created_at", "organization_id", "created_at", "id"),
        Index("ix_tickets_organization_id_status", "organization_id", "status", "created_at"),
        Index("ix_tickets_organization_id_priority", "organization_id", "priority", "created_at"),
        Index(
            "ix_tickets_organization_id_assignee_user_id",
            "organization_id",
            "assignee_user_id",
            "created_at",
            postgresql_where=text("assignee_user_id IS NOT NULL"),
        ),
        Index(
            "ix_tickets_organization_id_subject_lower",
            "organization_id",
            text("lower(subject)"),
            "id",
        ),
    )

    number: Mapped[int] = mapped_column(Integer, nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False, server_default="application")
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # The editor's sanitised HTML; ``description`` holds its text. Null for plain-text tickets.
    description_html: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="open")
    priority: Mapped[str] = mapped_column(Text, nullable=False, server_default="not_set")
    assignee_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TicketTagLink(Base, OrgScopedMixin):
    __tablename__ = "ticket_tag_links"
    __table_args__ = (
        PrimaryKeyConstraint("ticket_id", "tag_id"),
        ForeignKeyConstraint(
            ["organization_id", "ticket_id"],
            ["tickets.organization_id", "tickets.id"],
            name="fk_ticket_tag_links_ticket_id_tickets",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "tag_id"],
            ["ticket_tags.organization_id", "ticket_tags.id"],
            name="fk_ticket_tag_links_tag_id_ticket_tags",
            ondelete="CASCADE",
        ),
        Index("ix_ticket_tag_links_organization_id_tag_id", "organization_id", "tag_id"),
    )

    ticket_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tag_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TicketComment(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    __tablename__ = "ticket_comments"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_ticket_comments_organization_id_id"),
        # The target of an attachment's (ticket, comment) pair: the two must belong together.
        UniqueConstraint(
            "organization_id",
            "ticket_id",
            "id",
            name="uq_ticket_comments_organization_id_ticket_id_id",
        ),
        ForeignKeyConstraint(
            ["organization_id", "ticket_id"],
            ["tickets.organization_id", "tickets.id"],
            name="fk_ticket_comments_ticket_id_tickets",
            ondelete="CASCADE",
        ),
        _member_fk("author_user_id", "fk_ticket_comments_organization_id_author_members"),
        CheckConstraint("length(body) <= 5000", name="body_length"),
        CheckConstraint("body_html IS NULL OR length(body_html) <= 20000", name="body_html_length"),
        Index(
            "ix_ticket_comments_organization_id_ticket_id_created_at",
            "organization_id",
            "ticket_id",
            "created_at",
        ),
    )

    ticket_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    author_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # May be empty only when the comment carries attachments (a rule of the service).
    body: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # The editor's sanitised HTML; ``body`` holds its text. Null for plain-text comments.
    body_html: Mapped[str | None] = mapped_column(Text)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TicketAttachment(Base, IdMixin, OrgScopedMixin):
    __tablename__ = "ticket_attachments"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_ticket_attachments_organization_id_id"),
        ForeignKeyConstraint(
            ["organization_id", "ticket_id"],
            ["tickets.organization_id", "tickets.id"],
            name="fk_ticket_attachments_ticket_id_tickets",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "ticket_id", "comment_id"],
            [
                "ticket_comments.organization_id",
                "ticket_comments.ticket_id",
                "ticket_comments.id",
            ],
            name="fk_ticket_attachments_comment_id_ticket_comments",
            ondelete="CASCADE",
        ),
        _member_fk("uploaded_by", "fk_ticket_attachments_organization_id_uploader_members"),
        CheckConstraint("length(file_name) BETWEEN 1 AND 200", name="file_name_length"),
        CheckConstraint("length(content_type) BETWEEN 3 AND 100", name="content_type_length"),
        CheckConstraint(f"size_bytes BETWEEN 1 AND {MAX_ATTACHMENT_BYTES}", name="size_bytes"),
        CheckConstraint("length(storage_key) BETWEEN 16 AND 128", name="storage_key_length"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_format"),
        Index("uq_ticket_attachments_storage_key", "storage_key", unique=True),
        Index("ix_ticket_attachments_organization_id_ticket_id", "organization_id", "ticket_id"),
        Index(
            "ix_ticket_attachments_organization_id_comment_id",
            "organization_id",
            "comment_id",
            postgresql_where=text("comment_id IS NOT NULL"),
        ),
    )

    ticket_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    comment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # The sanitised name the person gave the file.
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    # What the content really is (sniffed), never what the client claimed.
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TicketStatusHistory(Base, IdMixin, OrgScopedMixin):
    __tablename__ = "ticket_status_history"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "ticket_id"],
            ["tickets.organization_id", "tickets.id"],
            name="fk_ticket_status_history_ticket_id_tickets",
            ondelete="CASCADE",
        ),
        _member_fk("changed_by", "fk_ticket_status_history_organization_id_changed_by_members"),
        CheckConstraint(
            f"from_status IS NULL OR {_in('from_status', TICKET_STATUSES)}", name="from_status"
        ),
        CheckConstraint(_in("to_status", TICKET_STATUSES), name="to_status"),
        CheckConstraint("from_status IS NULL OR from_status <> to_status", name="status_changed"),
        Index(
            "ix_ticket_status_history_organization_id_ticket_id_at",
            "organization_id",
            "ticket_id",
            "changed_at",
        ),
    )

    ticket_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Null for the row written when the ticket is created.
    from_status: Mapped[str | None] = mapped_column(Text)
    to_status: Mapped[str] = mapped_column(Text, nullable=False)
    changed_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "EXCERPT_LENGTH",
    "MAX_ATTACHMENT_BYTES",
    "TAG_COLORS",
    "TICKET_CATEGORIES",
    "TICKET_PRIORITIES",
    "TICKET_STATUSES",
    "Ticket",
    "TicketAttachment",
    "TicketComment",
    "TicketStatusHistory",
    "TicketTag",
    "TicketTagLink",
]
