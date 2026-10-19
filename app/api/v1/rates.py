"""Charge-out and cost rates, V1 views: a person's staff rates and a client's rates. HTTP only.

Both are views of the dated rate rules (``rate_rules.py`` has the full set of endpoints).
"""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import require
from app.schemas.common import ApiResponse, error_responses, ok
from app.schemas.rates import (
    ClientRateCreate,
    ClientRateRead,
    ClientRateUpdate,
    CurrentStaffRate,
    StaffRateCreate,
    StaffRateRead,
)
from app.services.policy import OrgContext
from app.services.rates import RateService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Rates"])

RateReader = Annotated[OrgContext, Depends(require("rate.read"))]
RateManager = Annotated[OrgContext, Depends(require("rate.manage"))]


def get_rate_service(session: SessionDep) -> RateService:
    return RateService(session)


RateServiceDep = Annotated[RateService, Depends(get_rate_service)]


# --- Staff rates ------------------------------------------------------------------------


@router.get(
    "/staff-rates/current",
    summary="Current staff rates",
    description=(
        "Every active staff member, by name, with the staff-level rate in force on `on_date` "
        "(default: today in the practice time zone) - the rule that covers it; rate fields are "
        "null for someone without one. This is the person's own rate, not the rate a given "
        "client or service would price (see `GET /rates/resolve`). Bounded list. "
        "Requires `rate.read`."
    ),
    responses=error_responses(403, 404),
)
async def current_staff_rates(
    org: RateReader,
    service: RateServiceDep,
    on_date: Annotated[date | None, Query(description="Day to evaluate (YYYY-MM-DD)")] = None,
) -> ApiResponse[list[CurrentStaffRate]]:
    day, rows = await service.current_staff_rates(org, on_date)
    return ok([CurrentStaffRate.build(m, u, charge, cost, day) for m, u, charge, cost in rows])


@router.get(
    "/members/{user_id}/rates",
    summary="A member's rate history",
    description=(
        "The person's charge-out and cost rates, newest `effective_from` first. Bounded "
        "list. Requires `rate.read`."
    ),
    responses=error_responses(403, 404),
)
async def list_staff_rates(
    user_id: uuid.UUID, org: RateReader, service: RateServiceDep
) -> ApiResponse[list[StaffRateRead]]:
    rates = await service.list_staff_rates(org, user_id)
    return ok([StaffRateRead.from_row(r, show_cost=True) for r in rates])


@router.post(
    "/members/{user_id}/rates",
    status_code=status.HTTP_201_CREATED,
    summary="Add a staff rate",
    description=(
        "Adds a rate from `effective_from` onwards (minor units per hour). Existing time "
        "entries keep their snapshotted rates. 409 `STAFF_RATE_EXISTS` when the person "
        "already has a rate starting that day, 409 `MEMBER_REMOVED` for someone who left. "
        "`cost_rate` in the response is null for a caller without `rate.read`. "
        "Requires `rate.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def create_staff_rate(
    user_id: uuid.UUID,
    body: StaffRateCreate,
    org: RateManager,
    service: RateServiceDep,
    ctx: ContextDep,
) -> ApiResponse[StaffRateRead]:
    rate = await service.create_staff_rate(org, user_id, body, ctx)
    # rate.manage does not imply rate.read: cost rates stay hidden without it (domain.md).
    return ok(StaffRateRead.from_row(rate, show_cost=service.can_see_costs(org)), "Rate added")


@router.delete(
    "/staff-rates/{rate_id}",
    summary="Delete a staff rate",
    description=(
        "Removes one row of a person's rate history (its charge-out and cost rate); the rate "
        "before it runs on over the gap. Time already recorded is not re-priced. A rate that "
        "time has been recorded under cannot be removed: 409 `RATE_IN_USE` (add a newer rate "
        "instead). Requires `rate.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def delete_staff_rate(
    rate_id: uuid.UUID, org: RateManager, service: RateServiceDep, ctx: ContextDep
) -> ApiResponse[None]:
    await service.delete_staff_rate(org, rate_id, ctx)
    return ok(message="Rate deleted")


# --- Client rates -----------------------------------------------------------------------


@router.get(
    "/clients/{client_id}/rates",
    summary="List a client's rates",
    description=(
        "Rates agreed with the client: the everyone-rate (`user_id` null) first, then per "
        "person by name. Bounded list. Requires `rate.read`."
    ),
    responses=error_responses(403, 404),
)
async def list_client_rates(
    client_id: uuid.UUID, org: RateReader, service: RateServiceDep
) -> ApiResponse[list[ClientRateRead]]:
    rates = await service.list_client_rates(org, client_id)
    return ok([ClientRateRead.from_model(r, name) for r, name in rates])


@router.post(
    "/clients/{client_id}/rates",
    status_code=status.HTTP_201_CREATED,
    summary="Add a client rate",
    description=(
        "A charge-out rate for the client, for one person or (with `user_id` null) for "
        "everyone. `effective_from` defaults to 2000-01-01 (every day) and `effective_to` to "
        "open-ended. At most one rate per client and person covers any day: 409 "
        "`CLIENT_RATE_EXISTS`. Requires `rate.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def create_client_rate(
    client_id: uuid.UUID,
    body: ClientRateCreate,
    org: RateManager,
    service: RateServiceDep,
    ctx: ContextDep,
) -> ApiResponse[ClientRateRead]:
    rate, name = await service.create_client_rate(org, client_id, body, ctx)
    return ok(ClientRateRead.from_model(rate, name), "Client rate added")


@router.patch(
    "/clients/{client_id}/rates/{rate_id}",
    summary="Change a client rate",
    description="Changes the hourly rate. Requires `rate.manage`.",
    responses=error_responses(403, 404),
)
async def update_client_rate(
    client_id: uuid.UUID,
    rate_id: uuid.UUID,
    body: ClientRateUpdate,
    org: RateManager,
    service: RateServiceDep,
    ctx: ContextDep,
) -> ApiResponse[ClientRateRead]:
    rate, name = await service.update_client_rate(org, client_id, rate_id, body, ctx)
    return ok(ClientRateRead.from_model(rate, name), "Client rate updated")


@router.delete(
    "/clients/{client_id}/rates/{rate_id}",
    summary="Delete a client rate",
    description=(
        "Removes the client rate. A rate that time has been recorded under cannot be removed: "
        "409 `RATE_IN_USE` (end-date it with `PATCH /rates/{rule_id}` instead). "
        "Requires `rate.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def delete_client_rate(
    client_id: uuid.UUID,
    rate_id: uuid.UUID,
    org: RateManager,
    service: RateServiceDep,
    ctx: ContextDep,
) -> ApiResponse[None]:
    await service.delete_client_rate(org, client_id, rate_id, ctx)
    return ok(message="Client rate deleted")
