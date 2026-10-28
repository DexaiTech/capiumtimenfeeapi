"""Dated rate rules, rate preview, rate history and re-rating. HTTP only.

Every rule of every level (client + service + staff, client + service, client + staff, client,
service + grade, staff, grade) and kind (charge-out, cost) is managed here; the V1 endpoints
for a person's rates and a client's rates (``rates.py``) are views of the same rules.
"""

import uuid
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status

from app.dependencies.common import ContextDep, SessionDep, SettingsDep
from app.dependencies.organization import PageParamsDep, require
from app.repositories.rate_history import ChangeFilters
from app.repositories.rate_rules import RuleFilters
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.schemas.rate_rules import (
    ChangeAction,
    RateChangeRead,
    RateKind,
    RateLevel,
    RateRuleCreate,
    RateRuleRead,
    RateRuleUpdate,
    RerateApply,
    ReratePreview,
    RerateRequest,
    RerunItemRead,
    RerunRead,
    ResolveRead,
    RuleSort,
    RuleStatus,
)
from app.schemas.time import RevalueRequest, RevalueResult
from app.services.policy import OrgContext
from app.services.rate_preview import RatePreviewService
from app.services.rate_rules import RateRuleService
from app.services.rerate import RerateService
from app.utils.practice_dates import today_in

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Rates"])

RateReader = Annotated[OrgContext, Depends(require("rate.read"))]
RateManager = Annotated[OrgContext, Depends(require("rate.manage"))]


def get_rate_rule_service(session: SessionDep) -> RateRuleService:
    return RateRuleService(session)


def get_rate_preview_service(session: SessionDep) -> RatePreviewService:
    return RatePreviewService(session)


def get_rerate_service(session: SessionDep, settings: SettingsDep) -> RerateService:
    return RerateService(session, settings)


RuleServiceDep = Annotated[RateRuleService, Depends(get_rate_rule_service)]
PreviewServiceDep = Annotated[RatePreviewService, Depends(get_rate_preview_service)]
RerateServiceDep = Annotated[RerateService, Depends(get_rerate_service)]


# --- Rules ------------------------------------------------------------------------------


@router.get(
    "/rates",
    summary="List rates",
    description=(
        "Dated rate rules of the practice, newest period first by default, cursor-paginated. "
        "`kind` = `charge` (charge-out) or `cost`; `level` (repeatable) = "
        "`client_service_staff`, `client_service`, `client_staff`, `client` (the client's rate "
        "for everyone), `service_staff`, `service_grade`, `staff`, `grade`. `client_id`, "
        "`service_id`, `user_id` "
        "(the staff member) and `grade_id` narrow by what the rule names; `on_date` keeps the "
        "rules in force that day; `status` = `current`, `future` or `ended` (relative to "
        "today in the practice time zone); `q` searches client, service, staff and grade "
        "names and codes. `sort` = `effective_from` | `rate` | `created_at`, `order` = "
        "`asc` | `desc`. `is_used` is true once time falls inside the rule's scope and period "
        "(such a rule can be end-dated, not deleted). Requires `rate.read`."
    ),
    responses=error_responses(403, 404),
)
async def list_rates(
    org: RateReader,
    service: RuleServiceDep,
    page: PageParamsDep,
    kind: Annotated[RateKind | None, Query(description="charge or cost")] = None,
    level: Annotated[list[RateLevel] | None, Query(description="Repeat for several")] = None,
    client_id: Annotated[uuid.UUID | None, Query(description="Rules naming this client")] = None,
    service_id: Annotated[uuid.UUID | None, Query(description="Rules naming this service")] = None,
    user_id: Annotated[uuid.UUID | None, Query(description="The staff member")] = None,
    grade_id: Annotated[uuid.UUID | None, Query(description="Rules naming this grade")] = None,
    on_date: Annotated[date | None, Query(description="Only rules in force that day")] = None,
    rule_status: Annotated[
        RuleStatus | None,
        Query(alias="status", description="current, future or ended, as of today"),
    ] = None,
    q: Annotated[
        str | None,
        Query(max_length=200, description="Search client, service, staff and grade names"),
    ] = None,
    sort: Annotated[RuleSort, Query(description="Sort key")] = "effective_from",
    order: Annotated[Literal["asc", "desc"], Query(description="Sort direction")] = "desc",
) -> ApiResponse[list[RateRuleRead]]:
    result, today = await service.list_rules(
        org,
        RuleFilters(
            kind=kind,
            levels=level,
            client_id=client_id,
            service_id=service_id,
            user_id=user_id,
            grade_id=grade_id,
            on_date=on_date,
            status=rule_status,
            q=q.strip() if q else None,
        ),
        sort=sort,
        descending=order == "desc",
        params=page,
    )
    return ok_page(
        [RateRuleRead.from_view(v, today=today, show_cost=True) for v in result.items],
        result.next_cursor,
    )


