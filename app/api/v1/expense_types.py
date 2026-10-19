"""Expense types: what an expense claim line is for. HTTP only: the rules live in the service."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import OrgContextDep, PageParamsDep
from app.repositories.expense_types import TypeFilters
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.schemas.expenses import (
    ExpenseTypeCreate,
    ExpenseTypeRead,
    ExpenseTypeUpdate,
    TypeSort,
)
from app.services.expense_types import ExpenseTypeService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Expense types"])


def get_type_service(session: SessionDep) -> ExpenseTypeService:
    return ExpenseTypeService(session)


TypeServiceDep = Annotated[ExpenseTypeService, Depends(get_type_service)]


@router.get(
    "/expense-types",
    summary="List expense types",
    description=(
        "The practice's expense types, cursor-paginated. `q` searches name and code; `is_active` "
        "and `is_mileage` narrow the list; `sort` = `name` (default) | `code`, `order` = `asc` "
        "(default) | `desc`. Each type says whether expenses use it (`is_used`, `can_delete`). "
        "Requires `expense.log`, `expense.read_all` or `expense.manage`."
    ),
    responses=error_responses(403, 404),
)
async def list_expense_types(
    org: OrgContextDep,
    service: TypeServiceDep,
    page: PageParamsDep,
    q: Annotated[str | None, Query(max_length=200, description="Search name and code")] = None,
    is_active: Annotated[bool | None, Query(description="Only active or inactive")] = None,
    is_mileage: Annotated[bool | None, Query(description="Only mileage or other types")] = None,
    sort: Annotated[TypeSort, Query(description="Sort key")] = "name",
    order: Annotated[Literal["asc", "desc"], Query(description="Sort direction")] = "asc",
) -> ApiResponse[list[ExpenseTypeRead]]:
    result = await service.list_types(
        org,
        TypeFilters(
            q=q.strip() if q and q.strip() else None, is_active=is_active, is_mileage=is_mileage
        ),
        sort=sort,
        descending=order == "desc",
        params=page,
    )
    return ok_page(result.items, result.next_cursor)


@router.post(
    "/expense-types",
    status_code=status.HTTP_201_CREATED,
    summary="Create an expense type",
    description=(
        "`name` (1-80 characters) is unique among the active types, ignoring case (409 "
        "`EXPENSE_TYPE_NAME_TAKEN`); `code` (1-12 letters, digits, `_`, `-`; upper-cased) is "
        "unique among all of them (409 `EXPENSE_TYPE_CODE_TAKEN`). `default_vat_rate_id` (a live "
        "VAT rate, else 422) prices the VAT of a line that gives only its net amount; "
        "`is_mileage` makes lines of this type priced from miles; `default_billable` is what a "
        "line with a client starts with. Requires `expense.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def create_expense_type(
    body: ExpenseTypeCreate, org: OrgContextDep, service: TypeServiceDep, ctx: ContextDep
) -> ApiResponse[ExpenseTypeRead]:
    return ok(await service.create(org, body, ctx), "Expense type created")


@router.get(
    "/expense-types/{type_id}",
    summary="Get an expense type",
    description=(
        "One expense type. Requires `expense.log`, `expense.read_all` or `expense.manage`."
    ),
    responses=error_responses(403, 404),
)
async def get_expense_type(
    type_id: uuid.UUID, org: OrgContextDep, service: TypeServiceDep
) -> ApiResponse[ExpenseTypeRead]:
    return ok(await service.get(org, type_id))


@router.patch(
    "/expense-types/{type_id}",
    summary="Update an expense type",
    description=(
        "Changes any field; `default_vat_rate_id` accepts null to clear it. 409 for a name or "
        "code that is taken; 409 `EXPENSE_TYPE_IN_USE` when `is_mileage` changes on a type that "
        "expenses use. Bringing an inactive type back claims its name again. Requires "
        "`expense.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def update_expense_type(
    type_id: uuid.UUID,
    body: ExpenseTypeUpdate,
    org: OrgContextDep,
    service: TypeServiceDep,
    ctx: ContextDep,
) -> ApiResponse[ExpenseTypeRead]:
    return ok(await service.update(org, type_id, body, ctx), "Expense type updated")


@router.delete(
    "/expense-types/{type_id}",
    summary="Delete an expense type",
    description=(
        "Deletes a type no expense uses. A used type answers 409 `EXPENSE_TYPE_IN_USE`: "
        "deactivate it (`is_active` false) instead. Requires `expense.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def delete_expense_type(
    type_id: uuid.UUID, org: OrgContextDep, service: TypeServiceDep, ctx: ContextDep
) -> ApiResponse[None]:
    await service.delete(org, type_id, ctx)
    return ok(message="Expense type deleted")
