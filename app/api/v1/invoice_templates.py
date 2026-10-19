"""Invoice templates. HTTP only."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import OrgContextDep, require
from app.schemas.common import ApiResponse, error_responses, ok
from app.schemas.invoice_templates import (
    InvoiceTemplateCreate,
    InvoiceTemplateRead,
    InvoiceTemplateUpdate,
)
from app.services.invoice_templates import InvoiceTemplateService
from app.services.policy import OrgContext

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Invoice templates"])

SettingsManager = Annotated[OrgContext, Depends(require("settings.manage"))]


def get_invoice_template_service(session: SessionDep) -> InvoiceTemplateService:
    return InvoiceTemplateService(session)


TemplateServiceDep = Annotated[InvoiceTemplateService, Depends(get_invoice_template_service)]
TemplateIdPath = Annotated[uuid.UUID, Path(description="The invoice template's id")]


@router.get(
    "/invoice-templates",
    summary="List invoice templates",
    description=(
        "The practice's active invoice templates: the default first, then by name. Each has "
        "`client_count`, the clients that use it. `include_inactive=true` adds the inactive "
        "ones and needs `settings.manage` (403 otherwise). Bounded list, not paginated. "
        "Any member."
    ),
    responses=error_responses(403, 404),
)
async def list_invoice_templates(
    org: OrgContextDep,
    service: TemplateServiceDep,
    include_inactive: Annotated[
        bool, Query(description="Include inactive templates (needs `settings.manage`)")
    ] = False,
) -> ApiResponse[list[InvoiceTemplateRead]]:
    return ok(await service.list(org, include_inactive=include_inactive))


@router.post(
    "/invoice-templates",
    status_code=status.HTTP_201_CREATED,
    summary="Create an invoice template",
    description=(
        "`layout` is `standard`, `detailed` or `summary`; `show_logo` and `show_bank_details` "
        "choose what is printed and `footer_text` (up to 1000 characters) ends the invoice. "
        "`is_default=true` makes it the practice default and unsets the previous default in "
        "the same transaction. 409 `TEMPLATE_NAME_TAKEN` for a name a live template has "
        "(ignoring case). Invoices are still drawn with the practice letterhead and footer: "
        "templates are what a later billing release prints from. Requires `settings.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def create_invoice_template(
    body: InvoiceTemplateCreate, org: SettingsManager, service: TemplateServiceDep, ctx: ContextDep
) -> ApiResponse[InvoiceTemplateRead]:
    return ok(await service.create(org, body, ctx), "Invoice template created")


@router.get(
    "/invoice-templates/{template_id}",
    summary="Get an invoice template",
    description=(
        "One template. An inactive template answers 404 unless the caller has "
        "`settings.manage`. Any member."
    ),
    responses=error_responses(404),
)
async def get_invoice_template(
    template_id: TemplateIdPath, org: OrgContextDep, service: TemplateServiceDep
) -> ApiResponse[InvoiceTemplateRead]:
    return ok(await service.get(org, template_id))


@router.patch(
    "/invoice-templates/{template_id}",
    summary="Update an invoice template",
    description=(
        "Changes the name, layout, logo and bank-details switches, footer or `status` "
        "(`inactive` hides it from pickers; clients that use it keep it). 409 "
        "`TEMPLATE_NAME_TAKEN`; 409 `TEMPLATE_IS_DEFAULT` when deactivating the default "
        "template. Requires `settings.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def update_invoice_template(
    template_id: TemplateIdPath,
    body: InvoiceTemplateUpdate,
    org: SettingsManager,
    service: TemplateServiceDep,
    ctx: ContextDep,
) -> ApiResponse[InvoiceTemplateRead]:
    return ok(await service.update(org, template_id, body, ctx), "Invoice template updated")


@router.delete(
    "/invoice-templates/{template_id}",
    summary="Delete an invoice template",
    description=(
        "Deletes a template no client uses (its name becomes free again). 409 "
        "`TEMPLATE_IN_USE` when a client uses it (`details.clients`): deactivate it instead; "
        "409 `TEMPLATE_IS_DEFAULT` for the default template. Requires `settings.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def delete_invoice_template(
    template_id: TemplateIdPath, org: SettingsManager, service: TemplateServiceDep, ctx: ContextDep
) -> ApiResponse[None]:
    await service.delete(org, template_id, ctx)
    return ok(message="Invoice template deleted")


@router.post(
    "/invoice-templates/{template_id}/make-default",
    summary="Make an invoice template the default",
    description=(
        "Makes the template the practice default and unsets the previous default in the same "
        "transaction (there is always exactly one). 409 `TEMPLATE_INACTIVE` for an inactive "
        "template. Making the default template the default again changes nothing. "
        "Requires `settings.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def make_default_invoice_template(
    template_id: TemplateIdPath, org: SettingsManager, service: TemplateServiceDep, ctx: ContextDep
) -> ApiResponse[InvoiceTemplateRead]:
    return ok(await service.make_default(org, template_id, ctx), "Default template changed")
