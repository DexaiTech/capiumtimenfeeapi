"""Moving everything that belongs to one client onto another (a central merge).

``REPOINTED`` tables move in one statement. The composite foreign keys ``(organization_id,
client_id, job_id)`` (jobs, and the time, disbursements and estimates that name one),
``(organization_id, client_id, invoice_id)`` and ``(organization_id, client_id, payment_id)``
mean a job, an invoice or a payment and the rows that name it carry the
client id twice over: changed one table at a time, each statement would leave a row pointing at
a key that no longer exists. PostgreSQL checks these constraints at the end of a statement, so
a single statement with one data-modifying ``WITH`` clause per table moves them all together.

``tests/test_client_merge.py`` discovers every table that refers to a client from the model
metadata and fails when it is not listed here, so a later module cannot be forgotten.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import Table, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.base import Base
from app.db.models import (
    Client,
    ClientContact,
    ClientService,
    RateChange,
    RateRule,
    Service,
)

# Moved together in one statement (see the module docstring).
REPOINTED: tuple[str, ...] = (
    "jobs",
    "time_entries",
    "timers",
    "disbursements",
    "expenses",
    "wip_write_offs",
    "invoices",
    "credit_notes",
    "payments",
    "payment_allocations",
    "statement_sends",
    "wip_adjustments",
    "wip_transfers",
    "credit_note_wip",
    "estimates",
    "recurring_schedules",
)
# Moved with a rule for the rows that cannot simply move.
CONFLICT_RULES: tuple[str, ...] = ("client_contacts", "client_services", "rate_rules")
# Append-only history that names a client without a foreign key: it keeps the client it was
# written for. (The moved rate rules get a new history row.) ``wip_movements`` cannot be moved at
# all: the database enforces it append-only (no UPDATE), which is also why it has no foreign key.
# The live WIP view resolves a movement's client through any merge chain instead (see
# ``adjustment_atoms`` in ``app/repositories/wip_atoms.py``). ``wip_snapshot_items`` records the
# client and job as they were on a past date and must not change after the fact.
HISTORY: tuple[str, ...] = (
    "rate_changes",
    "rate_rerun_items",
    "wip_movements",
    "wip_snapshot_items",
)
MOVED_TABLES: tuple[str, ...] = (*REPOINTED, *CONFLICT_RULES)
HANDLED_TABLES: frozenset[str] = frozenset((*MOVED_TABLES, *HISTORY))


@dataclass(slots=True)
class MergeCounts:
    """What a merge moved, by table (every table in ``MOVED_TABLES`` is present), and what it
    could not."""

    moved: dict[str, int] = field(default_factory=dict)
    # Rows that stay on the source because the target already has the same thing.
    left_behind: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    contacts_demoted: int = 0
    merged_clients_repointed: int = 0
    rate_history_rows: int = 0


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


class ClientMergeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def lock_pair(
        self, organization_id: uuid.UUID, client_ids: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """Lock both client rows in id order, so a concurrent merge in either direction waits
        and work being added to either client waits for the merge."""
        await self.session.execute(
            select(Client.id)
            .where(Client.organization_id == organization_id, Client.id.in_(client_ids))
            .order_by(Client.id)
            .with_for_update()
        )

    async def repoint(
        self, organization_id: uuid.UUID, source_id: uuid.UUID, target_id: uuid.UUID
    ) -> dict[str, int]:
        """Move every ``REPOINTED`` row of ``source_id`` to ``target_id``: rows per table."""
        ctes = []
        for name in REPOINTED:
            table: Table = Base.metadata.tables[name]
            moved = (
                update(table)
                .where(table.c.organization_id == organization_id, table.c.client_id == source_id)
                .values(client_id=target_id)
                .returning(table.c.id)
                .cte(f"moved_{name}")
            )
            ctes.append((name, moved))
        stmt = select(
            *(
                select(func.count()).select_from(cte).scalar_subquery().label(name)
                for name, cte in ctes
            )
        )
        row = (await self.session.execute(stmt)).one()
        return {name: int(row._mapping[name]) for name in REPOINTED}

    async def move_contacts(
        self, organization_id: uuid.UUID, source_id: uuid.UUID, target_id: uuid.UUID
    ) -> tuple[int, int]:
        """Move the contacts. A client has one primary contact: when the target already has
        one, the source's primary contact moves as an ordinary contact. Returns the contacts
        moved and how many of them were demoted."""
        target_has_primary = bool(
            (
                await self.session.execute(
                    select(
                        exists().where(
                            ClientContact.organization_id == organization_id,
                            ClientContact.client_id == target_id,
                            ClientContact.is_primary.is_(True),
                        )
                    )
                )
            ).scalar_one()
        )
        demoted = 0
        if target_has_primary:
            demoted = int(
                (
                    await self.session.execute(
                        select(func.count()).where(
                            ClientContact.organization_id == organization_id,
                            ClientContact.client_id == source_id,
                            ClientContact.is_primary.is_(True),
                        )
                    )
                ).scalar_one()
            )
        stmt = (
            update(ClientContact)
            .where(
                ClientContact.organization_id == organization_id,
                ClientContact.client_id == source_id,
            )
            .values(client_id=target_id)
            .returning(ClientContact.id)
            .execution_options(synchronize_session=False)
        )
        if target_has_primary:
            stmt = stmt.values(is_primary=False)
        moved = len((await self.session.execute(stmt)).all())
        return moved, demoted

    async def move_engagements(
        self, organization_id: uuid.UUID, source_id: uuid.UUID, target_id: uuid.UUID
    ) -> tuple[int, list[dict[str, Any]]]:
        """Move the services the source takes, except those the target takes already (the
        target's agreement wins). Returns the number moved and the ones left on the source."""
        theirs = select(ClientService.service_id).where(
            ClientService.organization_id == organization_id, ClientService.client_id == target_id
        )
        conflicts = (
            await self.session.execute(
                select(ClientService, Service.code, Service.name)
                .join(
                    Service,
                    (Service.organization_id == ClientService.organization_id)
                    & (Service.id == ClientService.service_id),
                )
                .where(
                    ClientService.organization_id == organization_id,
                    ClientService.client_id == source_id,
                    ClientService.service_id.in_(theirs),
                )
                .order_by(Service.code)
            )
        ).all()
        left = [
            {
                "id": str(row[0].id),
                "service_id": str(row[0].service_id),
                "service_code": row.code,
                "service_name": row.name,
                "fee": row[0].fee,
                "frequency": row[0].frequency,
            }
            for row in conflicts
        ]
        stmt = (
            update(ClientService)
            .where(
                ClientService.organization_id == organization_id,
                ClientService.client_id == source_id,
                ClientService.id.not_in([row[0].id for row in conflicts]),
            )
            .values(client_id=target_id)
            .returning(ClientService.id)
            .execution_options(synchronize_session=False)
        )
        return len((await self.session.execute(stmt)).all()), left

    async def move_rate_rules(
        self, organization_id: uuid.UUID, source_id: uuid.UUID, target_id: uuid.UUID
    ) -> tuple[list[RateRule], list[dict[str, Any]]]:
        """Move the source's rate rules, except those whose period overlaps a rule of the same
        kind and scope on the target (the target's rate wins; two overlapping rules of one
        scope cannot exist). Returns the rules moved (with their new client) and the ones left
        on the source."""
        mine = aliased(RateRule)
        theirs = aliased(RateRule)
        overlapping = exists().where(
            theirs.organization_id == mine.organization_id,
            theirs.client_id == target_id,
            theirs.kind == mine.kind,
            theirs.service_id.is_not_distinct_from(mine.service_id),
            theirs.user_id.is_not_distinct_from(mine.user_id),
            theirs.grade_id.is_not_distinct_from(mine.grade_id),
            func.daterange(theirs.effective_from, theirs.effective_to, "[]").op("&&")(
                func.daterange(mine.effective_from, mine.effective_to, "[]")
            ),
        )
        conflicts = list(
            (
                await self.session.execute(
                    select(mine)
                    .where(
                        mine.organization_id == organization_id,
                        mine.client_id == source_id,
                        overlapping,
                    )
                    .order_by(mine.effective_from, mine.id)
                )
            ).scalars()
        )
        left = [
            {
                "id": str(rule.id),
                "kind": rule.kind,
                "level": rule.level,
                "service_id": str(rule.service_id) if rule.service_id else None,
                "user_id": str(rule.user_id) if rule.user_id else None,
                "rate": rule.rate,
                "effective_from": _iso(rule.effective_from),
                "effective_to": _iso(rule.effective_to),
            }
            for rule in conflicts
        ]
        stmt = (
            update(RateRule)
            .where(
                RateRule.organization_id == organization_id,
                RateRule.client_id == source_id,
                RateRule.id.not_in([rule.id for rule in conflicts]),
            )
            .values(client_id=target_id)
            .returning(RateRule)
            .execution_options(populate_existing=True, synchronize_session=False)
        )
        moved = list((await self.session.execute(stmt)).scalars())
        return moved, left

    async def add_rate_history(self, rows: list[RateChange]) -> None:
        self.session.add_all(rows)
        await self.session.flush()

    async def repoint_merged_sources(
        self, organization_id: uuid.UUID, source_id: uuid.UUID, target_id: uuid.UUID
    ) -> int:
        """Clients that were merged into the source now point at the survivor."""
        stmt = (
            update(Client)
            .where(
                Client.organization_id == organization_id,
                Client.merged_into_client_id == source_id,
            )
            .values(merged_into_client_id=target_id)
            .returning(Client.id)
            .execution_options(synchronize_session=False)
        )
        return len((await self.session.execute(stmt)).all())
