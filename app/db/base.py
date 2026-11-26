"""Declarative base, constraint naming convention and shared column mixins."""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db.types import NOW, NOW_ON_UPDATE, UTCDateTime, Uuid36
from app.utils.ids import new_id

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # A column annotated ``uuid.UUID`` without an explicit type is a ``Uuid36`` too.
    type_annotation_map = {uuid.UUID: Uuid36()}  # noqa: RUF012 (SQLAlchemy reads the class attribute)


class IdMixin:
    id: Mapped[uuid.UUID] = mapped_column(Uuid36(), primary_key=True, default=new_id)


class TimestampMixin:
    """``updated_at`` is kept current by MySQL (``ON UPDATE CURRENT_TIMESTAMP(6)``)."""

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=NOW, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=NOW_ON_UPDATE, nullable=False
    )


class SoftDeleteMixin:
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class OrgScopedMixin:
    """The practice a business row belongs to. Deleting a practice deletes its data."""

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid36(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