@router.post(
    "/rates",
    status_code=status.HTTP_201_CREATED,
    summary="Add a rate",
    description=(
        "Adds a dated rate. Which of `client_id`, `service_id`, `user_id` (staff member) and "
        "`grade_id` you send decides the level: client + service + staff, client + service, "
        "client + staff, client alone (everyone), staff + service (for every client), "
        "service + grade, staff alone or grade alone "
        "(422 `invalid_scope` for any other combination; cost rates exist only for a staff "
        "member or a grade). `rate` is pence per hour; the rule applies from `effective_from` "
        "to `effective_to` inclusive (open-ended when null). 409 `RATE_OVERLAP` when another "
        "rule of the same kind and scope covers any day of the period (`details.conflicts` "
        "lists them); with `close_previous` an earlier rule that runs into the new period is "
        "end-dated the day before instead (only when that is a plain shortening). Existing "
        "time entries keep their rates. Cost rates in the response are null without "
        "`rate.read`. Requires `rate.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def create_rate(
    body: RateRuleCreate, org: RateManager, service: RuleServiceDep, ctx: ContextDep
) -> ApiResponse[RateRuleRead]:
    view = await service.create(org, body, ctx)
    today = _today(org)
    return ok(
        RateRuleRead.from_view(view, today=today, show_cost=service.can_see_costs(org)),
        "Rate added",
    )


@router.get(
    "/rates/resolve",
    summary="Preview the rate that applies",
    description=(
        "Which charge-out and cost rate a staff member (`user_id`) gets on `date` (default "
        "today) for a `client_id`, `job_id` and `service_id`. With a job the client and "
        "service default to the job's own (a job of another client is 422); a given "
        "`service_id` overrides the job's. The response names the applied rate and its "
        "source level, and lists every level of both ladders with whether it applies, "
        "exists and won (`charge_ladder`: levels 1-7 then the practice default; "
        "`cost_ladder`: staff, grade, none). Nothing is written. Unknown ids answer 404. "
        "Requires `rate.read`."
    ),
    responses=error_responses(403, 404),
)
async def resolve_rate(
    org: RateReader,
    service: PreviewServiceDep,
    user_id: Annotated[uuid.UUID, Query(description="The staff member")],
    on_date: Annotated[date | None, Query(alias="date", description="Default today")] = None,
    client_id: Annotated[uuid.UUID | None, Query(description="The client")] = None,
    job_id: Annotated[
        uuid.UUID | None, Query(description="The job; supplies the client and service")
    ] = None,
    service_id: Annotated[
        uuid.UUID | None, Query(description="The service; overrides the job's")
    ] = None,
) -> ApiResponse[ResolveRead]:
    return ok(
        await service.resolve(
            org,
            user_id=user_id,
            on_date=on_date,
            client_id=client_id,
            job_id=job_id,
            service_id=service_id,
        )
    )


@router.get(
    "/rates/history",
    summary="Rate change history",
    description=(
        "Every rate change, newest first (cursor-paginated): who, when, what and the old and "
        "new values (`rate`, `effective_from`, `effective_to`). `action` = `created`, "
        "`updated`, `end_dated` or `deleted`; changes of a grade's default rate appear with "
        "`source` `grade_default` and no `rule_id`. Filter by `user_id` (staff member), "
        "`grade_id`, `client_id`, `service_id`, `kind`, `action` and a `date_from`/`date_to` "
        "range (inclusive, in the practice time zone). Requires `rate.read`."
    ),
    responses=error_responses(403, 404),
)
async def rate_history(
    org: RateReader,
    service: RuleServiceDep,
    page: PageParamsDep,
    user_id: Annotated[uuid.UUID | None, Query(description="The staff member")] = None,
    grade_id: Annotated[uuid.UUID | None, Query(description="Changes naming this grade")] = None,
    client_id: Annotated[uuid.UUID | None, Query(description="Changes naming this client")] = None,
    service_id: Annotated[
        uuid.UUID | None, Query(description="Changes naming this service")
    ] = None,
    kind: Annotated[RateKind | None, Query(description="charge or cost")] = None,
    action: Annotated[
        ChangeAction | None, Query(description="created, updated, end_dated or deleted")
    ] = None,
    date_from: Annotated[
        date | None, Query(description="First day (practice time zone), inclusive")
    ] = None,
    date_to: Annotated[
        date | None, Query(description="Last day (practice time zone), inclusive")
    ] = None,
) -> ApiResponse[list[RateChangeRead]]:
    result = await service.history_page(
        org,
        ChangeFilters(
            kind=kind,
            action=action,
            user_id=user_id,
            grade_id=grade_id,
            client_id=client_id,
            service_id=service_id,
        ),
        date_from=date_from,
        date_to=date_to,
        params=page,
    )
    return ok_page(
        [RateChangeRead.from_view(v, show_cost=True) for v in result.items], result.next_cursor
    )


# --- Re-rating --------------------------------------------------------------------------


@router.post(
    "/rates/rerate/preview",
    summary="Preview re-rating unbilled time",
    description=(
        "Nothing changes. For the scope (`client_ids`, `job_ids`, `user_ids` and/or "
        "`date_from`+`date_to`; every filter given narrows the result, and at least clients, "
        "jobs or a date range is required) returns each unbilled entry's old and new rate "
        "and value, the totals, what is left alone (`skipped`: billed, in draft, written off "
        "or in a locked period) and a `preview_token` (valid 30 minutes, tied to the caller, "
        "the practice and the exact result) that `POST /rates/rerate` needs. `items` lists "
        "entries that would change or are skipped (`include_unchanged` adds the rest), up to "
        "`item_limit`; the totals always cover everything. 422 `scope_too_large` above 20,000 "
        "entries. Cost values are null without `rate.read`. Requires `rate.manage`."
    ),
    responses=error_responses(403, 404),
)
async def preview_rerate(
    body: RerateRequest, org: RateManager, service: RerateServiceDep
) -> ApiResponse[ReratePreview]:
    return ok(await service.preview(org, body))


@router.post(
    "/rates/rerate",
    status_code=status.HTTP_201_CREATED,
    summary="Re-rate unbilled time",
    description=(
        "Applies re-rating to the scope that was previewed: send the same `scope` and the "
        "`preview_token`. The result is computed again under a lock on the entries and must "
        "equal the previewed one: 409 `PREVIEW_REQUIRED` (token missing, invalid, expired or "
        "issued to someone else), 409 `PREVIEW_MISMATCH` (another scope) or 409 "
        "`PREVIEW_STALE` (rates or time changed since; preview again). Only `unbilled` "
        "entries outside locked periods change; a run record, one row per changed or skipped "
        "entry (before and after) and an audit row are written. Requires `rate.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def rerate(
    body: RerateApply, org: RateManager, service: RerateServiceDep, ctx: ContextDep
) -> ApiResponse[RerunRead]:
    run = await service.apply(org, body, ctx)
    return ok(run, f"{run.entries_changed} entries re-rated")


@router.get(
    "/rates/reruns",
    summary="List re-rate runs",
    description=(
        "Every applied re-rate (including V1 `revalue` calls), newest first, cursor-paginated, "
        "with the scope, counts and totals. Cost values are null without `rate.read`. "
        "Requires `rate.read`."
    ),
    responses=error_responses(403, 404),
)
async def list_reruns(
    org: RateReader, service: RerateServiceDep, page: PageParamsDep
) -> ApiResponse[list[RerunRead]]:
    result = await service.list_runs(org, page)
    return ok_page(result.items, result.next_cursor)


@router.get(
    "/rates/reruns/{run_id}",
    summary="Get a re-rate run",
    description="One applied re-rate with its scope, counts and totals. Requires `rate.read`.",
    responses=error_responses(403, 404),
)
async def get_rerun(
    run_id: uuid.UUID, org: RateReader, service: RerateServiceDep
) -> ApiResponse[RerunRead]:
    return ok(await service.get_run(org, run_id))


@router.get(
    "/rates/reruns/{run_id}/items",
    summary="List the entries of a re-rate run",
    description=(
        "The entries the run changed or skipped, by date, cursor-paginated: old and new "
        "rate, value and source. `outcome` = `changed` or `skipped` (with `skip_reason`). "
        "Requires `rate.read`."
    ),
    responses=error_responses(403, 404),
)
async def list_rerun_items(
    run_id: uuid.UUID,
    org: RateReader,
    service: RerateServiceDep,
    page: PageParamsDep,
    outcome: Annotated[
        Literal["changed", "skipped"] | None, Query(description="changed or skipped")
    ] = None,
) -> ApiResponse[list[RerunItemRead]]:
    result = await service.list_items(org, run_id, outcome=outcome, params=page)
    return ok_page(result.items, result.next_cursor)


@router.post(
    "/rates/revalue",
    summary="Re-price unbilled time (V1)",
    description=(
        "Re-takes the charge-out and cost rates of every `unbilled` time entry dated in the "
        "range (optionally one person's or one client's) from the current rate ladder, "
        "whatever its timesheet status, without a preview. Prefer the preview and apply pair "
        "(`/rates/rerate/preview`, `/rates/rerate`); this call is recorded as a re-rate run "
        "of source `revalue`. Returns how many entries changed and the change in value "
        "(`cost_value_delta` is null without `rate.read`). Requires `rate.manage`."
    ),
    responses=error_responses(403, 404),
)
async def revalue_time(
    body: RevalueRequest, org: RateManager, service: RerateServiceDep, ctx: ContextDep
) -> ApiResponse[RevalueResult]:
    result = await service.revalue(org, body, ctx)
    return ok(result, f"{result.entries_changed} entries re-priced")


# --- One rule (declared last: `{rule_id}` would otherwise swallow the paths above) -------


@router.get(
    "/rates/{rule_id}",
    summary="Get a rate",
    description="One dated rate rule with its scope names and usage. Requires `rate.read`.",
    responses=error_responses(403, 404),
)
async def get_rate(
    rule_id: uuid.UUID, org: RateReader, service: RuleServiceDep
) -> ApiResponse[RateRuleRead]:
    view = await service.get(org, rule_id)
    return ok(RateRuleRead.from_view(view, today=_today(org), show_cost=True))


@router.patch(
    "/rates/{rule_id}",
    summary="Change or end-date a rate",
    description=(
        "Changes the amount (`rate`) and/or the period. Send `effective_to` to end-date the "
        "rule, or `null` to make it open-ended again. The kind and scope never change. A rule "
        "that has been used on time entries keeps its start day (409 `RATE_IN_USE`) but can "
        "still be end-dated and its amount changed; entries already recorded keep their "
        "rates. 409 `RATE_OVERLAP` when the new period meets another rule of the same scope. "
        "Requires `rate.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def update_rate(
    rule_id: uuid.UUID,
    body: RateRuleUpdate,
    org: RateManager,
    service: RuleServiceDep,
    ctx: ContextDep,
) -> ApiResponse[RateRuleRead]:
    view = await service.update(org, rule_id, body, ctx)
    return ok(
        RateRuleRead.from_view(view, today=_today(org), show_cost=service.can_see_costs(org)),
        "Rate updated",
    )


@router.delete(
    "/rates/{rule_id}",
    summary="Delete an unused rate",
    description=(
        "Deletes a rate that no time entry could have used. A used rate (`is_used`) cannot "
        "be deleted: 409 `RATE_IN_USE`; end-date it with PATCH instead. The deletion is "
        "kept in the rate history. Requires `rate.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def delete_rate(
    rule_id: uuid.UUID, org: RateManager, service: RuleServiceDep, ctx: ContextDep
) -> ApiResponse[None]:
    await service.delete(org, rule_id, ctx)
    return ok(message="Rate deleted")


def _today(org: OrgContext) -> date:
    return today_in(org.organization.timezone)
