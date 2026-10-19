"""Expense claims, their lines, the workflow and the approval queue. HTTP only: the rules live in
the services (docs/modules/expenses.md)."""

import uuid
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, Query, status

from app.core.exceptions import ValidationError
from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import OrgContextDep, PageParamsDep, require
from app.repositories.expense_claims import ClaimFilters
from app.repositories.expense_lines import LineFilters
from app.schemas.billing import BillingStatus
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.schemas.expenses import (
    ClaimApprove,
    ClaimReturn,
    ClaimSort,
    ClaimStatus,
    ExpenseBulkApprove,
    ExpenseClaimCreate,
    ExpenseClaimDetail,
    ExpenseClaimRead,
    ExpenseClaimUpdate,
    ExpenseLineCreate,
    ExpenseLineListItem,
    ExpenseLineRead,
    ExpenseLineUpdate,
    ExpenseSummary,
    LineSort,
)
from app.services.expense_claims import ExpenseClaimService
from app.services.expense_workflow import ExpenseWorkflowService
from app.services.policy import OrgContext
from app.services.storage import FileStorage, get_storage

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Expense claims"])

StorageDep = Annotated[FileStorage, Depends(get_storage)]
ApproverDep = Annotated[OrgContext, Depends(require("expense.approve"))]


def get_claim_service(session: SessionDep, storage: StorageDep) -> ExpenseClaimService:
    return ExpenseClaimService(session, storage)


def get_workflow_service(session: SessionDep) -> ExpenseWorkflowService:
    return ExpenseWorkflowService(session)


ClaimServiceDep = Annotated[ExpenseClaimService, Depends(get_claim_service)]
WorkflowServiceDep = Annotated[ExpenseWorkflowService, Depends(get_workflow_service)]

_SELF = (
    " Nobody decides on their own claim unless they hold `organization.update` "
    "(403 `SELF_APPROVAL_FORBIDDEN`)."
)
_DECIDERS = (
    " The caller needs `expense.approve` and must be the claimant's manager or hold "
    "`expense.read_all` (403 otherwise)."
)


def _check_range(date_from: date | None, date_to: date | None) -> None:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ValidationError.for_field("date_to", "The end date is before the start date.")


def _order(sort: str, order: str | None, ascending_sorts: tuple[str, ...] = ()) -> bool:
    """Whether the list is descending: ``order`` when given, else newest or largest first."""
    return order == "desc" if order else sort not in ascending_sorts


# --- Claims -------------------------------------------------------------------------------


@router.get(
    "/expense-claims",
    summary="List expense claims",
    description=(
        "Expense claims, cursor-paginated, each with its claimant, status, totals and what the "
        "caller may do (`actions`). Everyone's with `expense.read_all` or `expense.manage`; "
        "otherwise the caller's own, plus those of the people they manage with "
        "`expense.approve` (403 without any expense permission). Filters: `status` (repeatable), "
        "`user_id`, `mine`, `date_from`/`date_to`, `client_id`, `job_id` and `is_billable` (all on "
        "one line of the claim) and `q` (title, claimant, line description or supplier). `sort` = "
        "`created_at` (default) | `submitted_at` | `total` | `user`; `order` = `asc` | `desc` "
        "(default `desc`, `asc` for `user`). A cursor from another sort answers 422."
    ),
    responses=error_responses(403, 404),
)
async def list_expense_claims(
    org: OrgContextDep,
    service: ClaimServiceDep,
    page: PageParamsDep,
    statuses: Annotated[
        list[ClaimStatus] | None,
        Query(alias="status", max_length=4, description="Status; repeat for several"),
    ] = None,
    user_id: Annotated[uuid.UUID | None, Query(description="Claims of this person")] = None,
    mine: Annotated[bool, Query(description="Only the caller's own claims")] = False,
    date_from: Annotated[date | None, Query(description="A line dated on or after")] = None,
    date_to: Annotated[date | None, Query(description="A line dated on or before")] = None,
    client_id: Annotated[uuid.UUID | None, Query(description="A line for this client")] = None,
    job_id: Annotated[uuid.UUID | None, Query(description="A line for this job")] = None,
    is_billable: Annotated[
        bool | None, Query(description="A line that is billable (true) or not (false)")
    ] = None,
    q: Annotated[
        str | None,
        Query(max_length=200, description="Search title, claimant, line description, supplier"),
    ] = None,
    sort: Annotated[ClaimSort, Query(description="Sort key")] = "created_at",
    order: Annotated[Literal["asc", "desc"] | None, Query(description="Sort direction")] = None,
) -> ApiResponse[list[ExpenseClaimRead]]:
    _check_range(date_from, date_to)
    result = await service.list_claims(
        org,
        ClaimFilters(
            statuses=statuses or (),
            user_id=user_id,
            date_from=date_from,
            date_to=date_to,
            client_id=client_id,
            job_id=job_id,
            is_billable=is_billable,
            q=q.strip() if q and q.strip() else None,
        ),
        mine=mine,
        sort=sort,
        descending=_order(sort, order, ("user",)),
        params=page,
    )
    return ok_page(result.items, result.next_cursor)


