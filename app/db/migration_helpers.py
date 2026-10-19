"""Shared building blocks for migrations."""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.permissions import CATALOGUE
from app.utils.ids import new_id


def id_column() -> sa.Column[Any]:
    return sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False)


def timestamps() -> list[sa.Column[Any]]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def add_updated_at_trigger(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_set_updated_at BEFORE UPDATE ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def add_updated_at_triggers(*tables: str) -> None:
    for table in tables:
        add_updated_at_trigger(table)


def sync_permissions() -> None:
    """Upsert the permission catalogue of ``app.core.permissions``.

    Idempotent, so a later migration that adds permissions calls it again. Codes that left
    the catalogue are not deleted here: a migration that retires one does so explicitly.
    """
    table = sa.table(
        "permissions",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("code", sa.Text()),
        sa.column("category", sa.Text()),
        sa.column("description", sa.Text()),
    )
    rows = [
        {"id": new_id(), "code": p.code, "category": p.category, "description": p.description}
        for p in CATALOGUE
    ]
    stmt = postgresql.insert(table).values(rows)
    op.execute(
        stmt.on_conflict_do_update(
            index_elements=["code"],
            set_={"category": stmt.excluded.category, "description": stmt.excluded.description},
        )
    )
