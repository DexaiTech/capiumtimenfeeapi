"""Clients, their contacts and the client overview. HTTP only."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import PageParamsDep, require
from app.schemas.clients import (
    AccountingSoftware,
    AmlStatus,
    CentralFilter,
    ClientCreate,
    ClientDetail,
    ClientFilters,
    ClientListItem,
    ClientServiceCreate,
    ClientServiceRead,
    ClientServiceUpdate,
    ClientSort,
    ClientStatus,
    ClientSummary,
    ClientType,
    ClientUpdate,
    ContactCreate,
    ContactRead,
    ContactUpdate,
    ImportResult,
)
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.services.client_import import (
    MAX_UPLOAD_BODY_BYTES,
    ClientImportService,
    ImportTooLargeError,
)
from app.services.clients import ClientsService
from app.services.policy import OrgContext

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Clients"])

ClientReader = Annotated[OrgContext, Depends(require("client.read"))]
ClientCreator = Annotated[OrgContext, Depends(require("client.create"))]
ClientEditor = Annotated[OrgContext, Depends(require("client.update"))]
ClientDeleter = Annotated[OrgContext, Depends(require("client.delete"))]


def get_clients_service(session: SessionDep) -> ClientsService:
    return ClientsService(session)


def get_import_service(session: SessionDep) -> ClientImportService:
    return ClientImportService(session)


ClientsServiceDep = Annotated[ClientsService, Depends(get_clients_service)]
ImportServiceDep = Annotated[ClientImportService, Depends(get_import_service)]


def client_filters(
    q: Annotated[
        str | None,
        Query(max_length=200, description="Search name, code, company number and emails"),
    ] = None,
    status: Annotated[
        list[ClientStatus] | None,
        Query(description="Repeat for several. Default: every status except archived"),
    ] = None,
    segment: Annotated[
        list[Annotated[str, Query(max_length=40)]] | None,
        Query(description="Segment (case-insensitive); repeat for several"),
    ] = None,
    client_type: Annotated[list[ClientType] | None, Query(description="Repeatable")] = None,
    aml_status: Annotated[list[AmlStatus] | None, Query(description="Repeatable")] = None,
    accounting_software: Annotated[
        list[AccountingSoftware] | None, Query(description="Repeatable")
    ] = None,
    service_id: Annotated[
        uuid.UUID | None, Query(description="Clients with an active engagement of this service")
    ] = None,
    manager_user_id: Annotated[uuid.UUID | None, Query()] = None,
    partner_user_id: Annotated[uuid.UUID | None, Query()] = None,
    central: Annotated[
        CentralFilter | None,
        Query(
            description=(
                "`linked`: clients enabled from the central client service; `local`: clients "
                "created in this practice"
            )
        ),
    ] = None,
) -> ClientFilters:
    return ClientFilters(
        q=q.strip() or None if q else None,
        statuses=tuple(status or ()),
        segments=tuple(segment or ()),
        client_types=tuple(client_type or ()),
        aml_statuses=tuple(aml_status or ()),
        accounting_software=tuple(accounting_software or ()),
        service_id=service_id,
        manager_user_id=manager_user_id,
        partner_user_id=partner_user_id,
        central=central,
    )


FiltersDep = Annotated[ClientFilters, Depends(client_filters)]
SortQuery = Annotated[ClientSort, Query(description="Sort key")]
OrderQuery = Annotated[Literal["asc", "desc"], Query(description="Sort direction")]


async def _read_body(request: Request) -> bytes:
    """The raw request body, refusing more than the upload cap (the global body-size
    middleware exempts ``/imports/...``)."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_UPLOAD_BODY_BYTES:
        raise ImportTooLargeError()
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_UPLOAD_BODY_BYTES:
            raise ImportTooLargeError()
    return bytes(body)


# --- List, summary, export, import -------------------------------------------------------


@router.get(
    "/clients/summary",
    summary="Client list header counts",
    description=(
        "Counts for the client list header: `total` (live clients that are not archived), "
        "by status (archived included), by client type, AML status, accounting software "
        "and segment, by origin (`by_central`: `linked` to the central client service or "
        "`local`), clients with overdue deadlines, and the number of active staff "
        "(`users`). Requires `client.read`."
    ),
    responses=error_responses(403, 404),
)
async def client_summary(
    org: ClientReader, service: ClientsServiceDep
) -> ApiResponse[ClientSummary]:
    return ok(await service.summary(org))


