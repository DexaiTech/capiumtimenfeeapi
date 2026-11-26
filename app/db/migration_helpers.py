"""Shared building blocks for migrations (MySQL 8.4)."""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

from app.core.permissions import CATALOGUE
from app.utils.ids import new_id


def id_column() -> sa.Column[Any]:
    return sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False)


def timestamps() -> list[sa.Column[Any]]:
    """``created_at`` and ``updated_at`` as ``app.db.base.TimestampMixin`` declares them:
    UTC ``DATETIME(6)``, and ``updated_at`` kept current by MySQL on every change."""
    return [
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
    ]


def sync_permissions() -> None:
    """Upsert the permission catalogue of ``app.core.permissions``.

    Idempotent, so a later migration that adds permissions calls it again. Codes that left
    the catalogue are not deleted here: a migration that retires one does so explicitly.
    """
    table = sa.table(
        "permissions",
        sa.column("id", sa.String()),
        sa.column("code", sa.String()),
        sa.column("category", sa.String()),
        sa.column("description", sa.String()),
    )
    rows = [
        {"id": str(new_id()), "code": p.code, "category": p.category, "description": p.description}
        for p in CATALOGUE
    ]
    stmt = mysql.insert(table).values(rows)
    # ``permissions.code`` is the only unique key besides the id, so a duplicate is the code.
    op.execute(
        stmt.on_duplicate_key_update(
            category=stmt.inserted.category, description=stmt.inserted.description
        )
    )