@router.post(
    "/expense-claims",
    status_code=status.HTTP_201_CREATED,
    summary="Create an expense claim",
    description=(
        "A draft claim, optionally with up to 100 `lines` (each checked like "
        "`POST /expense-claims/{id}/lines`; one bad line fails the whole call). `user_id` is the "
        "claimant: the caller by default (needs `expense.log`), another active member with "
        "`expense.manage` (staff without a login included; 404 for someone outside the "
        "practice). Every line date passes the period lock. Returns the claim with its lines."
    ),
    responses=error_responses(403, 404, 409),
)
async def create_expense_claim(
    body: ExpenseClaimCreate, org: OrgContextDep, service: ClaimServiceDep, ctx: ContextDep
) -> ApiResponse[ExpenseClaimDetail]:
    return ok(await service.create(org, body, ctx), "Expense claim created")


@router.get(
    "/expense-claims/summary",
    summary="Expense claim summary",
    description=(
        "Counts of the caller's visible claims by status (`draft`, `submitted`, `approved`, "
        "`returned`, `total`), the gross of the visible claims waiting for approval "
        "(`pending_gross`, pence) and the caller's own approval queue (`awaiting_my_approval`: "
        "claims and gross; zero without `expense.approve`). For the menu badges."
    ),
    responses=error_responses(403, 404),
)
async def expense_summary(
    org: OrgContextDep, service: ClaimServiceDep
) -> ApiResponse[ExpenseSummary]:
    return ok(await service.summary(org))


@router.get(
    "/expense-claims/{claim_id}",
    summary="Get an expense claim",
    description=(
        "The claim with its lines (each with its receipts' metadata, the VAT, mileage and "
        "recharge figures), totals (`net`, `vat`, `gross`, `recharge`, `reimbursable`), the "
        "history of submissions and decisions (`events`, oldest first) and what the caller may "
        "do (`actions`). Own claims, or anyone's with `expense.read_all`/`expense.manage`, or "
        "those of the people the caller manages with `expense.approve`; another practice's claim "
        "answers 404."
    ),
    responses=error_responses(403, 404),
)
async def get_expense_claim(
    claim_id: uuid.UUID, org: OrgContextDep, service: ClaimServiceDep
) -> ApiResponse[ExpenseClaimDetail]:
    return ok(await service.get(org, claim_id))