@router.get(
    "/clients/export",
    summary="Export clients as CSV",
    description=(
        "Every client matching the list filters, in the list's sort order, as CSV (UTF-8 "
        "with BOM). Text cells that start with `=`, `+`, `-` or `@` are prefixed with `'`. "
        "Requires `client.read`."
    ),
    responses={
        **error_responses(403, 404),
        200: {"content": {"text/csv": {}}, "description": "CSV file"},
    },
    response_class=Response,
)
async def export_clients(
    org: ClientReader,
    service: ClientsServiceDep,
    filters: FiltersDep,
    format: Annotated[Literal["csv"], Query(description="Only csv")] = "csv",
    sort: SortQuery = "name",
    order: OrderQuery = "asc",
) -> Response:
    content = await service.export_csv(org, filters, sort=sort, descending=order == "desc")
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="clients.csv"'},
    )


@router.post(
    "/imports/clients",
    summary="Import clients from CSV",
    description=(
        "Upload a CSV (multipart/form-data field `file`, or a `text/csv` body; at most 5 MB "
        "and 5000 rows). The header row is mapped by name: the client fields of "
        "`POST /clients` plus `year_end` (DD/MM), `contact_name`, `contact_email`, "
        "`contact_phone`, `contact_position` (creates the primary contact), `services` "
        "(service codes separated by `;`), `manager_email` and `partner_email`; aliases "
        "such as `Client name`, `Reg No` or `Contact` work and unknown columns are "
        "ignored. `dry_run=true` validates and returns per-row errors without writing. A "
        "real run is all-or-nothing: 422 `IMPORT_INVALID` (errors in `details`) or every "
        "client is created. Codes left blank come from the client sequence. 413 "
        "`FILE_TOO_LARGE`, 422 `IMPORT_TOO_MANY_ROWS` / `IMPORT_MISSING_COLUMNS`, 400 "
        "`FILE_INVALID`. Requires `client.create`."
    ),
    responses=error_responses(400, 403, 404, 409, 413, 415),
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {"file": {"type": "string", "format": "binary"}},
                        "required": ["file"],
                    }
                },
                "text/csv": {"schema": {"type": "string"}},
            },
        }
    },
)
async def import_clients(
    request: Request,
    org: ClientCreator,
    service: ImportServiceDep,
    ctx: ContextDep,
    dry_run: Annotated[bool, Query(description="Validate only; write nothing")] = False,
) -> ApiResponse[ImportResult]:
    body = await _read_body(request)
    result = await service.run(
        org, request.headers.get("content-type", ""), body, dry_run=dry_run, rctx=ctx
    )
    message = "Import checked" if dry_run else f"{result.created} client(s) imported"
    return ok(result, message)


@router.get(
    "/clients",
    summary="List clients",
    description=(
        "The client list: every client field plus the derived columns - `initials`, "
        "`primary_contact`/`contact_name`, `contact_email` (primary contact's, else the "
        "client's), `deadlines` (open jobs' due dates against today in the practice time "
        "zone: overdue/due today/due within 14 days, worst `status`, `next_due_date`), "
        "`last_activity_at` (latest change to the client, its jobs, time or invoices) and "
        "the active `services`. Filters: `q` (name, code, company number, client or "
        "contact email; case-insensitive), `status` (default: all but archived), "
        "`segment`, `client_type`, `aml_status`, `accounting_software`, `service_id`, "
        "`manager_user_id`, `partner_user_id`, `central` (`linked` or `local`). Sorted by "
        "`sort`/`order`, keyset-paginated "
        "(the cursor encodes the sort key; a cursor from another sort answers 422). "
        "Requires `client.read`."
    ),
    responses=error_responses(403, 404),
)
async def list_clients(
    org: ClientReader,
    service: ClientsServiceDep,
    filters: FiltersDep,
    page: PageParamsDep,
    sort: SortQuery = "name",
    order: OrderQuery = "asc",
) -> ApiResponse[list[ClientListItem]]:
    items, next_cursor = await service.list_clients(
        org, filters, sort=sort, descending=order == "desc", limit=page.limit, cursor=page.cursor
    )
    return ok_page(items, next_cursor)


