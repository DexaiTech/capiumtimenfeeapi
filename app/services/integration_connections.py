"""The integration cards of a practice: what each integration is, whether it is on, how it last did
and what it has queued, and the changes a person can make (switch it on or off, its settings).

The central client service is part of the client flow and cannot be switched off; the central user
service is on until it is switched off; practice management and bookkeeping are off until a
practice turns them on. Every change is audited. ``record_run`` is how the runners note an outcome
on the practice's connection.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.context import RequestContext
from app.core.exceptions import ConflictError, ValidationError
from app.db.models import IntegrationConnection
from app.db.session import unit_of_work
from app.repositories.integration_items import ConnectionRepository, SyncItemRepository
from app.repositories.integration_summary import IntegrationSummaryRepository
from app.schemas.integration_connections import (
    CONFIG_MODELS,
    CardStatus,
    IntegrationCard,
    IntegrationKey,
    IntegrationMock,
    IntegrationUpdate,
)
from app.services.central_clients import MockCentralClientGateway, get_central_gateway
from app.services.common import record_audit
from app.services.integrations import (
    BOOKKEEPING,
    CENTRAL_CLIENT,
    CENTRAL_USER,
    PRACTICE_MANAGEMENT,
    MockBookkeepingGateway,
    MockCentralUserGateway,
    MockPracticeManagementGateway,
    get_bookkeeping_gateway,
    get_central_user_gateway,
    get_practice_management_gateway,
)
from app.services.policy import OrgContext
from app.utils.ids import new_id


@dataclass(frozen=True, slots=True)
class IntegrationInfo:
    key: str
    name: str
    description: str
    phase: str | None
    default_enabled: bool
    can_disable: bool


INFO: dict[str, IntegrationInfo] = {
    info.key: info
    for info in (
        IntegrationInfo(
            CENTRAL_CLIENT,
            "Central Client",
            "The Capium group's master list of clients: names and addresses follow it, and "
            "renamed, archived, restored and merged clients are applied.",
            None,
            True,
            False,
        ),
        IntegrationInfo(
            CENTRAL_USER,
            "Central User",
            "The Capium group's directory of users: names and active status of the practice's "
            "staff follow it.",
            None,
            True,
            True,
        ),
        IntegrationInfo(
            PRACTICE_MANAGEMENT,
            "Practice Management",
            "Jobs and tasks come from practice management and their statuses stay in step in "
            "both directions.",
            "2A",
            False,
            True,
        ),
        IntegrationInfo(
            BOOKKEEPING,
            "Bookkeeping",
            "Issued invoices and credit notes are sent to bookkeeping with the income account "
            "of each service and the tax code of each VAT code.",
            "2B",
            False,
            True,
        ),
    )
}
ORDER = (CENTRAL_CLIENT, CENTRAL_USER, PRACTICE_MANAGEMENT, BOOKKEEPING)


class SwitchableMock(Protocol):
    @property
    def available(self) -> bool: ...

    def set_available(self, available: bool) -> None: ...


def mock_for(key: str) -> SwitchableMock | None:
    """The mock service of an integration while it is in use (never in production)."""
    if get_settings().is_production:
        return None
    gateway: object
    match key:
        case "central_client":
            gateway = get_central_gateway()
        case "central_user":
            gateway = get_central_user_gateway()
        case "practice_management":
            gateway = get_practice_management_gateway()
        case "bookkeeping":
            gateway = get_bookkeeping_gateway()
        case _:
            return None
    if isinstance(
        gateway,
        MockCentralClientGateway
        | MockCentralUserGateway
        | MockPracticeManagementGateway
        | MockBookkeepingGateway,
    ):
        return gateway
    return None


def validate_config(key: str, raw: dict[str, Any]) -> dict[str, Any]:
    """The settings of an integration checked and completed with their defaults (422 for an
    unknown or invalid setting)."""
    try:
        return CONFIG_MODELS[key].model_validate(raw).model_dump(mode="json")
    except PydanticValidationError as exc:
        raise ValidationError(
            details=[
                {
                    "field": ".".join(["config", *(str(part) for part in err["loc"])]),
                    "code": str(err["type"]),
                    "message": str(err["msg"]).removeprefix("Value error, "),
                }
                for err in exc.errors()
            ]
        ) from exc


def disabled_error(key: str) -> ConflictError:
    return ConflictError(
        f"{INFO[key].name} is switched off for this practice.", code="INTEGRATION_DISABLED"
    )


async def record_run(
    session: AsyncSession,
    organization_id: uuid.UUID,
    integration: str,
    *,
    succeeded: bool,
    error: str | None,
) -> None:
    """Note how a run of the integration went on the practice's connection."""
    await ConnectionRepository(session).record_run(
        organization_id,
        integration,
        enabled_default=INFO[integration].default_enabled,
        succeeded=succeeded,
        error=error,
        at=datetime.now(UTC),
    )


