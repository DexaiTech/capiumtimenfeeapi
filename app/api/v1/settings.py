"""Practice settings: VAT rates, services, activity codes and number sequences. HTTP only."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import OrgContextDep, require
from app.schemas.common import ApiResponse, error_responses, ok
from app.schemas.settings import (
    ActivityCodeCreate,
    ActivityCodeRead,
    ActivityCodeUpdate,
    DeleteOutcome,
    SequenceKind,
    SequenceRead,
    SequenceUpdate,
    ServiceActivityCodesRead,
    ServiceActivityCodesUpdate,
    ServiceCreate,
    ServiceRead,
    ServiceUpdate,
    VatRateCreate,
    VatRateRead,
    VatRateUpdate,
)
from app.services.policy import OrgContext
from app.services.settings import SettingsService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Settings"])

SettingsManager = Annotated[OrgContext, Depends(require("settings.manage"))]
NumberingManager = Annotated[OrgContext, Depends(require("organization.update"))]


def get_settings_service(session: SessionDep) -> SettingsService:
    return SettingsService(session)


SettingsServiceDep = Annotated[SettingsService, Depends(get_settings_service)]
_DELETE_MESSAGES = {
    "deleted": "{} deleted",
    "archived": "{} is in use, so it was archived",
    "deactivated": "{} is in use, so it was deactivated",
}


def _deleted(outcome: DeleteOutcome, what: str) -> ApiResponse[DeleteOutcome]:
    return ok(outcome, _DELETE_MESSAGES[outcome.outcome].format(what))


# --- VAT rates --------------------------------------------------------------------------


@router.get(
    "/vat-rates",
    summary="List VAT rates",
    description=(
        "Live VAT rates, highest rate first; `include_archived=true` adds archived ones "
        "(listed last). Bounded list, not paginated. Any member."
    ),
    responses=error_responses(404),
)
async def list_vat_rates(
    org: OrgContextDep,
    service: SettingsServiceDep,
    include_archived: Annotated[bool, Query(description="Include archived rates")] = False,
) -> ApiResponse[list[VatRateRead]]:
    rates = await service.list_vat_rates(org, include_archived=include_archived)
    return ok([VatRateRead.from_model(r) for r in rates])


@router.get(
    "/vat-rates/{rate_id}",
    summary="Get a VAT rate",
    description="One VAT rate, archived or not. Any member.",
    responses=error_responses(404),
)
async def get_vat_rate(
    rate_id: uuid.UUID, org: OrgContextDep, service: SettingsServiceDep
) -> ApiResponse[VatRateRead]:
    return ok(VatRateRead.from_model(await service.get_vat_rate(org, rate_id)))


@router.post(
    "/vat-rates",
    status_code=status.HTTP_201_CREATED,
    summary="Create a VAT rate",
    description=(
        "`rate_bp` is basis points (2000 = 20%). `is_default=true` makes it the practice "
        "default and unsets the previous default in the same transaction. 409 "
        "`VAT_RATE_NAME_TAKEN` for a duplicate live name. Requires `settings.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def create_vat_rate(
    body: VatRateCreate, org: SettingsManager, service: SettingsServiceDep, ctx: ContextDep
) -> ApiResponse[VatRateRead]:
    return ok(
        VatRateRead.from_model(await service.create_vat_rate(org, body, ctx)), "VAT rate created"
    )


@router.patch(
    "/vat-rates/{rate_id}",
    summary="Update a VAT rate",
    description=(
        "Changes name, code, rate, default flag or archive state (`is_archived`). Existing "
        "invoice and credit-note lines keep their snapshotted rate. An archived rate cannot "
        "be the default (422). Requires `settings.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def update_vat_rate(
    rate_id: uuid.UUID,
    body: VatRateUpdate,
    org: SettingsManager,
    service: SettingsServiceDep,
    ctx: ContextDep,
) -> ApiResponse[VatRateRead]:
    rate = await service.update_vat_rate(org, rate_id, body, ctx)
    return ok(VatRateRead.from_model(rate), "VAT rate updated")


@router.delete(
    "/vat-rates/{rate_id}",
    summary="Delete or archive a VAT rate",
    description=(
        "Deletes an unused rate. A rate referenced by a service, client, disbursement or "
        "invoice/credit-note line is archived instead (and stops being the default); "
        "`data.outcome` says which. Requires `settings.manage`."
    ),
    responses=error_responses(403, 404),
)
async def delete_vat_rate(
    rate_id: uuid.UUID, org: SettingsManager, service: SettingsServiceDep, ctx: ContextDep
) -> ApiResponse[DeleteOutcome]:
    return _deleted(await service.delete_vat_rate(org, rate_id, ctx), "VAT rate")


# --- Services ---------------------------------------------------------------------------


@router.get(
    "/services",
    summary="List services",
    description=(
        "Active services in display order; `include_inactive=true` adds deactivated ones, "
        "`q` searches name, code and description. Each service carries `status`, "
        "`activity_code_ids` (the activity codes its jobs' time is limited to; empty = any) "
        "and `is_used`/`can_delete`. Bounded list, not paginated. Any member."
    ),
    responses=error_responses(404),
)
async def list_services(
    org: OrgContextDep,
    service: SettingsServiceDep,
    include_inactive: Annotated[bool, Query(description="Include deactivated services")] = False,
    q: Annotated[
        str | None, Query(max_length=200, description="Search code, name and description")
    ] = None,
) -> ApiResponse[list[ServiceRead]]:
    return ok(
        await service.list_services(
            org, include_inactive=include_inactive, q=q.strip() if q and q.strip() else None
        )
    )


@router.get(
    "/services/{service_id}",
    summary="Get a service",
    description="One service. Any member.",
    responses=error_responses(404),
)
async def get_service(
    service_id: uuid.UUID, org: OrgContextDep, service: SettingsServiceDep
) -> ApiResponse[ServiceRead]:
    return ok(await service.get_service(org, service_id))


@router.post(
    "/services",
    status_code=status.HTTP_201_CREATED,
    summary="Create a service",
    description=(
        "`code` is upper-cased and unique in the practice (409 `SERVICE_CODE_TAKEN`), and so "
        "is `name`, ignoring case (409 `SERVICE_NAME_TAKEN`). `default_billable` (default "
        "true) says whether work on the service is billable by default: a job created "
        "without a billing basis starts as `non_billable` when it is false. "
        "`invoice_description` is the wording suggested for invoice lines. `vat_rate_id` "
        "must be a live VAT rate of the practice (422). Limit the activity codes with "
        "`PUT /services/{service_id}/activity-codes`. Requires `settings.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def create_service(
    body: ServiceCreate, org: SettingsManager, service: SettingsServiceDep, ctx: ContextDep
) -> ApiResponse[ServiceRead]:
    return ok(await service.create_service(org, body, ctx), "Service created")


@router.patch(
    "/services/{service_id}",
    summary="Update a service",
    description=(
        "Changes any field of a service (`description`, `default_fee`, `vat_rate_id` and "
        "`invoice_description` accept null to clear them). 409 `SERVICE_CODE_TAKEN` / "
        "`SERVICE_NAME_TAKEN` for duplicates. Requires `settings.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def update_service(
    service_id: uuid.UUID,
    body: ServiceUpdate,
    org: SettingsManager,
    service: SettingsServiceDep,
    ctx: ContextDep,
) -> ApiResponse[ServiceRead]:
    return ok(await service.update_service(org, service_id, body, ctx), "Service updated")


@router.post(
    "/services/{service_id}/deactivate",
    summary="Deactivate a service",
    description=(
        "Hides the service from pickers; existing jobs and engagements keep it. "
        "Requires `settings.manage`."
    ),
    responses=error_responses(403, 404),
)
async def deactivate_service(
    service_id: uuid.UUID, org: SettingsManager, service: SettingsServiceDep, ctx: ContextDep
) -> ApiResponse[ServiceRead]:
    return ok(await service.set_service_active(org, service_id, False, ctx), "Service deactivated")


@router.post(
    "/services/{service_id}/activate",
    summary="Reactivate a service",
    description="Makes a deactivated service available again. Requires `settings.manage`.",
    responses=error_responses(403, 404),
)
async def activate_service(
    service_id: uuid.UUID, org: SettingsManager, service: SettingsServiceDep, ctx: ContextDep
) -> ApiResponse[ServiceRead]:
    return ok(await service.set_service_active(org, service_id, True, ctx), "Service activated")


@router.delete(
    "/services/{service_id}",
    summary="Delete or deactivate a service",
    description=(
        "Deletes an unused service. A service used by a job, client engagement, client default "
        "service, invoice line or rate is deactivated instead; `data.outcome` says which. "
        "Requires `settings.manage`."
    ),
    responses=error_responses(403, 404),
)
async def delete_service(
    service_id: uuid.UUID, org: SettingsManager, service: SettingsServiceDep, ctx: ContextDep
) -> ApiResponse[DeleteOutcome]:
    return _deleted(await service.delete_service(org, service_id, ctx), "Service")


@router.get(
    "/services/{service_id}/activity-codes",
    summary="Activity codes of a service",
    description=(
        "The activity codes time on this service's jobs is limited to. `restricted` is false "
        "(and the list empty) when the service has no limit and every activity code is "
        "allowed. Any member."
    ),
    responses=error_responses(404),
)
async def get_service_activity_codes(
    service_id: uuid.UUID, org: OrgContextDep, service: SettingsServiceDep
) -> ApiResponse[ServiceActivityCodesRead]:
    return ok(await service.get_service_activity_codes(org, service_id))


@router.put(
    "/services/{service_id}/activity-codes",
    summary="Set the activity codes of a service",
    description=(
        "Replaces the activity codes allowed on the service's jobs with `activity_code_ids` "
        "(an empty list removes the limit: every code is allowed). Logging time or starting a "
        "timer on a job of the service with a code outside the list is 422 "
        "`activity_not_allowed`; time already recorded keeps its code. 422 for an unknown "
        "activity code. Requires `settings.manage`."
    ),
    responses=error_responses(403, 404),
)
async def set_service_activity_codes(
    service_id: uuid.UUID,
    body: ServiceActivityCodesUpdate,
    org: SettingsManager,
    service: SettingsServiceDep,
    ctx: ContextDep,
) -> ApiResponse[ServiceActivityCodesRead]:
    result = await service.set_service_activity_codes(org, service_id, body, ctx)
    return ok(result, "Activity codes updated")


# --- Activity codes ---------------------------------------------------------------------


@router.get(
    "/activity-codes",
    summary="List activity codes",
    description=(
        "Active codes, chargeable first then by code; `include_inactive=true` adds "
        "deactivated ones, `q` searches name and code. `is_chargeable` is the default billable "
        "status of time recorded against the code. Bounded list, not paginated. Any member."
    ),
    responses=error_responses(404),
)
async def list_activity_codes(
    org: OrgContextDep,
    service: SettingsServiceDep,
    include_inactive: Annotated[bool, Query(description="Include deactivated codes")] = False,
    q: Annotated[str | None, Query(max_length=200, description="Search code and name")] = None,
) -> ApiResponse[list[ActivityCodeRead]]:
    return ok(
        await service.list_activity_codes(
            org, include_inactive=include_inactive, q=q.strip() if q and q.strip() else None
        )
    )


@router.get(
    "/activity-codes/{activity_id}",
    summary="Get an activity code",
    description="One activity code. Any member.",
    responses=error_responses(404),
)
async def get_activity_code(
    activity_id: uuid.UUID, org: OrgContextDep, service: SettingsServiceDep
) -> ApiResponse[ActivityCodeRead]:
    return ok(await service.get_activity_code(org, activity_id))


@router.post(
    "/activity-codes",
    status_code=status.HTTP_201_CREATED,
    summary="Create an activity code",
    description=(
        "`code` is upper-cased and unique (409 `ACTIVITY_CODE_TAKEN`), and so is `name`, "
        "ignoring case (409 `ACTIVITY_NAME_TAKEN`). `is_chargeable` is the default billable "
        "status; non-chargeable codes record internal time. `vat_rate_id` (a live VAT rate, "
        "422 otherwise) and `invoice_description` are suggestions for invoice lines. "
        "Requires `settings.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def create_activity_code(
    body: ActivityCodeCreate, org: SettingsManager, service: SettingsServiceDep, ctx: ContextDep
) -> ApiResponse[ActivityCodeRead]:
    return ok(await service.create_activity_code(org, body, ctx), "Activity code created")


@router.patch(
    "/activity-codes/{activity_id}",
    summary="Update an activity code",
    description=(
        "Changes code, name, default billable status, VAT rate, invoice description or "
        "active state (`vat_rate_id` and `invoice_description` accept null to clear them). "
        "Requires `settings.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def update_activity_code(
    activity_id: uuid.UUID,
    body: ActivityCodeUpdate,
    org: SettingsManager,
    service: SettingsServiceDep,
    ctx: ContextDep,
) -> ApiResponse[ActivityCodeRead]:
    result = await service.update_activity_code(org, activity_id, body, ctx)
    return ok(result, "Activity code updated")


@router.post(
    "/activity-codes/{activity_id}/deactivate",
    summary="Deactivate an activity code",
    description=(
        "Hides the code from time entry; existing time keeps it. Requires `settings.manage`."
    ),
    responses=error_responses(403, 404),
)
async def deactivate_activity_code(
    activity_id: uuid.UUID, org: SettingsManager, service: SettingsServiceDep, ctx: ContextDep
) -> ApiResponse[ActivityCodeRead]:
    result = await service.set_activity_code_active(org, activity_id, False, ctx)
    return ok(result, "Activity code deactivated")


@router.post(
    "/activity-codes/{activity_id}/activate",
    summary="Reactivate an activity code",
    description="Makes a deactivated code available again. Requires `settings.manage`.",
    responses=error_responses(403, 404),
)
async def activate_activity_code(
    activity_id: uuid.UUID, org: SettingsManager, service: SettingsServiceDep, ctx: ContextDep
) -> ApiResponse[ActivityCodeRead]:
    result = await service.set_activity_code_active(org, activity_id, True, ctx)
    return ok(result, "Activity code activated")


@router.delete(
    "/activity-codes/{activity_id}",
    summary="Delete or deactivate an activity code",
    description=(
        "Deletes an unused code. A code used by time entries or timers, or listed by a "
        "service, is deactivated instead; `data.outcome` says which. "
        "Requires `settings.manage`."
    ),
    responses=error_responses(403, 404),
)
async def delete_activity_code(
    activity_id: uuid.UUID, org: SettingsManager, service: SettingsServiceDep, ctx: ContextDep
) -> ApiResponse[DeleteOutcome]:
    return _deleted(await service.delete_activity_code(org, activity_id, ctx), "Activity code")


# --- Number sequences -------------------------------------------------------------------


@router.get(
    "/sequences",
    summary="List number sequences",
    description=(
        "The seven sequences (client, credit_note, estimate, invoice, job, staff, ticket) with "
        "prefix, padding, next number and a preview of the next number. Any member."
    ),
    responses=error_responses(404),
)
async def list_sequences(
    org: OrgContextDep, service: SettingsServiceDep
) -> ApiResponse[list[SequenceRead]]:
    return ok([SequenceRead.from_model(s) for s in await service.list_sequences(org)])


@router.patch(
    "/sequences/{kind}",
    summary="Change a number sequence",
    description=(
        "Changes prefix, padding and next number. Invoice, credit-note, estimate and ticket "
        "numbers only move forward (409 `SEQUENCE_BACKWARDS`); a next number that is already "
        "used answers 409 `SEQUENCE_NUMBER_TAKEN`. Client codes must stay valid codes (422). "
        "Requires `organization.update`."
    ),
    responses=error_responses(403, 404, 409),
)
async def update_sequence(
    kind: SequenceKind,
    body: SequenceUpdate,
    org: NumberingManager,
    service: SettingsServiceDep,
    ctx: ContextDep,
) -> ApiResponse[SequenceRead]:
    sequence = await service.update_sequence(org, kind, body, ctx)
    return ok(SequenceRead.from_model(sequence), "Numbering updated")