@router.post(
    "/clients",
    status_code=status.HTTP_201_CREATED,
    summary="Create a client",
    description=(
        "Creates a client, optionally with `contacts` (the first becomes primary unless "
        "one is marked). `code` is upper-cased; when omitted it comes from the client "
        "sequence. 409 `CLIENT_CODE_TAKEN`; 422 for a partner/manager who is not an active "
        "member or a VAT rate that is not live; billing settings: `invoice_email`, "
        "`default_service_id` and `invoice_template_id` (404 for an id that is not the "
        "practice's, 422 for an inactive service or template). Creating an archived client "
        "also needs `client.delete`. Returns the client overview. Requires `client.create`."
    ),
    responses=error_responses(403, 404, 409),
)
async def create_client(
    body: ClientCreate, org: ClientCreator, service: ClientsServiceDep, ctx: ContextDep
) -> ApiResponse[ClientDetail]:
    return ok(await service.create(org, body, ctx), "Client created")


@router.get(
    "/clients/{client_id}",
    summary="Get a client overview",
    description=(
        "The client with its list columns, contacts, engagements, client rates (null "
        "without `rate.read`), financial snapshot (WIP figures need `wip.read`, balances "
        "`invoice.read`, on-account `payment.read`; otherwise null) and annualised "
        "recurring fees. Requires `client.read`."
    ),
    responses=error_responses(403, 404),
)
async def get_client(
    client_id: uuid.UUID, org: ClientReader, service: ClientsServiceDep
) -> ApiResponse[ClientDetail]:
    return ok(await service.get_detail(org, client_id))


@router.patch(
    "/clients/{client_id}",
    summary="Update a client",
    description=(
        "Changes any client field. Any status transition is allowed, but moving into or "
        "out of `archived` also needs `client.delete` (403). 409 `CLIENT_CODE_TAKEN`. "
        "A client linked to the central client service (`central_client_id`) has its name "
        "and address managed there: changing `name`, `address_line1`, `address_line2`, "
        "`city`, `postcode` or `country` answers 409 `CLIENT_FIELD_MANAGED_CENTRALLY` "
        "(`details` names the fields) until the client is unlinked. The billing settings "
        "`invoice_email`, `default_service_id` and `invoice_template_id` follow the rules "
        "of `POST /clients`. Requires `client.update`."
    ),
    responses=error_responses(403, 404, 409),
)
async def update_client(
    client_id: uuid.UUID,
    body: ClientUpdate,
    org: ClientEditor,
    service: ClientsServiceDep,
    ctx: ContextDep,
) -> ApiResponse[ClientDetail]:
    return ok(await service.update(org, client_id, body, ctx), "Client updated")


@router.post(
    "/clients/{client_id}/archive",
    summary="Archive a client",
    description=(
        "Sets the status to `archived`: hidden from the default list and pickers; no new "
        "jobs, time or disbursements (409 `CLIENT_INACTIVE` elsewhere); billing existing "
        "WIP stays possible. Requires `client.delete`."
    ),
    responses=error_responses(403, 404),
)
async def archive_client(
    client_id: uuid.UUID, org: ClientDeleter, service: ClientsServiceDep, ctx: ContextDep
) -> ApiResponse[ClientDetail]:
    return ok(await service.archive(org, client_id, ctx), "Client archived")


@router.delete(
    "/clients/{client_id}",
    summary="Delete a client",
    description=(
        "Soft-deletes a client nothing refers to; its code becomes free again. A client "
        "with jobs, time, disbursements, expenses, invoices, credit notes or payments answers "
        "409 `CLIENT_IN_USE` (counts in `details`) - archive it instead. "
        "Requires `client.delete`."
    ),
    responses=error_responses(403, 404, 409),
)
async def delete_client(
    client_id: uuid.UUID, org: ClientDeleter, service: ClientsServiceDep, ctx: ContextDep
) -> ApiResponse[None]:
    await service.delete(org, client_id, ctx)
    return ok(message="Client deleted")


# --- Contacts ---------------------------------------------------------------------------


@router.get(
    "/clients/{client_id}/contacts",
    summary="List a client's contacts",
    description="Primary contact first, then by name. Bounded list. Requires `client.read`.",
    responses=error_responses(403, 404),
)
async def list_contacts(
    client_id: uuid.UUID, org: ClientReader, service: ClientsServiceDep
) -> ApiResponse[list[ContactRead]]:
    return ok([ContactRead.from_model(c) for c in await service.list_contacts(org, client_id)])


