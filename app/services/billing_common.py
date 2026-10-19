"""Rules shared by the billing services: VAT rate resolution, client lookup, today's date
in the practice's time zone, error codes and the audit helper."""

import uuid
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.db.models import Client, Organization
from app.repositories.billing_common import BillingReferenceRepository
from app.services import audit
from app.services.policy import OrgContext


def today_for(organization: Organization) -> date:
    """Today in the practice's time zone."""
    return local_date(organization, datetime.now(UTC))


def local_date(organization: Organization, moment: datetime) -> date:
    """The calendar day of ``moment`` in the practice's time zone."""
    try:
        zone = ZoneInfo(organization.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("Europe/London")
    return moment.astimezone(zone).date()


def now_utc() -> datetime:
    return datetime.now(UTC)


def conflict(code: str, message: str, details: Any = None) -> ConflictError:
    return ConflictError(message, code=code, details=details)


def invalid(code: str, field: str, message: str) -> ValidationError:
    return ValidationError(
        message, code=code, details=[{"field": field, "code": code.lower(), "message": message}]
    )


async def get_client_or_404(
    refs: BillingReferenceRepository, organization_id: uuid.UUID, client_id: uuid.UUID
) -> Client:
    client = await refs.get_client(organization_id, client_id)
    if client is None:
        raise NotFoundError("Client not found.")
    return client


class VatResolver:
    """The VAT rate of a line: its chosen rate, else the service's, else the client's
    default, else the practice default. Always 0 when the practice is not VAT-registered.

    Returns ``(vat_rate_id, rate_bp)``; the pair is snapshotted onto the line.
    """

    def __init__(self, refs: BillingReferenceRepository, organization: Organization) -> None:
        self.refs = refs
        self.organization = organization
        self._rates: dict[uuid.UUID, tuple[uuid.UUID, int] | None] = {}
        self._services: dict[uuid.UUID, uuid.UUID | None] = {}
        self._default: tuple[uuid.UUID | None, int] | None = None

    async def chosen(self, rate_id: uuid.UUID, field: str = "vat_rate_id") -> tuple[uuid.UUID, int]:
        """A rate picked in the request: it must exist in the practice and be live."""
        rate = await self.refs.get_vat_rate(self.organization.id, rate_id)
        if rate is None or rate.archived_at is not None:
            raise ValidationError.for_field(field, "This VAT rate does not exist.")
        return rate.id, rate.rate_bp

    async def _rate(self, rate_id: uuid.UUID) -> tuple[uuid.UUID, int] | None:
        if rate_id not in self._rates:
            rate = await self.refs.get_vat_rate(self.organization.id, rate_id)
            self._rates[rate_id] = (rate.id, rate.rate_bp) if rate else None
        return self._rates[rate_id]

    async def _service_rate(self, service_id: uuid.UUID) -> uuid.UUID | None:
        if service_id not in self._services:
            service = await self.refs.get_service(self.organization.id, service_id)
            self._services[service_id] = service.vat_rate_id if service else None
        return self._services[service_id]

    async def default(self) -> tuple[uuid.UUID | None, int]:
        if self._default is None:
            rate = await self.refs.default_vat_rate(self.organization.id)
            self._default = (rate.id, rate.rate_bp) if rate else (None, 0)
        return self._default

    async def resolve(
        self,
        *,
        client: Client,
        line_rate_id: uuid.UUID | None = None,
        service_id: uuid.UUID | None = None,
        validate_line_rate: bool = False,
    ) -> tuple[uuid.UUID | None, int]:
        if not self.organization.vat_registered:
            return None, 0
        if line_rate_id is not None:
            if validate_line_rate:
                return await self.chosen(line_rate_id)
            found = await self._rate(line_rate_id)
            if found:
                return found
        if service_id is not None:
            service_rate = await self._service_rate(service_id)
            if service_rate is not None and (found := await self._rate(service_rate)):
                return found
        if client.default_vat_rate_id is not None and (
            found := await self._rate(client.default_vat_rate_id)
        ):
            return found
        return await self.default()


async def record(
    session: AsyncSession,
    ctx: OrgContext,
    rctx: RequestContext | None,
    action: str,
    *,
    target_type: str,
    target_id: uuid.UUID,
    meta: dict[str, Any],
) -> None:
    await audit.record(
        session,
        action,
        ctx=rctx,
        actor_user_id=ctx.user.id,
        organization_id=ctx.organization_id,
        target_type=target_type,
        target_id=target_id,
        meta=meta,
    )
