"""Leads: prospective clients, what winning them cost, and their conversion into clients.

* ``acquisition_cost`` and ``onboarding_cost`` are pence; both stay editable after conversion
  (onboarding is often costed once the client is in).
* Converting is one step: a client is created through ``ClientsService.create`` (same
  transaction, same validation, same ``client.created`` entry) from the lead's name, type,
  email, phone and notes, with a primary contact from ``contact_name``. A lead converts once
  (409 ``LEAD_ALREADY_CONVERTED``); an archived lead must be unarchived first (409
  ``LEAD_ARCHIVED``). The lead row is locked, so two clicks make one client.
* Archiving hides a lead from the open (or converted) list; unarchiving brings it back.
* Permissions follow the clients: reading ``client.read``, adding and converting
  ``client.create``, editing ``client.update``, archiving and unarchiving ``client.delete``.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import ConflictError, NotFoundError
from app.db.models import Lead
from app.db.session import unit_of_work
from app.repositories.leads import LeadRepository
from app.schemas.clients import ClientCreate
from app.schemas.leads import (
    LeadConvert,
    LeadCreate,
    LeadRead,
    LeadStatus,
    LeadSummary,
    LeadUpdate,
)
from app.services.clients import ClientsService
from app.services.common import apply_changes, record_audit
from app.services.policy import OrgContext
from app.utils.pagination import Page, PageParams


class LeadService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.leads = LeadRepository(session)

    async def _reads(self, ctx: OrgContext, leads: list[Lead]) -> list[LeadRead]:
        clients = await self.leads.live_clients(
            ctx.organization_id,
            {lead.converted_client_id for lead in leads if lead.converted_client_id is not None},
        )
        return [
            LeadRead.from_model(
                lead, clients.get(lead.converted_client_id) if lead.converted_client_id else None
            )
            for lead in leads
        ]

    async def _read(self, ctx: OrgContext, lead: Lead) -> LeadRead:
        [read] = await self._reads(ctx, [lead])
        return read

    async def _lead(self, ctx: OrgContext, lead_id: uuid.UUID, *, lock: bool = False) -> Lead:
        lead = await self.leads.get(ctx.organization_id, lead_id, lock=lock)
        if lead is None:
            raise NotFoundError("Lead not found.")
        return lead

    # --- Reads ----------------------------------------------------------------------------

    async def list_leads(
        self,
        ctx: OrgContext,
        params: PageParams,
        *,
        status: LeadStatus = "open",
        q: str | None = None,
    ) -> Page[LeadRead]:
        page = await self.leads.list_page(ctx.organization_id, params, status=status, q=q)
        return Page(items=await self._reads(ctx, page.items), next_cursor=page.next_cursor)

    async def summary(self, ctx: OrgContext) -> LeadSummary:
        return LeadSummary(**await self.leads.totals(ctx.organization_id))

    async def get(self, ctx: OrgContext, lead_id: uuid.UUID) -> LeadRead:
        return await self._read(ctx, await self._lead(ctx, lead_id))

    # --- Writes ---------------------------------------------------------------------------

    async def create(self, ctx: OrgContext, data: LeadCreate, rctx: RequestContext) -> LeadRead:
        async with unit_of_work(self.session):
            lead = await self.leads.add(
                Lead(
                    organization_id=ctx.organization_id,
                    created_by=ctx.user.id,
                    **data.model_dump(),
                )
            )
            await record_audit(
                self.session, ctx, rctx, "lead.created", "lead", lead.id,
                {
                    "name": lead.name,
                    "acquisition_cost": lead.acquisition_cost,
                    "onboarding_cost": lead.onboarding_cost,
                },
            )  # fmt: skip
            result = await self._read(ctx, lead)
        return result

    async def update(
        self, ctx: OrgContext, lead_id: uuid.UUID, data: LeadUpdate, rctx: RequestContext
    ) -> LeadRead:
        values = data.model_dump(exclude_unset=True)
        async with unit_of_work(self.session):
            lead = await self._lead(ctx, lead_id, lock=True)
            changes = apply_changes(lead, values)
            lead = await self.leads.save(lead)
            if changes:
                await record_audit(
                    self.session, ctx, rctx, "lead.updated", "lead", lead.id,
                    {"name": lead.name, "changes": changes},
                )  # fmt: skip
            result = await self._read(ctx, lead)
        return result

    async def set_archived(
        self, ctx: OrgContext, lead_id: uuid.UUID, archived: bool, rctx: RequestContext
    ) -> LeadRead:
        """Archive or unarchive; doing it twice changes nothing and writes no audit entry."""
        async with unit_of_work(self.session):
            lead = await self._lead(ctx, lead_id, lock=True)
            if (lead.archived_at is not None) != archived:
                lead.archived_at = datetime.now(UTC) if archived else None
                lead = await self.leads.save(lead)
                action = "lead.archived" if archived else "lead.unarchived"
                await record_audit(
                    self.session, ctx, rctx, action, "lead", lead.id, {"name": lead.name}
                )
            result = await self._read(ctx, lead)
        return result

    async def convert(
        self, ctx: OrgContext, lead_id: uuid.UUID, data: LeadConvert, rctx: RequestContext
    ) -> LeadRead:
        """Create an active client from the lead and link the two."""
        async with unit_of_work(self.session):
            lead = await self._lead(ctx, lead_id, lock=True)
            if lead.converted_client_id is not None:
                raise ConflictError(
                    "This lead has already been converted to a client.",
                    code="LEAD_ALREADY_CONVERTED",
                    details={"client_id": str(lead.converted_client_id)},
                )
            if lead.archived_at is not None:
                raise ConflictError(
                    "This lead is archived. Unarchive it to convert it to a client.",
                    code="LEAD_ARCHIVED",
                )
            contact = {"name": lead.contact_name, "email": lead.email, "phone": lead.phone}
            values = {
                "code": data.code,
                "name": lead.name,
                "client_type": lead.client_type,
                "status": "active",
                "email": lead.email,
                "phone": lead.phone,
                "notes": lead.notes,
                "contacts": [contact] if lead.contact_name else [],
            }
            client = await ClientsService(self.session).create(
                ctx, ClientCreate.model_validate(values), rctx
            )
            lead.converted_client_id = client.id
            lead.converted_at = datetime.now(UTC)
            lead = await self.leads.save(lead)
            # ``client_id`` puts the entry in the client's history as well.
            await record_audit(
                self.session, ctx, rctx, "lead.converted", "lead", lead.id,
                {"name": lead.name, "client_id": str(client.id), "client_code": client.code},
            )  # fmt: skip
            result = await self._read(ctx, lead)
        return result
