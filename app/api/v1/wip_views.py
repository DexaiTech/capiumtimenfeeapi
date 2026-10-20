"""WIP views: the summary by client, job, partner, manager or service, the item list and the ledger.
HTTP only; the figures come from the WIP atoms (``app/services/wip_views.py``)."""

import uuid
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from app.dependencies.common import SessionDep
from app.dependencies.organization import PageParamsDep, require
from app.repositories.wip_atoms import WipFilters
from app.repositories.wip_ledger import MovementFilters
from app.schemas.billing import WipItemRead
from app.schemas.common import ApiResponse, PageMeta, error_responses, ok_page
from app.schemas.wip import (
    MovementKind,
    MovementRead,
    MovementScope,
    WipGroupBy,
    WipItemKind,
    WipSummaryRead,
)
from app.services.policy import OrgContext
from app.services.wip_ledger import WipMovementService
from app.services.wip_views import SummaryQuery, WipViewService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["WIP"])


def get_view_service(session: SessionDep) -> WipViewService:
    return WipViewService(session)


def get_movement_service(session: SessionDep) -> WipMovementService:
    return WipMovementService(session)


ViewServiceDep = Annotated[WipViewService, Depends(get_view_service)]
MovementServiceDep = Annotated[WipMovementService, Depends(get_movement_service)]
WipReader = Annotated[OrgContext, Depends(require("wip.read"))]

SummarySort = Literal["name", "total", "time", "fixed_fee", "expense", "oldest", "over_90", "items"]
ItemSort = Literal["date", "value", "client"]
Order = Literal["asc", "desc"]


@router.get(
    "/wip/summary",
    summary="WIP grouped by client, job, partner, manager or service",
    description=(
        "Unbilled work in progress grouped by `group_by` (default `client`). Every row has "
        "Time WIP, Fixed fee WIP and Expense WIP (disbursements and approved expense lines), "
        "their sum `total`, the value in age buckets (0-30, 31-60, 61-90 and 90+ days old, by "
        "item date, as at `as_at`), the oldest item date, the share older than 90 days and the "
        "item count; `totals` cover every row that matches, not only the page. Fixed-fee WIP "
        "follows the practice's `fixed_fee_wip_mode`; adjustments and transfers in force are "
        "part of the figures. `as_at` (default today in the practice time zone) looks at a day "
        "in the past: the first look at a closed day freezes it, so it never changes later; a "
        "future day is 422. Rows are cursor-paginated and sorted by `sort` and `order`. "
        "Filters: client, job, partner, manager (the job's, else the client's), service, and "
        "`q` (name or code). Requires `wip.read`."
    ),
    response_model=ApiResponse[WipSummaryRead],
    responses=error_responses(403, 404),
)
async def wip_summary(
    org: WipReader,
    service: ViewServiceDep,
    page: PageParamsDep,
    group_by: Annotated[WipGroupBy, Query(description="What a row stands for")] = "client",
    as_at: Annotated[date | None, Query(description="WIP as at the end of this day")] = None,
    client_id: Annotated[uuid.UUID | None, Query(description="Only this client")] = None,
    job_id: Annotated[uuid.UUID | None, Query(description="Only this job")] = None,
    partner_user_id: Annotated[uuid.UUID | None, Query(description="Responsible partner")] = None,
    manager_user_id: Annotated[uuid.UUID | None, Query(description="Responsible manager")] = None,
    service_id: Annotated[uuid.UUID | None, Query(description="Only jobs of this service")] = None,
    q: Annotated[
        str | None, Query(max_length=100, description="Name or code contains this text")
    ] = None,
    sort: Annotated[SummarySort, Query(description="Column to sort by")] = "name",
    order: Annotated[Order, Query(description="Sort direction")] = "asc",
) -> ApiResponse[WipSummaryRead]:
    query = SummaryQuery(
        group_by=group_by,
        filters=WipFilters(
            client_id=client_id,
            job_id=job_id,
            partner_user_id=partner_user_id,
            manager_user_id=manager_user_id,
            service_id=service_id,
        ),
        q=q,
        sort=sort,
        descending=order == "desc",
        as_at=as_at,
    )
    read, next_cursor = await service.summary(org, query, page)
    return ApiResponse[WipSummaryRead](
        data=read, meta=PageMeta(next_cursor=next_cursor, has_more=next_cursor is not None)
    )