@router.patch(
    "/expense-claims/{claim_id}",
    summary="Update an expense claim",
    description=(
        "Changes the `title` (null or blank clears it) of a `draft` or `returned` claim "
        "(409 `EXPENSE_CLAIM_LOCKED` otherwise). The claimant with `expense.log`, or anyone "
        "with `expense.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def update_expense_claim(
    claim_id: uuid.UUID,
    body: ExpenseClaimUpdate,
    org: OrgContextDep,
    service: ClaimServiceDep,
    ctx: ContextDep,
) -> ApiResponse[ExpenseClaimDetail]:
    return ok(await service.update(org, claim_id, body, ctx), "Expense claim updated")


@router.delete(
    "/expense-claims/{claim_id}",
    summary="Delete an expense claim",
    description=(
        "Deletes a `draft` claim with its lines and receipts (also from the file storage); "
        "any other status answers 409 `EXPENSE_CLAIM_LOCKED`. The claimant with `expense.log`, "
        "or anyone with `expense.manage`. Every line date passes the period lock."
    ),
    responses=error_responses(403, 404, 409),
)
async def delete_expense_claim(
    claim_id: uuid.UUID, org: OrgContextDep, service: ClaimServiceDep, ctx: ContextDep
) -> ApiResponse[None]:
    await service.delete(org, claim_id, ctx)
    return ok(message="Expense claim deleted")


# --- Lines --------------------------------------------------------------------------------


@router.post(
    "/expense-claims/{claim_id}/lines",
    status_code=status.HTTP_201_CREATED,
    summary="Add a line to an expense claim",
    description=(
        "Adds a line to a `draft` or `returned` claim (409 `EXPENSE_CLAIM_LOCKED` otherwise; at "
        "most 200 lines, 409 `EXPENSE_CLAIM_LINE_LIMIT`). `incurred_on` is not in the future. "
        "Amounts are pence: any two of `net`, `vat`, `gross` give the third; only `net` or "
        "only `gross` takes the VAT from `vat_rate_id`, else the type's default VAT code, else "
        "none; `gross` must be `net + vat` (422). A mileage type takes `miles` (at most two "
        "decimals) and prices the net as miles x the practice's mileage rate, kept on the line "
        "(`mileage_rate_applied`); other types refuse `miles`. `job_id` must be an open job of "
        "`client_id` (409 `JOB_CLOSED`, 422 for another client's job); an inactive or archived "
        "client answers 409 `CLIENT_INACTIVE`. `is_billable` defaults to the type's, and false "
        "without a client. A billable line is recharged at `net` plus the practice markup (the "
        "markup is kept on the line); `recharge_amount` overrides it and needs `expense.manage`. "
        "The date passes the period lock. The claimant with `expense.log`, or anyone with "
        "`expense.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def add_expense_line(
    claim_id: uuid.UUID,
    body: ExpenseLineCreate,
    org: OrgContextDep,
    service: ClaimServiceDep,
    ctx: ContextDep,
) -> ApiResponse[ExpenseLineRead]:
    return ok(await service.add_line(org, claim_id, body, ctx), "Expense added")


@router.patch(
    "/expense-claims/{claim_id}/lines/{line_id}",
    summary="Update a line of an expense claim",
    description=(
        "Changes any field of a line of a `draft` or `returned` claim; what is not sent stays. "
        "Sending `net`, `vat` or `gross` recalculates the others as when adding a line (only "
        "`vat` keeps the net); changing `vat_rate_id` alone recalculates the VAT; changing "
        "`miles` (or the type to a mileage type) prices the mileage again at today's rate; "
        "changing the net or `is_billable` recalculates the recharge unless `recharge_amount` "
        "is sent (needs `expense.manage`; null recalculates it). Changing `client_id` clears "
        "the job unless `job_id` is sent. An **approved** claim's line can be changed only by "
        "someone with `expense.manage`, with a `reason` (422 "
        "`EXPENSE_CORRECTION_REASON_REQUIRED`); it is audited with before and after values and "
        "added to the claim's history; a billed line answers 409 `EXPENSE_BILLED`. A submitted "
        "claim answers 409 `EXPENSE_CLAIM_LOCKED`. The old and the new date pass the period lock."
    ),
    responses=error_responses(403, 404, 409),
)
async def update_expense_line(
    claim_id: uuid.UUID,
    line_id: uuid.UUID,
    body: ExpenseLineUpdate,
    org: OrgContextDep,
    service: ClaimServiceDep,
    ctx: ContextDep,
) -> ApiResponse[ExpenseLineRead]:
    return ok(await service.update_line(org, claim_id, line_id, body, ctx), "Expense updated")


@router.delete(
    "/expense-claims/{claim_id}/lines/{line_id}",
    summary="Delete a line of an expense claim",
    description=(
        "Deletes a line of a `draft` or `returned` claim, with its receipts (also from the file "
        "storage). 409 `EXPENSE_CLAIM_LOCKED` otherwise; the line date passes the period lock."
    ),
    responses=error_responses(403, 404, 409),
)
async def delete_expense_line(
    claim_id: uuid.UUID,
    line_id: uuid.UUID,
    org: OrgContextDep,
    service: ClaimServiceDep,
    ctx: ContextDep,
) -> ApiResponse[None]:
    await service.delete_line(org, claim_id, line_id, ctx)
    return ok(message="Expense deleted")


# --- Workflow -----------------------------------------------------------------------------


@router.post(
    "/expense-claims/{claim_id}/submit",
    summary="Submit an expense claim",
    description=(
        "Sends a `draft` or `returned` claim for approval. It needs at least one line (422 "
        "`EXPENSE_CLAIM_EMPTY`); every line above the practice's receipt threshold (gross, "
        "pence; not mileage) needs a receipt (422 `RECEIPT_REQUIRED`, `error.details` has one "
        "`line_id` per line without one); every line date passes the period lock. The approvers "
        "(the claimant's manager, else everyone with `expense.approve` and `expense.read_all`) "
        "are notified. The claimant with `expense.log`, or anyone with `expense.manage`; "
        "another status answers 409 `INVALID_STATUS_TRANSITION`."
    ),
    responses=error_responses(403, 404, 409),
)
async def submit_expense_claim(
    claim_id: uuid.UUID, org: OrgContextDep, service: WorkflowServiceDep, ctx: ContextDep
) -> ApiResponse[ExpenseClaimDetail]:
    return ok(await service.submit(org, claim_id, ctx), "Expense claim submitted")


@router.post(
    "/expense-claims/{claim_id}/recall",
    summary="Recall an expense claim",
    description=(
        "Pulls a `submitted` claim back to `draft` before anyone decides on it. The claimant "
        "with `expense.log`, or anyone with `expense.manage`; another status answers 409 "
        "`INVALID_STATUS_TRANSITION`."
    ),
    responses=error_responses(403, 404, 409),
)
async def recall_expense_claim(
    claim_id: uuid.UUID, org: OrgContextDep, service: WorkflowServiceDep, ctx: ContextDep
) -> ApiResponse[ExpenseClaimDetail]:
    return ok(await service.recall(org, claim_id, ctx), "Expense claim recalled")


@router.post(
    "/expense-claims/{claim_id}/approve",
    summary="Approve an expense claim",
    description=(
        "Approves a `submitted` claim, with an optional `note`; its lines become immutable and "
        "the billable ones work in progress. Every line date passes the period lock again. The "
        "claimant is notified." + _DECIDERS + _SELF
    ),
    responses=error_responses(403, 404, 409),
)
async def approve_expense_claim(
    claim_id: uuid.UUID,
    org: ApproverDep,
    service: WorkflowServiceDep,
    ctx: ContextDep,
    body: Annotated[ClaimApprove | None, Body()] = None,
) -> ApiResponse[ExpenseClaimDetail]:
    note = body.note if body else None
    return ok(await service.approve(org, claim_id, note, ctx), "Expense claim approved")


@router.post(
    "/expense-claims/{claim_id}/return",
    summary="Return an expense claim",
    description=(
        "Sends a `submitted` claim back to its claimant with a `reason` (required: 422 "
        "`EXPENSE_RETURN_REASON_REQUIRED`); they edit and submit it again. The claimant is "
        "notified." + _DECIDERS + _SELF
    ),
    responses=error_responses(403, 404, 409),
)
async def return_expense_claim(
    claim_id: uuid.UUID,
    org: ApproverDep,
    service: WorkflowServiceDep,
    ctx: ContextDep,
    body: Annotated[ClaimReturn | None, Body()] = None,
) -> ApiResponse[ExpenseClaimDetail]:
    reason = body.reason if body else None
    return ok(await service.return_claim(org, claim_id, reason, ctx), "Expense claim returned")


# --- Approvals ----------------------------------------------------------------------------


@router.get(
    "/expense-approvals",
    summary="Expense approvals queue",
    description=(
        "Submitted claims waiting for a decision, oldest submission first, cursor-paginated: "
        "those of the people the caller manages, or everyone's with `expense.read_all`. The "
        "caller's own claim is listed only when they may approve it. `user_id` and `q` narrow "
        "the queue. Requires `expense.approve`."
    ),
    responses=error_responses(403, 404),
)
async def expense_approvals(
    org: ApproverDep,
    service: WorkflowServiceDep,
    page: PageParamsDep,
    user_id: Annotated[uuid.UUID | None, Query(description="Claims of this person")] = None,
    q: Annotated[
        str | None,
        Query(max_length=200, description="Search title, claimant, line description, supplier"),
    ] = None,
) -> ApiResponse[list[ExpenseClaimRead]]:
    result = await service.approvals(
        org, ClaimFilters(user_id=user_id, q=q.strip() if q and q.strip() else None), params=page
    )
    return ok_page(result.items, result.next_cursor)


@router.post(
    "/expense-approvals/bulk-approve",
    summary="Approve many expense claims",
    description=(
        "Approves up to 100 submitted claims atomically: an unknown claim (404, `details` lists "
        "the ids), one that is not submitted (409), one the caller may not decide on (403), "
        "one's own (403) or a locked period (409 `PERIOD_LOCKED`) rolls back all of them. "
        "Requires `expense.approve`." + _SELF
    ),
    responses=error_responses(403, 404, 409),
)
async def bulk_approve_expense_claims(
    body: ExpenseBulkApprove, org: ApproverDep, service: WorkflowServiceDep, ctx: ContextDep
) -> ApiResponse[list[ExpenseClaimRead]]:
    claims = await service.bulk_approve(org, body.claim_ids, ctx)
    plural = "" if len(claims) == 1 else "s"
    return ok(claims, f"{len(claims)} expense claim{plural} approved")


# --- Lines for the finance views ----------------------------------------------------------


@router.get(
    "/expenses",
    summary="List expense lines",
    description=(
        "Expense lines across claims, for the finance views and My Expenses, cursor-paginated; "
        "each carries its claim's status, title and claimant. The same visibility as "
        "`GET /expense-claims`. Filters: `status` (of the claim; repeatable), `claim_id`, "
        "`user_id`, `mine`, `client_id`, `job_id`, `expense_type_id`, `is_billable`, "
        "`billing_status` (repeatable), `date_from`/`date_to` (inclusive, on the line date), "
        "`needs_receipt` (true: only lines above the practice's receipt threshold, not mileage, "
        "that have no receipt) and "
        "`q` (description, supplier, claim title, claimant). `sort` = `incurred_on` (default) | "
        "`gross` | `created_at`, `order` = `asc` | `desc` (default `desc`)."
    ),
    responses=error_responses(403, 404),
)
async def list_expense_lines(
    org: OrgContextDep,
    service: ClaimServiceDep,
    page: PageParamsDep,
    statuses: Annotated[
        list[ClaimStatus] | None,
        Query(alias="status", max_length=4, description="Claim status; repeat for several"),
    ] = None,
    claim_id: Annotated[uuid.UUID | None, Query(description="Lines of this claim")] = None,
    user_id: Annotated[uuid.UUID | None, Query(description="Claims of this person")] = None,
    mine: Annotated[bool, Query(description="Only the caller's own lines")] = False,
    client_id: Annotated[uuid.UUID | None, Query(description="Lines for this client")] = None,
    job_id: Annotated[uuid.UUID | None, Query(description="Lines for this job")] = None,
    expense_type_id: Annotated[uuid.UUID | None, Query(description="Lines of this type")] = None,
    is_billable: Annotated[bool | None, Query(description="Billable or not")] = None,
    billing_status: Annotated[
        list[BillingStatus] | None,
        Query(max_length=4, description="Billing status; repeat for several"),
    ] = None,
    date_from: Annotated[date | None, Query(description="Incurred on or after")] = None,
    date_to: Annotated[date | None, Query(description="Incurred on or before")] = None,
    needs_receipt: Annotated[
        bool, Query(description="Only lines that need a receipt and have none")
    ] = False,
    q: Annotated[
        str | None,
        Query(max_length=200, description="Search description, supplier, claim title, claimant"),
    ] = None,
    sort: Annotated[LineSort, Query(description="Sort key")] = "incurred_on",
    order: Annotated[Literal["asc", "desc"] | None, Query(description="Sort direction")] = None,
) -> ApiResponse[list[ExpenseLineListItem]]:
    _check_range(date_from, date_to)
    result = await service.list_lines(
        org,
        LineFilters(
            claim_statuses=statuses or (),
            claim_id=claim_id,
            user_id=user_id,
            client_id=client_id,
            job_id=job_id,
            expense_type_id=expense_type_id,
            is_billable=is_billable,
            billing_statuses=billing_status or (),
            date_from=date_from,
            date_to=date_to,
            q=q.strip() if q and q.strip() else None,
        ),
        mine=mine,
        needs_receipt=needs_receipt,
        sort=sort,
        descending=_order(sort, order),
        params=page,
    )
    return ok_page(result.items, result.next_cursor)
