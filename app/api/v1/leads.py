"""Leads: prospective clients and their conversion into clients. HTTP only."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, status

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import PageParamsDep, require
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.schemas.leads import (
    LeadConvert,
    LeadCreate,
    LeadRead,
    LeadStatus,
    LeadSummary,
    LeadUpdate,
)
from app.services.leads import LeadService
from app.services.policy import OrgContext

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Leads"])

LeadReader = Annotated[OrgContext, Depends(require("client.read"))]
LeadCreator = Annotated[OrgContext, Depends(require("client.create"))]
LeadEditor = Annotated[OrgContext, Depends(require("client.update"))]
LeadArchiver = Annotated[OrgContext, Depends(require("client.delete"))]


def get_lead_service(session: SessionDep) -> LeadService:
    return LeadService(session)


LeadServiceDep = Annotated[LeadService, Depends(get_lead_service)]


@router.get(
    "/leads",
    summary="List leads",
    description=(
        "The practice's leads of one `status` (`open`, the default; `converted`; `archived`, "
        "which wins over converted), newest first. `q` searches the name, the contact name and "
        "the email. Cursor-paginated (`limit`, `cursor`, `meta.next_cursor`). Requires "
        "`client.read`."
    ),
    responses=error_responses(403, 404),
)
async def list_leads(
    org: LeadReader,
    service: LeadServiceDep,
    params: PageParamsDep,
    status: Annotated[LeadStatus, Query(description="Which leads")] = "open",
    q: Annotated[
        str | None, Query(max_length=200, description="Search name, contact name and email")
    ] = None,
) -> ApiResponse[list[LeadRead]]:
    page = await service.list_leads(
        org, params, status=status, q=q.strip() or None if q else None
    )
    return ok_page(page.items, page.next_cursor)


@router.get(
    "/leads/summary",
    summary="Lead totals",
    description=(
        "For each status (`open`, `converted`, `archived`): how many leads and their "
        "acquisition, onboarding and total cost in pence. Requires `client.read`."
    ),
    responses=error_responses(403, 404),
)
async def lead_summary(org: LeadReader, service: LeadServiceDep) -> ApiResponse[LeadSummary]:
    return ok(await service.summary(org))


@router.post(
    "/leads",
    status_code=status.HTTP_201_CREATED,
    summary="Add a lead",
    description=(
        "`name` (1-200) is required; `acquisition_cost` and `onboarding_cost` are pence (0 "
        "and up, default 0). `contact_name`, `email` and `phone` become the client's primary "
        "contact on conversion. Requires `client.create`."
    ),
    responses=error_responses(403, 404),
)
async def create_lead(
    body: LeadCreate, org: LeadCreator, service: LeadServiceDep, ctx: ContextDep
) -> ApiResponse[LeadRead]:
    return ok(await service.create(org, body, ctx), "Lead added")


@router.get(
    "/leads/{lead_id}",
    summary="Get a lead",
    description="One lead, with the client it became once converted. Requires `client.read`.",
    responses=error_responses(403, 404),
)
async def get_lead(
    lead_id: uuid.UUID, org: LeadReader, service: LeadServiceDep
) -> ApiResponse[LeadRead]:
    return ok(await service.get(org, lead_id))


@router.patch(
    "/leads/{lead_id}",
    summary="Update a lead",
    description=(
        "Changes any field; the text fields accept null to clear them. The costs stay "
        "editable after conversion (onboarding is often costed later). Requires "
        "`client.update`."
    ),
    responses=error_responses(403, 404),
)
async def update_lead(
    lead_id: uuid.UUID,
    body: LeadUpdate,
    org: LeadEditor,
    service: LeadServiceDep,
    ctx: ContextDep,
) -> ApiResponse[LeadRead]:
    return ok(await service.update(org, lead_id, body, ctx), "Lead updated")


@router.post(
    "/leads/{lead_id}/archive",
    summary="Archive a lead",
    description=(
        "Hides the lead from the open and converted lists (a converted client is not "
        "touched). Archiving an archived lead changes nothing. Requires `client.delete`."
    ),
    responses=error_responses(403, 404),
)
async def archive_lead(
    lead_id: uuid.UUID, org: LeadArchiver, service: LeadServiceDep, ctx: ContextDep
) -> ApiResponse[LeadRead]:
    return ok(await service.set_archived(org, lead_id, True, ctx), "Lead archived")


@router.post(
    "/leads/{lead_id}/unarchive",
    summary="Unarchive a lead",
    description=(
        "Brings an archived lead back to the open (or converted) list. Requires "
        "`client.delete`."
    ),
    responses=error_responses(403, 404),
)
async def unarchive_lead(
    lead_id: uuid.UUID, org: LeadArchiver, service: LeadServiceDep, ctx: ContextDep
) -> ApiResponse[LeadRead]:
    return ok(await service.set_archived(org, lead_id, False, ctx), "Lead unarchived")


@router.post(
    "/leads/{lead_id}/convert",
    summary="Convert a lead to a client",
    description=(
        "Creates an active client from the lead (name, type, email, phone, notes, and a "
        "primary contact from `contact_name`) and links it: `converted_client` in the "
        "response. `code` is optional (generated from the client sequence; 409 "
        "`CLIENT_CODE_TAKEN` when used). 409 `LEAD_ALREADY_CONVERTED` for a second "
        "conversion, 409 `LEAD_ARCHIVED` for an archived lead. Requires `client.create`."
    ),
    responses=error_responses(403, 404, 409),
)
async def convert_lead(
    lead_id: uuid.UUID,
    org: LeadCreator,
    service: LeadServiceDep,
    ctx: ContextDep,
    body: Annotated[LeadConvert | None, Body()] = None,
) -> ApiResponse[LeadRead]:
    return ok(
        await service.convert(org, lead_id, body or LeadConvert(), ctx),
        "Lead converted to a client",
    )