class ConnectionService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.connections = ConnectionRepository(session)
        self.items = SyncItemRepository(session)
        self.summary = IntegrationSummaryRepository(session)

    async def is_enabled(self, organization_id: uuid.UUID, key: str) -> bool:
        connection = await self.connections.get(organization_id, key)
        return connection.enabled if connection else INFO[key].default_enabled

    async def ensure_enabled(self, organization_id: uuid.UUID, key: str) -> None:
        """409 ``INTEGRATION_DISABLED`` while the practice has the integration switched off."""
        if not await self.is_enabled(organization_id, key):
            raise disabled_error(key)

    async def config_of(self, organization_id: uuid.UUID, key: str) -> dict[str, Any]:
        connection = await self.connections.get(organization_id, key)
        return validate_config(key, connection.config if connection else {})

    # --- Reads ----------------------------------------------------------------------------

    async def cards(self, ctx: OrgContext) -> list[IntegrationCard]:
        org_id = ctx.organization_id
        connections = await self.connections.list(org_id)
        counts = await self.items.counts(org_id)
        return [
            await self._card(org_id, key, connections.get(key), counts.get(key, {}))
            for key in ORDER
        ]

    async def card(self, ctx: OrgContext, key: str) -> IntegrationCard:
        org_id = ctx.organization_id
        connection = await self.connections.get(org_id, key)
        counts = await self.items.counts(org_id)
        return await self._card(org_id, key, connection, counts.get(key, {}))

    async def _card(
        self,
        organization_id: uuid.UUID,
        key: str,
        connection: IntegrationConnection | None,
        counts: dict[str, int],
    ) -> IntegrationCard:
        info = INFO[key]
        enabled = connection.enabled if connection else info.default_enabled
        last_success = connection.last_success_at if connection else None
        last_attempt = connection.last_attempt_at if connection else None
        last_error = connection.last_error if connection else None
        failed_last = connection is not None and connection.status == "error"
        summary: dict[str, int] = {}
        if key == CENTRAL_CLIENT:
            # The central client integration predates the connection row: its log says it all.
            outcome = await self.summary.log_outcome(organization_id, key)
            last_success, last_attempt = outcome.last_success_at, outcome.last_attempt_at
            last_error, failed_last = outcome.last_error, outcome.last_error is not None
            summary = await self.summary.client_links(organization_id)
        elif key == PRACTICE_MANAGEMENT:
            summary = await self.summary.pm_links(organization_id)
        elif key == BOOKKEEPING:
            summary = {"sent": counts.get("ok", 0), "skipped": counts.get("skipped", 0)}
        pending, failed = counts.get("pending", 0), counts.get("failed", 0)
        status: CardStatus
        if not enabled:
            status = "disabled"
        elif failed_last or failed:
            status = "error"
        elif last_success is not None:
            status = "connected"
        else:
            status = "ready"
        mock = mock_for(key)
        return IntegrationCard(
            key=cast(IntegrationKey, key),
            name=info.name,
            description=info.description,
            phase=info.phase,
            enabled=enabled,
            always_on=not info.can_disable,
            status=status,
            last_success_at=last_success,
            last_attempt_at=last_attempt,
            last_error=last_error,
            pending_items=pending,
            failed_items=failed,
            config=validate_config(key, connection.config if connection else {}),
            summary=summary,
            mock=IntegrationMock(available=mock.available) if mock else None,
        )

    # --- Writes ---------------------------------------------------------------------------

    async def update(
        self, ctx: OrgContext, key: str, data: IntegrationUpdate, rctx: RequestContext
    ) -> IntegrationCard:
        info = INFO[key]
        fields = data.model_fields_set
        async with unit_of_work(self.session):
            connection = await self.connections.ensure(
                ctx.organization_id, key, enabled=info.default_enabled, new_id=new_id()
            )
            changes: dict[str, list[Any]] = {}
            if (
                "enabled" in fields
                and data.enabled is not None
                and data.enabled != connection.enabled
            ):
                if not data.enabled and not info.can_disable:
                    raise ConflictError(
                        f"{info.name} is part of the client flow and cannot be switched off.",
                        code="INTEGRATION_ALWAYS_ON",
                    )
                changes["enabled"] = [connection.enabled, data.enabled]
                connection.enabled = data.enabled
            if "config" in fields and data.config is not None:
                before = validate_config(key, connection.config)
                after = validate_config(key, {**before, **data.config})
                if after != before:
                    changes["config"] = [before, after]
                    connection.config = after
            if changes:
                await self.session.flush()
                await record_audit(
                    self.session,
                    ctx,
                    rctx,
                    "integration.updated",
                    "integration",
                    connection.id,
                    {"integration": key, "name": info.name, "changes": changes},
                )
            result = await self.card(ctx, key)
        return result
