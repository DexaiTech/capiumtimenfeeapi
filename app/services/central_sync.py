"""The central client integration, as users drive it: search the central service, enable one of
its clients in the practice, link and unlink existing clients, and synchronise names and
addresses (docs/modules/central-clients.md).

Rules (docs/domain.md, "Central clients"):

* A central client is enabled at most once per practice. Enabling one that is already enabled
  answers with the existing client; a client of the practice that is not linked and has the same
  company number (normalised) is linked instead of a second one being created.
* The name and address of a linked client are managed centrally: they come from the central
  service, and changing them on the client is refused (``ClientsService``).
* While the central service is down, searching and enabling use the practice's cache and a
  synchronisation keeps the stored values, marks the client ``stale`` and logs the failure; none
  of them answer with a server error.
* Every attempt is written to the integration log; every change to the client is audited.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import (
    AppException,
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
)
from app.db.models import CentralClientCache, Client
from app.db.session import unit_of_work
from app.repositories.central_clients import CacheRow, CentralClientRepository
from app.repositories.clients import ClientRepository
from app.schemas.central_clients import (
    CentralClientRead,
    CentralEnabled,
    CentralSearchResult,
    CentralSyncAll,
    ClientSynced,
    MatchingClient,
)
from app.schemas.clients import CENTRALLY_MANAGED_FIELDS, ClientDetail
from app.services import audit
from app.services.billing_common import conflict
from app.services.central_clients import (
    CentralClient,
    CentralClientGateway,
    CentralUnavailableError,
)
from app.services.clients import ClientsService, generate_client_codes
from app.services.integration_logs import CENTRAL_CLIENT, record_sync
from app.services.members import integrity_constraint
from app.services.policy import OrgContext
from app.utils.company_numbers import normalise_company_number

MAX_SYNC_CLIENTS = 200
UNIQUE_CENTRAL_INDEX = "uq_clients_organization_id_central_client_id"
UNAVAILABLE_MESSAGE = (
    "The central client service is unavailable: the stored name and address were kept."
)
EnableOutcome = Literal["created", "linked_existing", "already_enabled"]


def now_utc() -> datetime:
    return datetime.now(UTC)


def cache_row(client: CentralClient) -> CacheRow:
    return CacheRow(
        central_id=client.central_id,
        name=client.name,
        company_number=normalise_company_number(client.company_number),
        address_line1=client.address_line1,
        address_line2=client.address_line2,
        city=client.city,
        postcode=client.postcode,
        country=client.country,
        status=client.status,
        source_updated_at=client.updated_at,
    )


def central_from_cache(row: CentralClientCache) -> CentralClient:
    return CentralClient(
        central_id=row.central_id,
        name=row.name,
        company_number=row.company_number,
        address_line1=row.address_line1,
        address_line2=row.address_line2,
        city=row.city,
        postcode=row.postcode,
        country=row.country,
        status="archived" if row.status == "archived" else "active",
        updated_at=row.source_updated_at,
    )


def unavailable_error(message: str) -> ServiceUnavailableError:
    return ServiceUnavailableError(
        message, code="CENTRAL_UNAVAILABLE", headers={"Retry-After": "30"}
    )


def already_linked_error(other: Client) -> ConflictError:
    return conflict(
        "CENTRAL_CLIENT_ALREADY_LINKED",
        "This central client is already linked to another client of the practice.",
        details={"client_id": str(other.id), "client_code": other.code},
    )


def archived_error() -> ConflictError:
    return conflict(
        "CENTRAL_CLIENT_ARCHIVED",
        "This central client is archived: it cannot be enabled or linked.",
    )


def apply_central_fields(client: Client, central: CentralClient) -> dict[str, list[str | None]]:
    """Copy the centrally managed fields onto the client; returns ``{field: [old, new]}``."""
    changes: dict[str, list[str | None]] = {}
    for field in CENTRALLY_MANAGED_FIELDS:
        new: str | None = getattr(central, field)
        old: str | None = getattr(client, field)
        if old != new:
            changes[field] = [old, new]
            setattr(client, field, new)
    return changes


@dataclass(frozen=True, slots=True)
class Fetched:
    """A central client and where it came from."""

    client: CentralClient
    from_cache: bool
    fetched_at: datetime


class CentralClientService:
    def __init__(self, session: AsyncSession, gateway: CentralClientGateway) -> None:
        self.session = session
        self.gateway = gateway
        self.central = CentralClientRepository(session)
        self.clients = ClientRepository(session)
        self.client_service = ClientsService(session)

    async def _audit(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        action: str,
        client: Client,
        meta: dict[str, Any],
    ) -> None:
        await audit.record(
            self.session,
            action,
            ctx=rctx,
            actor_user_id=ctx.user.id,
            organization_id=ctx.organization_id,
            target_type="client",
            target_id=client.id,
            meta={"code": client.code, "name": client.name, **meta},
        )

    async def _client(
        self, ctx: OrgContext, client_id: uuid.UUID, *, lock: str | None = None
    ) -> Client:
        client = await self.clients.get(ctx.organization_id, client_id, lock=lock)
        if client is None:
            raise NotFoundError("Client not found.")
        return client

    async def _fetch(self, organization_id: uuid.UUID, central_id: str) -> Fetched:
        """The central client from the central service, else from the cache."""
        try:
            found = await self.gateway.get(central_id)
        except CentralUnavailableError:
            cached = await self.central.get_cache(organization_id, central_id)
            if cached is None:
                raise unavailable_error(
                    "The central client service is unavailable and this client is not cached."
                ) from None
            return Fetched(central_from_cache(cached), True, cached.fetched_at)
        if found is None:
            raise NotFoundError("Central client not found.")
        return Fetched(found, False, now_utc())

    async def _remember(self, organization_id: uuid.UUID, fetched: Fetched) -> None:
        """A live answer refreshes the cache; a cached one is already there."""
        if not fetched.from_cache:
            await self.central.upsert_cache(
                organization_id, [cache_row(fetched.client)], fetched.fetched_at
            )

    @staticmethod
    def _ensure_syncable(client: Client) -> None:
        if client.central_client_id is None:
            raise conflict("CLIENT_NOT_LINKED", "This client is not linked to a central client.")
        if client.merged_into_client_id is not None:
            raise conflict("CLIENT_MERGED", "This client was merged into another client.")

    # --- Search ---------------------------------------------------------------------------

    async def _items(
        self,
        organization_id: uuid.UUID,
        found: list[CentralClient],
        fetched: Mapping[str, datetime | None],
    ) -> list[CentralClientRead]:
        linked = await self.central.clients_by_central_ids(
            organization_id, [c.central_id for c in found]
        )
        numbers = {
            n
            for c in found
            if c.central_id not in linked and (n := normalise_company_number(c.company_number))
        }
        matches = await self.central.unlinked_by_company_numbers(organization_id, numbers)
        items = []
        for c in found:
            number = normalise_company_number(c.company_number)
            local = linked.get(c.central_id)
            candidates = matches.get(number, []) if number and local is None else []
            match = candidates[0] if candidates else None
            items.append(
                CentralClientRead(
                    central_id=c.central_id,
                    name=c.name,
                    company_number=number,
                    address_line1=c.address_line1,
                    address_line2=c.address_line2,
                    city=c.city,
                    postcode=c.postcode,
                    country=c.country,
                    status=c.status,
                    updated_at=c.updated_at,
                    fetched_at=fetched.get(c.central_id),
                    enabled=local is not None,
                    client_id=local.id if local else None,
                    client_code=local.code if local else None,
                    matching_client=(
                        MatchingClient(id=match.id, code=match.code, name=match.name)
                        if match
                        else None
                    ),
                )
            )
        return items

    async def search(self, ctx: OrgContext, q: str, limit: int) -> CentralSearchResult:
        org_id = ctx.organization_id
        try:
            found = await self.gateway.search(q, limit)
        except CentralUnavailableError:
            return await self._search_cache(ctx, q, limit)
        fetched_at = now_utc()
        async with unit_of_work(self.session):
            await self.central.upsert_cache(org_id, [cache_row(c) for c in found], fetched_at)
        items = await self._items(
            org_id, found, dict.fromkeys((c.central_id for c in found), fetched_at)
        )
        return CentralSearchResult(items=items, source="live", stale=False)

    async def _search_cache(self, ctx: OrgContext, q: str, limit: int) -> CentralSearchResult:
        org_id = ctx.organization_id
        rows = await self.central.search_cache(org_id, q, limit)
        if not rows and not await self.central.has_cache(org_id):
            raise unavailable_error(
                "The central client service is unavailable and no central clients are cached yet."
            )
        items = await self._items(
            org_id,
            [central_from_cache(r) for r in rows],
            {r.central_id: r.fetched_at for r in rows},
        )
        return CentralSearchResult(items=items, source="cache", stale=True)

    # --- Enable ---------------------------------------------------------------------------

    async def enable(
        self, ctx: OrgContext, central_id: str, rctx: RequestContext
    ) -> tuple[CentralEnabled, bool]:
        """Enable a central client in the practice. Returns the outcome and whether a client
        was created."""
        org_id = ctx.organization_id
        existing = await self.central.client_by_central_id(org_id, central_id)
        if existing is not None:
            return await self._enabled(ctx, existing, "already_enabled", False), False
        fetched = await self._fetch(org_id, central_id)
        if fetched.client.status == "archived":
            raise archived_error()
        client: Client
        outcome: EnableOutcome
        try:
            async with unit_of_work(self.session):
                # Two requests enabling the same central client queue up here: the second one
                # finds the first one's client.
                await self.central.lock_central_ids(org_id, [central_id])
                existing = await self.central.client_by_central_id(org_id, central_id)
                if existing is not None:
                    client, outcome = existing, "already_enabled"
                else:
                    await self._remember(org_id, fetched)
                    client, outcome = await self._enable_new(ctx, rctx, fetched)
        except IntegrityError as exc:
            if integrity_constraint(exc) == UNIQUE_CENTRAL_INDEX:
                raise conflict(
                    "CENTRAL_CLIENT_ALREADY_LINKED",
                    "This central client was just linked to a client of the practice.",
                ) from exc
            raise
        enabled = await self._enabled(ctx, client, outcome, fetched.from_cache)
        return enabled, outcome == "created"

    async def _enabled(
        self, ctx: OrgContext, client: Client, outcome: EnableOutcome, from_cache: bool
    ) -> CentralEnabled:
        return CentralEnabled(
            result=outcome,
            from_cache=from_cache,
            client=await self.client_service.get_detail(ctx, client.id),
        )

    async def _enable_new(
        self, ctx: OrgContext, rctx: RequestContext, fetched: Fetched
    ) -> tuple[Client, EnableOutcome]:
        """Link the practice's own client with this company number, else create one."""
        org_id = ctx.organization_id
        central = fetched.client
        number = normalise_company_number(central.company_number)
        state = "stale" if fetched.from_cache else "synced"
        candidate: Client | None = None
        outcome: EnableOutcome
        changes: dict[str, list[str | None]] = {}
        if number:
            for found in (await self.central.unlinked_by_company_numbers(org_id, {number})).get(
                number, []
            ):
                candidate = await self.clients.get(org_id, found.id, lock="no_key_update")
                if (
                    candidate is not None
                    and candidate.central_client_id is None
                    and candidate.merged_into_client_id is None
                ):
                    break
                candidate = None
        if candidate is not None:
            changes = apply_central_fields(candidate, central)
            candidate.central_client_id = central.central_id
            candidate.central_sync_state = state
            candidate.central_synced_at = fetched.fetched_at
            if candidate.company_number is None:
                candidate.company_number = number
            client = await self.clients.save(candidate)
            outcome = "linked_existing"
        else:
            changes = {}
            code = (await generate_client_codes(self.session, org_id, 1, set()))[0]
            client = await self.clients.add(
                Client(
                    organization_id=org_id,
                    code=code,
                    name=central.name,
                    # A company number says it is a limited company; without one, a person.
                    client_type="limited_company" if number else "sole_trader",
                    status="active",
                    address_line1=central.address_line1,
                    address_line2=central.address_line2,
                    city=central.city,
                    postcode=central.postcode,
                    country=central.country,
                    company_number=number,
                    central_client_id=central.central_id,
                    central_sync_state=state,
                    central_synced_at=fetched.fetched_at,
                )
            )
            outcome = "created"
        meta = {
            "central_id": central.central_id,
            "result": outcome,
            "from_cache": fetched.from_cache,
            "changes": changes,
        }
        await self._audit(ctx, rctx, "client.central_enabled", client, meta)
        await record_sync(
            self.session,
            org_id,
            integration=CENTRAL_CLIENT,
            operation="enable",
            status="ok",
            message=f"Enabled as {client.code} ({outcome.replace('_', ' ')}).",
            entity_type="client",
            entity_id=client.id,
            external_id=central.central_id,
            details=meta,
        )
        return client, outcome

    # --- Link and unlink ------------------------------------------------------------------

    async def link(
        self, ctx: OrgContext, client_id: uuid.UUID, central_id: str, rctx: RequestContext
    ) -> ClientDetail:
        """Link a client of the practice to a central client; it takes the central name and
        address."""
        org_id = ctx.organization_id
        await self._client(ctx, client_id)
        fetched = await self._fetch(org_id, central_id)
        if fetched.client.status == "archived":
            raise archived_error()
        try:
            async with unit_of_work(self.session):
                await self.central.lock_central_ids(org_id, [central_id])
                client = await self._client(ctx, client_id, lock="no_key_update")
                if client.central_client_id is not None:
                    raise conflict(
                        "CLIENT_ALREADY_LINKED",
                        "This client is already linked to a central client: unlink it first.",
                        details={"central_id": client.central_client_id},
                    )
                if client.merged_into_client_id is not None:
                    raise conflict("CLIENT_MERGED", "This client was merged into another client.")
                other = await self.central.client_by_central_id(org_id, central_id)
                if other is not None:
                    raise already_linked_error(other)
                await self._remember(org_id, fetched)
                central = fetched.client
                changes = apply_central_fields(client, central)
                number = normalise_company_number(central.company_number)
                if client.company_number is None and number:
                    client.company_number = number
                client.central_client_id = central_id
                client.central_sync_state = "stale" if fetched.from_cache else "synced"
                client.central_synced_at = fetched.fetched_at
                await self.clients.save(client)
                meta = {
                    "central_id": central_id,
                    "from_cache": fetched.from_cache,
                    "changes": changes,
                }
                await self._audit(ctx, rctx, "client.central_linked", client, meta)
                await record_sync(
                    self.session,
                    org_id,
                    integration=CENTRAL_CLIENT,
                    operation="link",
                    status="ok",
                    message=f"Linked {client.code} to the central client.",
                    entity_type="client",
                    entity_id=client.id,
                    external_id=central_id,
                    details=meta,
                )
        except IntegrityError as exc:
            if integrity_constraint(exc) == UNIQUE_CENTRAL_INDEX:
                raise conflict(
                    "CENTRAL_CLIENT_ALREADY_LINKED",
                    "This central client was just linked to another client of the practice.",
                ) from exc
            raise
        return await self.client_service.get_detail(ctx, client_id)

    async def unlink(
        self, ctx: OrgContext, client_id: uuid.UUID, rctx: RequestContext
    ) -> ClientDetail:
        """Make a linked client a local one again: its name and address become editable."""
        async with unit_of_work(self.session):
            client = await self._client(ctx, client_id, lock="no_key_update")
            if client.central_client_id is None:
                raise conflict(
                    "CLIENT_NOT_LINKED", "This client is not linked to a central client."
                )
            central_id = client.central_client_id
            client.central_client_id = None
            client.central_sync_state = "local"
            client.central_synced_at = None
            await self.clients.save(client)
            meta = {"central_id": central_id}
            await self._audit(ctx, rctx, "client.central_unlinked", client, meta)
            await record_sync(
                self.session,
                ctx.organization_id,
                integration=CENTRAL_CLIENT,
                operation="unlink",
                status="ok",
                message=f"Unlinked {client.code} from the central client.",
                entity_type="client",
                entity_id=client.id,
                external_id=central_id,
                details=meta,
            )
        return await self.client_service.get_detail(ctx, client_id)

    # --- Synchronise ----------------------------------------------------------------------

    async def sync_client(
        self, ctx: OrgContext, client_id: uuid.UUID, rctx: RequestContext
    ) -> ClientSynced:
        result, _ = await self._sync_client(ctx, client_id, rctx, gateway_down=False)
        return result

    async def sync_all(self, ctx: OrgContext, rctx: RequestContext) -> CentralSyncAll:
        """Synchronise the linked clients (at most ``MAX_SYNC_CLIENTS``, the longest unsynced
        first). Once the central service fails, the rest are marked stale without more calls."""
        clients, remaining = await self.central.linked_for_sync(
            ctx.organization_id, MAX_SYNC_CLIENTS
        )
        results: list[ClientSynced] = []
        down = False
        for client in clients:
            try:
                result, down_now = await self._sync_client(ctx, client.id, rctx, gateway_down=down)
            except AppException:
                continue  # archived, unlinked or merged since the list was read
            down = down or down_now
            results.append(result)
        return CentralSyncAll(
            available=not down,
            processed=len(results),
            synced=sum(1 for r in results if r.state == "synced"),
            stale=sum(1 for r in results if r.state == "stale"),
            failed=sum(1 for r in results if r.state == "failed"),
            changed=sum(1 for r in results if r.changed),
            remaining=remaining,
            results=results,
        )

    async def _sync_client(
        self,
        ctx: OrgContext,
        client_id: uuid.UUID,
        rctx: RequestContext,
        *,
        gateway_down: bool,
    ) -> tuple[ClientSynced, bool]:
        """One client. Returns the result and whether the central service was unavailable."""
        org_id = ctx.organization_id
        probe = await self._client(ctx, client_id)
        self._ensure_syncable(probe)
        central_id = probe.central_client_id
        assert central_id is not None
        central: CentralClient | None = None
        down = gateway_down
        if not down:
            # Asked before the row lock: no transaction waits for the central service.
            try:
                central = await self.gateway.get(central_id)
            except CentralUnavailableError:
                down = True
        async with unit_of_work(self.session):
            client = await self._client(ctx, client_id, lock="no_key_update")
            self._ensure_syncable(client)
            if client.central_client_id != central_id:
                raise conflict(
                    "CENTRAL_LINK_CHANGED", "The client's central link changed: try again."
                )
            old_state = client.central_sync_state
            changes: dict[str, list[str | None]] = {}
            state: Literal["synced", "stale", "failed"]
            if down:
                state, message = "stale", UNAVAILABLE_MESSAGE
            elif central is None:
                state, message = "failed", "The central service no longer knows this client."
            else:
                changes = apply_central_fields(client, central)
                state = "synced"
                message = "Name and address updated." if changes else "Already up to date."
                client.central_synced_at = now_utc()
                await self.central.upsert_cache(org_id, [cache_row(central)], now_utc())
            client.central_sync_state = state
            await self.clients.save(client)
            meta: dict[str, Any] = {
                "central_id": central_id,
                "state": state,
                "changes": changes,
                "message": message,
            }
            if changes or state != old_state or state != "synced":
                await self._audit(ctx, rctx, "client.central_synced", client, meta)
            await record_sync(
                self.session,
                org_id,
                integration=CENTRAL_CLIENT,
                operation="sync",
                status="ok" if state == "synced" else "failed",
                message=f"{client.code}: {message}",
                entity_type="client",
                entity_id=client.id,
                external_id=central_id,
                details=meta,
            )
        return (
            ClientSynced(
                client_id=client.id,
                code=client.code,
                name=client.name,
                central_id=central_id,
                state=state,
                changed=bool(changes),
                changes=changes,
                from_cache=down,
                message=message,
                synced_at=client.central_synced_at,
            ),
            down,
        )
