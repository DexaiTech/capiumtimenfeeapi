"""Notifications, their settings, practice email templates and the log of sent messages.

* ``notifications``: one row per recipient and event, shown in the person's inbox. ``type`` is
  one of the codes of ``app.core.notification_types`` (a format check only, so a new type
  needs no migration). ``dedupe_key`` makes a repeated trigger idempotent per recipient.
* ``notification_settings``: the practice's switch and channel defaults per type.
* ``notification_preferences``: a person's own channel choices per type.
* ``email_templates``: a practice's override of a built-in template (the default lives in code).
* ``message_log``: what the mock messaging gateway "sent". Not tied to a practice when the
  message is an account email.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, OrgScopedMixin, TimestampMixin
from app.db.types import JSON_EMPTY_OBJECT, NOW, UTCDateTime, Uuid36

_TYPE_FORMAT = "REGEXP_LIKE(type, '^[a-z][a-z0-9_]{1,49}$', 'c')"

MESSAGE_CHANNELS = ("email", "sms")
MESSAGE_STATUSES = ("sent", "failed")


def _recipient_fk(name: str) -> ForeignKeyConstraint:
    """The person must belong to the practice; their rows go when the membership does."""
    return ForeignKeyConstraint(
        ["organization_id", "user_id"],
        ["organization_members.organization_id", "organization_members.user_id"],
        name=name,
        ondelete="CASCADE",
    )


class Notification(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_notifications_organization_id_id"),
        _recipient_fk("fk_notifications_organization_id_user_id_organization_members"),
        CheckConstraint(_TYPE_FORMAT, name="type_format"),
        CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="title_length"),
        CheckConstraint("char_length(body) <= 2000", name="body_length"),
        # A path inside the app, never an address that leaves it.
        CheckConstraint(
            "link IS NULL OR (char_length(link) <= 300 AND REGEXP_LIKE(link, '^/($|[^/])', 'c') "
            "AND NOT REGEXP_LIKE(link, '[[:space:][:cntrl:]]', 'c') "
            # No backslash: CHAR(92 USING utf8mb4), so no escaping is involved.
            "AND INSTR(link, CHAR(92 USING utf8mb4)) = 0)",
            name="link_relative",
        ),
        CheckConstraint("(entity_type IS NULL) = (entity_id IS NULL)", name="entity_pair"),
        CheckConstraint(
            "entity_type IS NULL OR char_length(entity_type) BETWEEN 1 AND 50",
            name="entity_type_length",
        ),
        CheckConstraint(
            "dedupe_key IS NULL OR char_length(dedupe_key) BETWEEN 1 AND 200",
            name="dedupe_key_length",
        ),
        Index(
            "ix_notifications_organization_id_user_id_created_at",
            "organization_id",
            "user_id",
            "created_at",
        ),
        Index(
            "ix_notifications_unread",
            "organization_id",
            "user_id",
        ),
        Index(
            "uq_notifications_organization_id_user_id_dedupe_key",
            text("(CASE WHEN dedupe_key IS NOT NULL THEN `organization_id` END)"),
            text("(CASE WHEN dedupe_key IS NOT NULL THEN `user_id` END)"),
            text("(CASE WHEN dedupe_key IS NOT NULL THEN `dedupe_key` END)"),
            unique=True,
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid36(), nullable=False)
    type: Mapped[str] = mapped_column(String(49), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("('')"))
    link: Mapped[str | None] = mapped_column(String(300))
    entity_type: Mapped[str | None] = mapped_column(String(50))
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid36())
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, server_default=JSON_EMPTY_OBJECT
    )
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class NotificationPreference(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """A person's own channel choices for one notification type (no row: the practice default)."""

    __tablename__ = "notification_preferences"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "user_id",
            "type",
            name="uq_notification_preferences_organization_id_user_id_type",
        ),
        _recipient_fk("fk_notification_preferences_user_organization_members"),
        CheckConstraint(_TYPE_FORMAT, name="type_format"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid36(), nullable=False)
    type: Mapped[str] = mapped_column(String(49), nullable=False)
    in_app: Mapped[bool] = mapped_column(Boolean, nullable=False)
    email: Mapped[bool] = mapped_column(Boolean, nullable=False)
    sms: Mapped[bool] = mapped_column(Boolean, nullable=False)


class NotificationSetting(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """The practice's switch and channel defaults for one type (no row: on, in-app and email)."""

    __tablename__ = "notification_settings"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "type", name="uq_notification_settings_organization_id_type"
        ),
        CheckConstraint(_TYPE_FORMAT, name="type_format"),
    )

    type: Mapped[str] = mapped_column(String(49), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    default_in_app: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    default_email: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    default_sms: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid36(), ForeignKey("users.id"))


class EmailTemplate(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """A practice's version of a built-in email template."""

    __tablename__ = "email_templates"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "template_key",
            name="uq_email_templates_organization_id_template_key",
        ),
        CheckConstraint(
            "REGEXP_LIKE(template_key, '^[a-z][a-z0-9_]{1,49}$', 'c')", name="key_format"
        ),
        CheckConstraint(
            "char_length(subject) BETWEEN 1 AND 200 AND NOT REGEXP_LIKE(subject, '[[:cntrl:]]', 'c')",
            name="subject_valid",
        ),
        CheckConstraint("char_length(html_body) BETWEEN 1 AND 100000", name="html_body_length"),
        CheckConstraint("char_length(text_body) BETWEEN 1 AND 50000", name="text_body_length"),
    )

    template_key: Mapped[str] = mapped_column(String(49), nullable=False)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    html_body: Mapped[str] = mapped_column(Text, nullable=False)
    text_body: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid36(), ForeignKey("users.id"))


class MessageLog(Base, IdMixin):
    """Append-only record of every message the mock gateway accepted or failed to send.

    One-time links are removed from the stored bodies. ``user_id`` has no foreign key: the log
    outlives the person.
    """

    __tablename__ = "message_log"
    __table_args__ = (
        CheckConstraint("channel IN ('email', 'sms')", name="channel"),
        CheckConstraint("status IN ('sent', 'failed')", name="status"),
        Index("ix_message_log_organization_id_created_at", "organization_id", "created_at"),
        Index("ix_message_log_created_at", "created_at"),
    )

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=NOW)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid36(), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid36())
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    recipient: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    html_body: Mapped[str | None] = mapped_column(Text)
    template_key: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_ref: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
