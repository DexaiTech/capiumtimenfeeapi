"""Emailing client statements and the statements list. HTTP only: every rule is in the services."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import PageParamsDep, require
from app.schemas.common import ApiResponse, PageMeta, error_responses, ok
from app.schemas.statement_sends import (
    SortOrder,
    StatementSendRead,
    StatementSendRequest,
    StatementSort,
    StatementsRead,
)
from app.services.policy import OrgContext
from app.services.statement_sends import StatementListQuery, StatementSendService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Statements"])


def get_statement_send_service(session: SessionDep) -> StatementSendService:
    return StatementSendService(session)


StatementSendServiceDep = Annotated[StatementSendService, Depends(get_statement_send_service)]
Sender = Annotated[OrgContext, Depends(require("payment.record", "invoice.read", "payment.read"))]
Reader = Annotated[OrgContext, Depends(require("invoice.read", "payment.read"))]


@router.post(
    "/clients/{client_id}/statement/send",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Email a client statement",
    description=(
        "Queues the client's statement of account for email: the PDF (drawn with the client's "
        "invoice template: letterhead, bank details and footer) is attached to the "
        "`client_statement` email. The period is `date_from` to `date_to` (default today); the "
        "addresses are `to`, else the client's billing recipients (422 `NO_RECIPIENT` when "
        "there are none). The request is logged and audited (`statement.send_queued`). A client "
        "that already has a statement queued in the last ten minutes is not queued again "
        "(`queued: false`, the existing `id`). Requires `payment.record`, `invoice.read` and "
        "`payment.read`."
    ),
    responses=error_responses(403, 404),
)
async def send_statement(
    client_id: uuid.UUID,
    body: StatementSendRequest,
    org: Sender,
    service: StatementSendServiceDep,
    ctx: ContextDep,
) -> ApiResponse[StatementSendRead]:
    result = await service.send(org, client_id, body, ctx)
    return ok(result, "Statement queued" if result.queued else "A statement is already queued")


@router.get(
    "/statements",
    summary="Statements list",
    description=(
        "The clients that owe money or hold cash on account, with what is owed on issued "
        "invoices (`outstanding`), the cash on account, the `balance` (the statement's closing "
        "balance), the overdue part, the oldest overdue invoice, when a statement was last "
        "emailed and whether the client has an address to send one to. `totals` cover every "
        "matching client. Filters: client, the client's partner or manager, `min_balance`, "
        "`overdue_only`, `never_sent` and `q` (client name or code). Sorted by `sort` "
        "(`client` A-Z by default, else largest or latest first unless `order=asc`), "
        "cursor-paginated. Requires `invoice.read` and `payment.read`."
    ),
    responses=error_responses(403),
)
async def list_statements(
    org: Reader,
    service: StatementSendServiceDep,
    page: PageParamsDep,
    client_id: Annotated[uuid.UUID | None, Query(description="Only this client")] = None,
    partner_user_id: Annotated[
        uuid.UUID | None, Query(description="Clients whose partner is this person")
    ] = None,
    manager_user_id: Annotated[
        uuid.UUID | None, Query(description="Clients whose manager is this person")
    ] = None,
    min_balance: Annotated[
        int | None,
        Query(ge=-(10**12), le=10**12, description="Only clients whose balance is at least this"),
    ] = None,
    overdue_only: Annotated[bool, Query(description="Only clients with overdue invoices")] = False,
    never_sent: Annotated[bool, Query(description="Only clients never sent a statement")] = False,
    q: Annotated[str | None, Query(max_length=100, description="Client name or code")] = None,
    sort: Annotated[StatementSort, Query(description="Sort column")] = "client",
    order: Annotated[
        SortOrder | None, Query(description="Default `asc` for `client`, else `desc`")
    ] = None,
) -> ApiResponse[StatementsRead]:
    query = StatementListQuery(
        client_id=client_id,
        partner_user_id=partner_user_id,
        manager_user_id=manager_user_id,
        min_balance=min_balance,
        overdue_only=overdue_only,
        never_sent=never_sent,
        q=q.strip() if q else None,
        sort=sort,
        order=order,
    )
    data, next_cursor = await service.list_statements(org, query, page)
    return ApiResponse[StatementsRead](
        data=data, meta=PageMeta(next_cursor=next_cursor, has_more=next_cursor is not None)
    )
