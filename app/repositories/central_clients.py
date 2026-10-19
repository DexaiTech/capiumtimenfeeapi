"""Queries for the central client integration: the cache of central clients and the clients
that are, or could be, linked to one. Every method is scoped to one organization."""

import uuid
from collections.abc import Collection, Iterable, Sequence
from datetime import datetime
from typing import TypedDict

from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import ColumnElement, ColumnExpressionArgument

from app.db.models import CentralClientCache, Client
from app.utils.ids import new_id


class CacheRow(TypedDict):
    """What the cache keeps of a central client (company number already normalised)."""

    central_id: str
    name: str
    company_number: str | None
    address_line1: str | None
    address_line2: str | None
    city: str | None
    postcode: str | None
    country: str
    status: str
    source_updated_at: datetime


def normalised_company_number(
    column: ColumnExpressionArgument[str | None],
) -> ColumnElement[str | None]:
    """SQL twin of ``normalise_company_number``: upper case, letters and digits only, an
    all-digit number padded to eight digits (blank: null)."""
    cleaned = func.regexp_replace(func.upper(column), "[^A-Z0-9]", "", "g")
    return case(
        (cleaned.op("~")(r"^[0-9]{1,7}$"), func.lpad(cleaned, 8, "0")),
        else_=func.nullif(cleaned, ""),
    )


def _live_clients(organization_id: uuid.UUID) -> Select[Client]:
    return select(Client).where(
        Client.organization_id == organization_id, Client.deleted_at.is_(None)
    )


class CentralClientRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- The cache -----------------------------------------------------------------------

    async def upsert_cache(
        self, organization_id: uuid.UUID, clients: Sequence[CacheRow], fetched_at: datetime
    ) -> None:
        """Remember what the central service said; rows are written in id order so two
        requests refreshing overlapping sets cannot deadlock."""
        if not clients:
            return
        rows = [
            {
                **c,
                "id": new_id(),
                "organization_id": organization_id,
                "fetched_at": fetched_at,
            }
            for c in sorted(clients, key=lambda c: c["central_id"])
        ]
        stmt = insert(CentralClientCache).values(rows)
        await self.session.execute(
            stmt.on_conflict_do_update(
                index_elements=["organization_id", "central_id"],
                set_={
                    column: stmt.excluded[column]
                    for column in (
                        "name",
                        "company_number",
                        "address_line1",
                        "address_line2",
                        "city",
                        "postcode",
                        "country",
                        "status",
                        "source_updated_at",
                        "fetched_at",
                    )
                },
            )
        )

    async def get_cache(
        self, organization_id: uuid.UUID, central_id: str
    ) -> CentralClientCache | None:
        stmt = (
            select(CentralClientCache)
            .where(
                CentralClientCache.organization_id == organization_id,
                CentralClientCache.central_id == central_id,
            )
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def has_cache(self, organization_id: uuid.UUID) -> bool:
        stmt = select(CentralClientCache.id).where(
            CentralClientCache.organization_id == organization_id
        )
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def search_cache(
        self, organization_id: uuid.UUID, q: str, limit: int
    ) -> list[CentralClientCache]:
        """Cached clients whose name or company number contains ``q`` (all for a blank one)."""
        stmt = select(CentralClientCache).where(
            CentralClientCache.organization_id == organization_id
        )
        needle = q.strip()
        if needle:
            number = "".join(ch for ch in needle if ch.isalnum()).upper()
            conditions = [CentralClientCache.name.icontains(needle, autoescape=True)]
            if len(needle) >= 3 and number:
                conditions.append(
                    CentralClientCache.company_number.icontains(number, autoescape=True)
                )
            stmt = stmt.where(or_(*conditions))
        stmt = stmt.order_by(func.lower(CentralClientCache.name), CentralClientCache.central_id)
        return list((await self.session.execute(stmt.limit(limit))).scalars())

    # --- Clients linked to a central client ----------------------------------------------

    async def client_by_central_id(
        self, organization_id: uuid.UUID, central_id: str, *, lock: bool = False
    ) -> Client | None:
        """The live client enabled from ``central_id`` (at most one: a unique index)."""
        stmt = _live_clients(organization_id).where(Client.central_client_id == central_id)
        if lock:
            stmt = stmt.with_for_update()
        stmt = stmt.execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def clients_by_central_ids(
        self, organization_id: uuid.UUID, central_ids: Collection[str]
    ) -> dict[str, Client]:
        if not central_ids:
            return {}
        stmt = _live_clients(organization_id).where(Client.central_client_id.in_(list(central_ids)))
        return {
            c.central_client_id: c
            for c in (await self.session.execute(stmt)).scalars()
            if c.central_client_id
        }

    async def unlinked_by_company_numbers(
        self, organization_id: uuid.UUID, numbers: Collection[str]
    ) -> dict[str, list[Client]]:
        """Live clients that are not linked and not merged away, by normalised company number
        (best candidate first: not archived, then oldest)."""
        if not numbers:
            return {}
        number = normalised_company_number(Client.company_number)
        stmt = (
            _live_clients(organization_id)
            .where(
                Client.central_client_id.is_(None),
                Client.merged_into_client_id.is_(None),
                number.in_(list(numbers)),
            )
            .add_columns(number.label("normalised"))
            .order_by(Client.status == "archived", Client.created_at, Client.id)
        )
        found: dict[str, list[Client]] = {}
        for row in (await self.session.execute(stmt)).all():
            found.setdefault(row.normalised, []).append(row[0])
        return found

    async def linked_with_company_number(
        self, organization_id: uuid.UUID, numbers: Collection[str]
    ) -> dict[str, Client]:
        """Live clients enabled from the central service that have one of these (normalised)
        company numbers: their own, or their central client's as cached."""
        if not numbers:
            return {}
        number = normalised_company_number(Client.company_number)
        stmt = (
            _live_clients(organization_id)
            .where(Client.central_client_id.is_not(None), number.in_(list(numbers)))
            .add_columns(number.label("normalised"))
            .order_by(Client.created_at, Client.id)
        )
        found: dict[str, Client] = {}
        for row in (await self.session.execute(stmt)).all():
            found.setdefault(row.normalised, row[0])
        via_cache = (
            _live_clients(organization_id)
            .join(
                CentralClientCache,
                (CentralClientCache.organization_id == Client.organization_id)
                & (CentralClientCache.central_id == Client.central_client_id),
            )
            .where(CentralClientCache.company_number.in_(list(numbers)))
            .add_columns(CentralClientCache.company_number.label("normalised"))
            .order_by(Client.created_at, Client.id)
        )
        for row in (await self.session.execute(via_cache)).all():
            found.setdefault(row.normalised, row[0])
        return found

    async def linked_for_sync(
        self, organization_id: uuid.UUID, limit: int
    ) -> tuple[list[Client], int]:
        """Linked clients that are not archived or merged away, the least recently synced first
        (at most ``limit``), and how many more there are."""
        base = _live_clients(organization_id).where(
            Client.central_client_id.is_not(None),
            Client.status != "archived",
            Client.merged_into_client_id.is_(None),
        )
        total = int(
            (
                await self.session.execute(
                    select(func.count()).select_from(base.order_by(None).subquery())
                )
            ).scalar_one()
        )
        stmt = base.order_by(Client.central_synced_at.asc().nulls_first(), Client.id).limit(limit)
        clients = list((await self.session.execute(stmt)).scalars())
        return clients, max(0, total - len(clients))

    async def lock_central_ids(
        self, organization_id: uuid.UUID, central_ids: Iterable[str]
    ) -> None:
        """Serialise changes to the clients enabled from these central ids: a transaction-scoped
        advisory lock per id, in id order. Two requests enabling or linking the same central
        client then run one after the other and the second sees the first one's client."""
        for central_id in sorted(set(central_ids)):
            await self.session.execute(
                select(
                    func.pg_advisory_xact_lock(
                        func.hashtextextended(f"central_client:{organization_id}:{central_id}", 0)
                    )
                )
            )