@router.get(
    "/wip/items",
    summary="WIP items",
    description=(
        "The unbilled items behind the figures, cursor-paginated: time entries, disbursements, "
        "approved expense lines, the fixed-fee WIP of jobs (one item per job, with its "
        "`fixed_fee` breakdown) and the adjustments and transfers in force (`kind: adjustment`, "
        "not selectable for billing). Each item says its `component`, `value`, `age_days` and "
        "whether billing may take it (`selectable`). Filters: client, job, kind (repeat the "
        "parameter), partner, manager, service, `min_age_days` and `q` (description, client or "
        "job). Sorted by `date` (default, oldest first), `value` or `client`. Requires "
        "`wip.read`."
    ),
    response_model=ApiResponse[list[WipItemRead]],
    responses=error_responses(403, 404),
)
async def wip_items(
    org: WipReader,
    service: ViewServiceDep,
    page: PageParamsDep,
    client_id: Annotated[uuid.UUID | None, Query(description="Only this client")] = None,
    job_id: Annotated[uuid.UUID | None, Query(description="Only this job")] = None,
    kind: Annotated[list[WipItemKind] | None, Query(description="Only these kinds of item")] = None,
    partner_user_id: Annotated[uuid.UUID | None, Query(description="Responsible partner")] = None,
    manager_user_id: Annotated[uuid.UUID | None, Query(description="Responsible manager")] = None,
    service_id: Annotated[uuid.UUID | None, Query(description="Only jobs of this service")] = None,
    min_age_days: Annotated[
        int | None, Query(ge=0, le=36_500, description="Only items at least this many days old")
    ] = None,
    q: Annotated[str | None, Query(max_length=100, description="Text search")] = None,
    sort: Annotated[ItemSort, Query(description="Sort column")] = "date",
    order: Annotated[Order, Query(description="Sort direction")] = "asc",
) -> ApiResponse[list[WipItemRead]]:
    filters = WipFilters(
        client_id=client_id,
        job_id=job_id,
        partner_user_id=partner_user_id,
        manager_user_id=manager_user_id,
        service_id=service_id,
        kinds=frozenset(kind) if kind else None,
    )
    result = await service.items(
        org,
        filters,
        min_age_days=min_age_days,
        q=q,
        sort=sort,
        descending=order == "desc",
        params=page,
    )
    return ok_page(result.items, result.next_cursor)


@router.get(
    "/wip/ledger",
    summary="The WIP ledger",
    description=(
        "Every WIP movement, newest first, cursor-paginated. The ledger is append-only: a "
        "movement is never changed or deleted, a correction is a `reversal` row that points "
        "at the movement (`reversal_of_id`, and `reversed` on the original). `scope: amount` "
        "rows (write-ons, write-offs, transfers) adjust a job's WIP; `scope: items` rows are "
        "memos of what a credit note did to items. `amount` is signed: positive adds WIP, "
        "negative removes it. Filters: client, job, kind (repeat), scope, source and the "
        "effective-date range. Requires `wip.read`."
    ),
    response_model=ApiResponse[list[MovementRead]],
    responses=error_responses(403),
)
async def wip_ledger(
    org: WipReader,
    service: MovementServiceDep,
    page: PageParamsDep,
    client_id: Annotated[uuid.UUID | None, Query(description="Only this client")] = None,
    job_id: Annotated[uuid.UUID | None, Query(description="Only this job")] = None,
    kind: Annotated[list[MovementKind] | None, Query(description="Only these kinds")] = None,
    scope: Annotated[MovementScope | None, Query(description="`amount` or `items`")] = None,
    source_type: Annotated[
        Literal["adjustment", "transfer", "write_off", "credit_note"] | None,
        Query(description="Only movements made by this kind of record"),
    ] = None,
    date_from: Annotated[date | None, Query(description="Effective on or after")] = None,
    date_to: Annotated[date | None, Query(description="Effective on or before")] = None,
) -> ApiResponse[list[MovementRead]]:
    filters = MovementFilters(
        client_id=client_id,
        job_id=job_id,
        kinds=tuple(kind or ()),
        scope=scope,
        source_type=source_type,
        date_from=date_from,
        date_to=date_to,
    )
    result = await service.list(org, filters, page)
    return ok_page(result.items, result.next_cursor)
