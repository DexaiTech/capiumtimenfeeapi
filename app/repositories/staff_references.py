"""Everything that refers to a staff member (a user id): the counts that decide whether a
record can be deleted, and the re-pointing used when two records are merged.

The references are found from the table metadata, so a table added later that points at a
user or a member is covered without touching this module. Ids stored without a foreign key are
listed in ``_UNKEYED``.
"""

import uuid
from dataclasses import dataclass
from functools import cache
from typing import Any

from sqlalchemy import (
    Column,
    ColumnElement,
    Table,
    and_,
    func,
    literal,
    or_,
    select,
    union_all,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base

# Sign-in state of the person: not history, and removed with the account.
_NOT_HISTORY = frozenset(
    {
        "user_sessions",
        "refresh_tokens",
        "email_verification_tokens",
        "password_reset_tokens",
        # The person's inbox and their own settings go with their membership.
        "notifications",
        "notification_preferences",
    }
)
# Ids kept without a foreign key (the history outlives what it names).
_UNKEYED = (("rate_changes", "user_id"), ("rate_rerun_items", "user_id"))
_MEMBERS = "organization_members"


class ReferenceConflict(Exception):  # noqa: N818  (not an error type: the caller converts it)
    """Moving the references would break a uniqueness rule of ``table``."""

    def __init__(self, table: str) -> None:
        super().__init__(table)
        self.table = table


@dataclass(frozen=True, slots=True)
class Reference:
    table: Table
    columns: tuple[Column[Any], ...]

    @property
    def name(self) -> str:
        return self.table.name

    @property
    def scoped(self) -> bool:
        return "organization_id" in self.table.c

    def matches(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> ColumnElement[bool]:
        held = or_(*[column == user_id for column in self.columns])
        if self.scoped:
            return and_(self.table.c.organization_id == organization_id, held)
        return held


@cache
def references() -> tuple[Reference, ...]:
    """Every column of every table that holds a user or member id."""
    found: dict[str, list[Column[Any]]] = {}
    for table in Base.metadata.sorted_tables:
        if table.name in _NOT_HISTORY:
            continue
        for constraint in table.foreign_key_constraints:
            referred = constraint.referred_table.name
            if referred == "users":
                if constraint.ondelete == "CASCADE":
                    continue
                column = constraint.elements[0].parent
            elif referred == _MEMBERS:
                column = next(e.parent for e in constraint.elements if e.column.name == "user_id")
            else:
                continue
            # A member row's own ``user_id`` is the member, not a reference to it.
            if table.name == _MEMBERS and column.name == "user_id":
                continue
            found.setdefault(table.name, []).append(column)
    for name, column_name in _UNKEYED:
        table = Base.metadata.tables[name]
        found.setdefault(name, []).append(table.c[column_name])
    return tuple(
        Reference(Base.metadata.tables[name], tuple(columns)) for name, columns in found.items()
    )


async def usage(
    session: AsyncSession, organization_id: uuid.UUID, user_id: uuid.UUID
) -> dict[str, int]:
    """Rows that refer to the person, by table, in one query. An approver link from another
    member's record is not history and is left out."""
    selects = [
        select(literal(ref.name).label("name"), func.count().label("count"))
        .select_from(ref.table)
        .where(ref.matches(organization_id, user_id))
        for ref in references()
        if ref.name != _MEMBERS
    ]
    rows = (await session.execute(union_all(*selects))).all()
    return {name: int(count) for name, count in rows if count}


async def reassign(
    session: AsyncSession, organization_id: uuid.UUID, old_id: uuid.UUID, new_id: uuid.UUID
) -> None:
    """Point every reference to ``old_id`` at ``new_id``.

    Run it inside a savepoint: ``ReferenceConflict`` leaves the transaction unusable until the
    savepoint is rolled back.
    """
    for ref in references():
        for column in ref.columns:
            stmt = update(ref.table).where(column == old_id).values({column: new_id})
            if ref.scoped:
                stmt = stmt.where(ref.table.c.organization_id == organization_id)
            try:
                await session.execute(stmt)
            except IntegrityError as exc:
                raise ReferenceConflict(ref.name) from exc
