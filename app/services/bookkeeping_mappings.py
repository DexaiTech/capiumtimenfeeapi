"""The bookkeeping mappings: which income account each service is booked to and which tax code each
VAT code is booked to. The editor reads the whole table (every service and VAT code, mapped or not,
plus the row for lines that have none) and writes the whole set back; a document is checked against
the mappings when it is sent (``bookkeeping_sync``), not here.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import ValidationError
from app.db.models import BookkeepingServiceMapping, BookkeepingVatMapping
from app.db.session import unit_of_work
from app.repositories.bookkeeping_sync import BookkeepingRepository
from app.schemas.bookkeeping_mappings import (
    MappingsRead,
    MappingsWrite,
    ServiceMappingRead,
    VatMappingRead,
)
from app.services import audit
from app.services.policy import OrgContext
from app.utils.ids import new_id


class MappingService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = BookkeepingRepository(session)

    async def get(self, ctx: OrgContext) -> MappingsRead:
        org_id = ctx.organization_id
        mappings = await self.repo.mappings(org_id)
        services = [
            ServiceMappingRead(
                service_id=None,
                code=None,
                name=None,
                is_active=None,
                account_code=mappings.accounts[None][0] if None in mappings.accounts else None,
                account_name=mappings.accounts[None][1] if None in mappings.accounts else None,
            )
        ]
        for service in await self.repo.services(org_id):
            account = mappings.accounts.get(service.id)
            services.append(
                ServiceMappingRead(
                    service_id=service.id,
                    code=service.code,
                    name=service.name,
                    is_active=service.is_active,
                    account_code=account[0] if account else None,
                    account_name=account[1] if account else None,
                )
            )
        none_tax = mappings.taxes.get(None)
        vat_rates = [
            VatMappingRead(
                vat_rate_id=None,
                code=None,
                name=None,
                rate_bp=None,
                is_archived=None,
                tax_code=none_tax[0] if none_tax else None,
                tax_name=none_tax[1] if none_tax else None,
            )
        ]
        for rate in await self.repo.vat_rates(org_id):
            tax = mappings.taxes.get(rate.id)
            vat_rates.append(
                VatMappingRead(
                    vat_rate_id=rate.id,
                    code=rate.code,
                    name=rate.name,
                    rate_bp=rate.rate_bp,
                    is_archived=rate.archived_at is not None,
                    tax_code=tax[0] if tax else None,
                    tax_name=tax[1] if tax else None,
                )
            )
        return MappingsRead(
            services=services,
            vat_rates=vat_rates,
            unmapped_services=sum(1 for row in services if row.account_code is None),
            unmapped_vat_rates=sum(1 for row in vat_rates if row.tax_code is None),
        )

    async def put(
        self, ctx: OrgContext, data: MappingsWrite, rctx: RequestContext
    ) -> MappingsRead:
        org_id = ctx.organization_id
        details: list[dict[str, str]] = []
        service_ids = {service.id: service for service in await self.repo.services(org_id)}
        vat_ids = {rate.id: rate for rate in await self.repo.vat_rates(org_id)}
        for index, row in enumerate(data.services):
            field = f"services.{index}.service_id"
            if row.service_id is not None and row.service_id not in service_ids:
                details.append(
                    {"field": field, "code": "unknown", "message": "This service does not exist."}
                )
        for index, vat_row in enumerate(data.vat_rates):
            field = f"vat_rates.{index}.vat_rate_id"
            if vat_row.vat_rate_id is not None and vat_row.vat_rate_id not in vat_ids:
                details.append(
                    {
                        "field": field,
                        "code": "unknown",
                        "message": "This VAT code does not exist.",
                    }
                )
        details += _duplicates(
            [r.service_id for r in data.services], "services", "service_id", "service"
        )
        details += _duplicates(
            [r.vat_rate_id for r in data.vat_rates], "vat_rates", "vat_rate_id", "VAT code"
        )
        if details:
            raise ValidationError(details=details)

        async with unit_of_work(self.session):
            before = await self.repo.mappings(org_id)
            accounts = [
                BookkeepingServiceMapping(
                    id=new_id(),
                    organization_id=org_id,
                    service_id=row.service_id,
                    account_code=row.account_code,
                    account_name=row.account_name,
                )
                for row in data.services
            ]
            taxes = [
                BookkeepingVatMapping(
                    id=new_id(),
                    organization_id=org_id,
                    vat_rate_id=vat_row.vat_rate_id,
                    tax_code=vat_row.tax_code,
                    tax_name=vat_row.tax_name,
                )
                for vat_row in data.vat_rates
            ]
            await self.repo.replace_mappings(org_id, accounts, taxes)
            changes = _changes(
                {k: v[0] for k, v in before.accounts.items()},
                {a.service_id: a.account_code for a in accounts},
                lambda key: service_ids[key].code if key in service_ids else "no service",
            )
            tax_changes = _changes(
                {k: v[0] for k, v in before.taxes.items()},
                {t.vat_rate_id: t.tax_code for t in taxes},
                lambda key: vat_ids[key].name if key in vat_ids else "no VAT code",
            )
            if changes or tax_changes:
                await audit.record(
                    self.session,
                    "integration.mappings_updated",
                    ctx=rctx,
                    actor_user_id=ctx.user.id,
                    organization_id=org_id,
                    target_type="integration",
                    target_id=None,
                    meta={
                        "integration": "bookkeeping",
                        "changes": {"services": changes, "vat_rates": tax_changes},
                    },
                )
        return await self.get(ctx)


def _duplicates(
    keys: list[uuid.UUID | None], collection: str, field: str, noun: str
) -> list[dict[str, str]]:
    seen: set[uuid.UUID | None] = set()
    found: list[dict[str, str]] = []
    for index, key in enumerate(keys):
        if key in seen:
            what = (
                f"A {noun} is mapped once"
                if key is not None
                else f"The row for no {noun} is mapped once"
            )
            found.append(
                {
                    "field": f"{collection}.{index}.{field}",
                    "code": "duplicate",
                    "message": f"{what}.",
                }
            )
        seen.add(key)
    return found


def _changes(
    before: dict[uuid.UUID | None, str], after: dict[uuid.UUID | None, str], label: Any
) -> dict[str, list[str | None]]:
    """``{name: [old code, new code]}`` for each mapping that was added, removed or changed."""
    changed: dict[str, list[str | None]] = {}
    for key in before.keys() | after.keys():
        old, new = before.get(key), after.get(key)
        if old != new:
            changed[label(key)] = [old, new]
    return changed