@router.post(
    "/clients/{client_id}/contacts",
    status_code=status.HTTP_201_CREATED,
    summary="Add a contact",
    description=(
        "Adds a contact. `is_primary=true` makes it the primary contact and unsets the "
        "previous one in the same transaction; left out, the client's first contact "
        "becomes primary. Requires `client.update`."
    ),
    responses=error_responses(403, 404),
)
async def create_contact(
    client_id: uuid.UUID,
    body: ContactCreate,
    org: ClientEditor,
    service: ClientsServiceDep,
    ctx: ContextDep,
) -> ApiResponse[ContactRead]:
    contact = await service.create_contact(org, client_id, body, ctx)
    return ok(ContactRead.from_model(contact), "Contact added")


@router.patch(
    "/clients/{client_id}/contacts/{contact_id}",
    summary="Update a contact",
    description=(
        "Changes a contact; `is_primary=true` moves the primary flag to it. "
        "Requires `client.update`."
    ),
    responses=error_responses(403, 404),
)
async def update_contact(
    client_id: uuid.UUID,
    contact_id: uuid.UUID,
    body: ContactUpdate,
    org: ClientEditor,
    service: ClientsServiceDep,
    ctx: ContextDep,
) -> ApiResponse[ContactRead]:
    contact = await service.update_contact(org, client_id, contact_id, body, ctx)
    return ok(ContactRead.from_model(contact), "Contact updated")


@router.delete(
    "/clients/{client_id}/contacts/{contact_id}",
    summary="Delete a contact",
    description=(
        "Deletes the contact. Deleting the primary contact leaves the client without one. "
        "Requires `client.update`."
    ),
    responses=error_responses(403, 404),
)
async def delete_contact(
    client_id: uuid.UUID,
    contact_id: uuid.UUID,
    org: ClientEditor,
    service: ClientsServiceDep,
    ctx: ContextDep,
) -> ApiResponse[None]:
    await service.delete_contact(org, client_id, contact_id, ctx)
    return ok(message="Contact deleted")


# --- Engagements ------------------------------------------------------------------------


@router.get(
    "/clients/{client_id}/services",
    summary="List a client's services",
    description=(
        "The services the client takes (engagements): active first, in service order, "
        "with the agreed fee and the effective fee (agreed, else the service default). "
        "Bounded list. Requires `client.read`."
    ),
    responses=error_responses(403, 404),
)
async def list_engagements(
    client_id: uuid.UUID, org: ClientReader, service: ClientsServiceDep
) -> ApiResponse[list[ClientServiceRead]]:
    rows = await service.list_engagements(org, client_id)
    return ok([ClientServiceRead.from_model(e, s) for e, s in rows])


@router.post(
    "/clients/{client_id}/services",
    status_code=status.HTTP_201_CREATED,
    summary="Add a service to a client",
    description=(
        "Records that the client takes an active service of the practice (422 otherwise), "
        "with an agreed `fee` (null: the service default), `frequency` and dates. One per "
        "service: 409 `CLIENT_SERVICE_EXISTS`. Requires `client.update`."
    ),
    responses=error_responses(403, 404, 409),
)
async def create_engagement(
    client_id: uuid.UUID,
    body: ClientServiceCreate,
    org: ClientEditor,
    service: ClientsServiceDep,
    ctx: ContextDep,
) -> ApiResponse[ClientServiceRead]:
    engagement, svc = await service.create_engagement(org, client_id, body, ctx)
    return ok(ClientServiceRead.from_model(engagement, svc), "Service added")


@router.patch(
    "/clients/{client_id}/services/{engagement_id}",
    summary="Update a client's service",
    description="Changes fee, frequency, dates or active flag. Requires `client.update`.",
    responses=error_responses(403, 404),
)
async def update_engagement(
    client_id: uuid.UUID,
    engagement_id: uuid.UUID,
    body: ClientServiceUpdate,
    org: ClientEditor,
    service: ClientsServiceDep,
    ctx: ContextDep,
) -> ApiResponse[ClientServiceRead]:
    engagement, svc = await service.update_engagement(org, client_id, engagement_id, body, ctx)
    return ok(ClientServiceRead.from_model(engagement, svc), "Service updated")


@router.delete(
    "/clients/{client_id}/services/{engagement_id}",
    summary="Remove a service from a client",
    description="Deletes the engagement. Requires `client.update`.",
    responses=error_responses(403, 404),
)
async def delete_engagement(
    client_id: uuid.UUID,
    engagement_id: uuid.UUID,
    org: ClientEditor,
    service: ClientsServiceDep,
    ctx: ContextDep,
) -> ApiResponse[None]:
    await service.delete_engagement(org, client_id, engagement_id, ctx)
    return ok(message="Service removed")
