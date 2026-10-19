"""Ticket tag routes (the labels of Application Testing). HTTP only."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import require
from app.schemas.common import ApiResponse, error_responses, ok
from app.schemas.tickets import TagCreate, TagDeleted, TagRead, TagUpdate
from app.services.policy import OrgContext
from app.services.ticket_tags import TicketTagService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Ticket tags"])

TagViewer = Annotated[OrgContext, Depends(require("ticket.read"))]
TagEditor = Annotated[OrgContext, Depends(require("ticket.update"))]
TagDeleter = Annotated[OrgContext, Depends(require("ticket.delete"))]


def get_tag_service(session: SessionDep) -> TicketTagService:
    return TicketTagService(session)


TagServiceDep = Annotated[TicketTagService, Depends(get_tag_service)]


@router.get(
    "/ticket-tags",
    summary="List ticket tags",
    description=(
        "Every tag of the practice (at most 200) by name, each with `ticket_count`, the "
        "tickets that carry it. Not paginated. Requires `ticket.read`."
    ),
    responses=error_responses(403, 404),
)
async def list_tags(org: TagViewer, service: TagServiceDep) -> ApiResponse[list[TagRead]]:
    return ok(await service.list_tags(org))


@router.post(
    "/ticket-tags",
    status_code=status.HTTP_201_CREATED,
    summary="Create a ticket tag",
    description=(
        "`name` is 1-40 characters and unique per practice ignoring case (409 "
        "`TAG_NAME_TAKEN`); `color` is `violet`, `blue`, `green`, `red`, `orange` or `grey` "
        "(default). At most 200 tags (409 `TAG_LIMIT_REACHED`). Requires `ticket.update`."
    ),
    responses=error_responses(403, 404, 409),
)
async def create_tag(
    body: TagCreate, org: TagEditor, service: TagServiceDep, ctx: ContextDep
) -> ApiResponse[TagRead]:
    return ok(await service.create(org, body, ctx), "Tag created")


@router.patch(
    "/ticket-tags/{tag_id}",
    summary="Update a ticket tag",
    description=(
        "Renames or recolours the tag; the tickets that carry it show the new name. 409 "
        "`TAG_NAME_TAKEN` for a duplicate name. Requires `ticket.update`."
    ),
    responses=error_responses(403, 404, 409),
)
async def update_tag(
    tag_id: uuid.UUID, body: TagUpdate, org: TagEditor, service: TagServiceDep, ctx: ContextDep
) -> ApiResponse[TagRead]:
    return ok(await service.update(org, tag_id, body, ctx), "Tag updated")


@router.delete(
    "/ticket-tags/{tag_id}",
    summary="Delete a ticket tag",
    description=(
        "Deletes an unused tag. A tag that tickets carry answers 409 `TAG_IN_USE` (with "
        "`details.ticket_count`) until the caller confirms with `force=true`, which takes the "
        "tag off those tickets and deletes it. Requires `ticket.delete`."
    ),
    responses=error_responses(403, 404, 409),
)
async def delete_tag(
    tag_id: uuid.UUID,
    org: TagDeleter,
    service: TagServiceDep,
    ctx: ContextDep,
    force: Annotated[bool, Query(description="Delete the tag although tickets carry it")] = False,
) -> ApiResponse[TagDeleted]:
    return ok(await service.delete(org, tag_id, force=force, rctx=ctx), "Tag deleted")
