"""Staff: records, lifecycle and login access. HTTP only: every rule lives in the services."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response, status

from app.dependencies.common import ContextDep, SessionDep, SettingsDep
from app.dependencies.organization import PageParamsDep, require
from app.repositories.staff import STATUS_ALIASES, StaffFilters
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.schemas.staff import (
    StaffBulkAction,
    StaffBulkResult,
    StaffCreate,
    StaffDeactivate,
    StaffDetail,
    StaffInvite,
    StaffInvited,
    StaffLinked,
    StaffLinkLogin,
    StaffRead,
    StaffSort,
    StaffStatus,
    StaffUpdate,
)
from app.services.policy import OrgContext
from app.services.sessions import SessionService
from app.services.staff import StaffService
from app.services.staff_bulk import StaffBulkService
from app.services.staff_login import StaffLoginService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Staff"])

StaffReader = Annotated[OrgContext, Depends(require("member.read"))]
StaffCreator = Annotated[OrgContext, Depends(require("member.invite"))]
StaffEditor = Annotated[OrgContext, Depends(require("member.update"))]
StaffRemover = Annotated[OrgContext, Depends(require("member.remove"))]

_NO_STORE = {"Cache-Control": "no-store"}
_RATES = (
    " `charge_rate`, `cost_rate` and their sources (the standard rates that apply to the person "
    "today) are null for a caller without `rate.read`."
)


def get_staff_service(session: SessionDep, settings: SettingsDep) -> StaffService:
    return StaffService(session, SessionService(session, settings))


def get_staff_login_service(session: SessionDep, settings: SettingsDep) -> StaffLoginService:
    return StaffLoginService(session, SessionService(session, settings))


def get_staff_bulk_service(
    session: SessionDep, service: Annotated[StaffService, Depends(get_staff_service)]
) -> StaffBulkService:
    return StaffBulkService(session, service)


StaffServiceDep = Annotated[StaffService, Depends(get_staff_service)]
StaffBulkServiceDep = Annotated[StaffBulkService, Depends(get_staff_bulk_service)]
StaffLoginServiceDep = Annotated[StaffLoginService, Depends(get_staff_login_service)]


@router.get(
    "/staff",
    summary="List staff",
    description=(
        "Staff of the practice, cursor-paginated. `q` searches name, code, work email and "
        "sign-in email; `status` (repeatable) = `active`, `inactive` or `left` - by default "
        "active and inactive staff; `grade_id`, `approver_id`, `role_id`; `has_login` "
        "true/false; `selectable=true` keeps only people time can be recorded for (active and "
        "not past their leaving date). `sort` = `name` (default) | `code` | `email` | `grade` "
        "(grade order) | `status` | `start_date` | `leaving_date`, `order` = `asc` | `desc`. "
        "Staff without a login are included." + _RATES + " Requires `member.read`."
    ),
    responses=error_responses(403, 404),
)
async def list_staff(
    org: StaffReader,
    service: StaffServiceDep,
    page: PageParamsDep,
    q: Annotated[
        str | None,
        Query(max_length=200, description="Search name, code, work email and sign-in email"),
    ] = None,
    staff_status: Annotated[
        list[StaffStatus] | None,
        Query(alias="status", description="active, inactive or left; repeat for several"),
    ] = None,
    grade_id: Annotated[uuid.UUID | None, Query(description="Only this grade")] = None,
    approver_id: Annotated[
        uuid.UUID | None, Query(description="Only staff approved by this staff member")
    ] = None,
    role_id: Annotated[
        uuid.UUID | None, Query(description="Only staff with this role (needs a login)")
    ] = None,
    has_login: Annotated[
        bool | None, Query(description="true: can sign in; false: no login")
    ] = None,
    selectable: Annotated[
        bool | None, Query(description="true: time can be recorded for them today")
    ] = None,
    sort: Annotated[StaffSort, Query(description="Sort key")] = "name",
    order: Annotated[Literal["asc", "desc"], Query(description="Sort direction")] = "asc",
) -> ApiResponse[list[StaffRead]]:
    wanted = staff_status or ["active", "inactive"]
    stored = tuple(dict.fromkeys(s for name in wanted for s in STATUS_ALIASES[name]))
    result = await service.list_staff(
        org,
        StaffFilters(
            q=q.strip() if q and q.strip() else None,
            statuses=stored,
            grade_id=grade_id,
            approver_id=approver_id,
            role_id=role_id,
            has_login=has_login,
            selectable=selectable,
        ),
        sort=sort,
        descending=order == "desc",
        params=page,
    )
    return ok_page(result.items, result.next_cursor)


@router.post(
    "/staff",
    status_code=status.HTTP_201_CREATED,
    summary="Create a staff member",
    description=(
        "Creates a staff member without a login (give them one later with `invite` or "
        "`link-login`). Required: `full_name`, `grade_id` (an active grade) and `start_date`. "
        "`code` is upper-cased and unique (409 `STAFF_CODE_TAKEN`); without it the next staff "
        "number is used. `email` (work email) must not belong to another active staff member "
        "(409 `STAFF_EMAIL_TAKEN`). `initials` are unique in the practice (409 "
        "`INITIALS_TAKEN`). `approver_id` must be an active staff member who can sign in. "
        "Working hours default to the practice's minutes per day and working days; the "
        "chargeable target defaults to the grade's target, else 7500 (75%). `role_id` is the "
        "role the person gets once they have a login (default: the Staff role). 422 for an "
        "unknown or inactive grade, role or approver." + _RATES + " Requires `member.invite`."
    ),
    responses=error_responses(403, 404, 409),
)
async def create_staff(
    body: StaffCreate, org: StaffCreator, service: StaffServiceDep, ctx: ContextDep
) -> ApiResponse[StaffDetail]:
    return ok(await service.create(org, body, ctx), "Staff member created")


@router.post(
    "/staff/bulk",
    summary="Change or remove several staff members",
    description=(
        "Applies one `action` (`set_grade`, `set_approver`, `clear_approver`, `deactivate`, "
        "`reactivate`, `delete`) to 1-200 staff members in one transaction: all or nothing. "
        "`value` is the grade id, the approver's staff id, or for `deactivate` `inactive` "
        "(default) or `left` (with an optional `leaving_date`). Each person goes through the "
        "rules of the single endpoint (the last administrator, an owner, yourself, a person "
        "records refer to): if any is refused the answer is 409 `BULK_REFUSED` with every "
        "person in `details` (`staff_id`, `number` = staff code, `name`, `error_code`, "
        "`reason`), and nothing changes. An unknown id answers 404 listing it. Audited as one "
        "`staff.bulk_changed` entry besides the usual ones. "
        "`affected` counts the distinct people, `changed` those that actually changed. "
        "Requires `member.update`; `deactivate` and `delete` need `member.remove`."
    ),
    responses=error_responses(403, 404, 409),
)
async def bulk_staff(
    body: StaffBulkAction, org: StaffEditor, service: StaffBulkServiceDep, ctx: ContextDep
) -> ApiResponse[StaffBulkResult]:
    result = await service.run(org, body, ctx)
    return ok(result, f"{result.changed} staff member(s) updated")


@router.get(
    "/staff/{staff_id}",
    summary="Get a staff member",
    description=(
        "One staff member of any status, with `usage` (what refers to them: time entries, "
        "expenses, invoices, ...) and `can_delete` (true when nothing does)."
        + _RATES
        + " Requires `member.read`."
    ),
    responses=error_responses(403, 404),
)
async def get_staff(
    staff_id: uuid.UUID, org: StaffReader, service: StaffServiceDep
) -> ApiResponse[StaffDetail]:
    return ok(await service.get(org, staff_id))


@router.patch(
    "/staff/{staff_id}",
    summary="Update a staff member",
    description=(
        "Changes details, grade, approver, working hours and days (the weekly capacity is "
        "recalculated), chargeable target and dates. `email`, `approver_id`, `job_title`, "
        "`initials`, `start_date` and `leaving_date` accept null to clear them. A leaving "
        "date in the past stops new time after it (422 `after_leaving_date` when logging) "
        "but the status stays what it is: use `deactivate` to mark someone as left. "
        "`full_name` lives on the person's account: 409 `ACCOUNT_SHARED` when they also "
        "belong to another practice. 409 `STAFF_CODE_TAKEN`, `STAFF_EMAIL_TAKEN`, "
        "`INITIALS_TAKEN` for duplicates. Role and status are changed with "
        "`PATCH /members/{user_id}` and the lifecycle endpoints."
        + _RATES
        + " Requires `member.update`."
    ),
    responses=error_responses(403, 404, 409),
)
async def update_staff(
    staff_id: uuid.UUID,
    body: StaffUpdate,
    org: StaffEditor,
    service: StaffServiceDep,
    ctx: ContextDep,
) -> ApiResponse[StaffDetail]:
    return ok(await service.update(org, staff_id, body, ctx), "Staff member updated")


@router.post(
    "/staff/{staff_id}/deactivate",
    summary="Deactivate a staff member or mark them as left",
    description=(
        "`status` = `inactive` (default: paused, can be reactivated) or `left` (no longer "
        "works for the practice: `leaving_date` defaults to today and is set; people they "
        "approved lose their approver). Everything recorded for them is kept; they can no "
        "longer sign in and are not offered for new time. A person with a login is signed "
        "out everywhere unless their account also belongs to another practice. 403 for "
        "oneself; only an owner changes an owner and the last owner who can sign in cannot "
        "be deactivated (409). 409 `STAFF_LEFT` to mark someone who has left as inactive. "
        "Idempotent. Requires `member.remove`."
    ),
    responses=error_responses(403, 404, 409),
)
async def deactivate_staff(
    staff_id: uuid.UUID,
    body: StaffDeactivate,
    org: StaffRemover,
    service: StaffServiceDep,
    ctx: ContextDep,
) -> ApiResponse[StaffDetail]:
    return ok(await service.deactivate(org, staff_id, body, ctx), "Staff member deactivated")


@router.post(
    "/staff/{staff_id}/reactivate",
    summary="Reactivate a staff member",
    description=(
        "Makes an inactive or departed staff member active again and clears the leaving "
        "date. 409 `STAFF_EMAIL_TAKEN` when another active staff member now has their work "
        "email. Idempotent. Requires `member.update`."
    ),
    responses=error_responses(403, 404, 409),
)
async def reactivate_staff(
    staff_id: uuid.UUID, org: StaffEditor, service: StaffServiceDep, ctx: ContextDep
) -> ApiResponse[StaffDetail]:
    return ok(await service.reactivate(org, staff_id, ctx), "Staff member reactivated")


@router.delete(
    "/staff/{staff_id}",
    summary="Delete a staff member",
    description=(
        "Deletes a staff record that nothing refers to (`can_delete` on the detail). Anyone "
        "with time, expenses, invoices, jobs, rates or other records answers 409 "
        "`STAFF_IN_USE` (`details.usage` lists what; deactivate them instead). 403 for "
        "oneself; only an owner deletes an owner. Requires `member.remove`."
    ),
    responses=error_responses(403, 404, 409),
)
async def delete_staff(
    staff_id: uuid.UUID, org: StaffRemover, service: StaffServiceDep, ctx: ContextDep
) -> ApiResponse[None]:
    await service.delete(org, staff_id, ctx)
    return ok(message="Staff member deleted")


@router.post(
    "/staff/{staff_id}/invite",
    summary="Invite a staff member to sign in",
    description=(
        "Gives a staff member without a login a pending login at `email` (default: their work "
        "email) and emails a one-time link, valid 7 days, for choosing a password. "
        "`login_status` is `invited` until they do. Calling it again for someone still "
        "invited re-sends the link (`resent: true`). `role_id` sets the role they get "
        "(first invitation). 409 `EMAIL_TAKEN` (the address belongs to an account), "
        "409 `STAFF_EMAIL_TAKEN`, 409 `LOGIN_ALREADY_ACTIVE`, 409 `STAFF_NOT_ACTIVE`, "
        "422 when no email is known; 403 when the caller may not hand out the role. "
        "Requires `member.invite`."
    ),
    responses=error_responses(403, 404, 409),
)
async def invite_staff(
    staff_id: uuid.UUID,
    body: StaffInvite,
    org: StaffCreator,
    service: StaffLoginServiceDep,
    ctx: ContextDep,
) -> ApiResponse[StaffInvited]:
    invited = await service.invite(org, staff_id, body, ctx)
    return ok(invited, "Invitation sent")


@router.post(
    "/staff/{staff_id}/link-login",
    summary="Link a login to a staff member",
    description=(
        "For a staff member without a login. `mode` `create_account`: makes the account "
        "(sign-in `email`, default the work email; `role_id` optional) with a temporary "
        "password returned once in this response (`Cache-Control: no-store`) that must be "
        "changed at first sign-in; with `send_email` (default true) they also get a link to "
        "choose their own. `mode` `existing_user`: merges the record into the active member of "
        "this practice who signs in with `email`: that member keeps their account and "
        "takes over the code, grade, hours and dates, and everything recorded for the "
        "placeholder (time, rates, jobs, approvals) moves to them. The surviving staff id is "
        "the login member's (`merged_from` is the old id). 409 `LOGIN_ALREADY_LINKED`, "
        "`EMAIL_TAKEN`, `STAFF_EMAIL_TAKEN`, `STAFF_NOT_ACTIVE`, `LINK_CONFLICT` (both have "
        "records that cannot be combined, e.g. a timesheet for the same week); 422 when no "
        "email is known or no member signs in with it; 403 when the caller may not hand out "
        "the role. Requires `member.invite`."
    ),
    responses=error_responses(403, 404, 409),
)
async def link_login(
    staff_id: uuid.UUID,
    body: StaffLinkLogin,
    response: Response,
    org: StaffCreator,
    service: StaffLoginServiceDep,
    ctx: ContextDep,
) -> ApiResponse[StaffLinked]:
    linked = await service.link_login(org, staff_id, body, ctx)
    response.headers.update(_NO_STORE)
    return ok(linked, "Login linked")


@router.post(
    "/staff/{staff_id}/unlink-login",
    summary="Remove a staff member's login",
    description=(
        "The person keeps their record and history but can no longer sign in: sessions "
        "are revoked, the account becomes a placeholder, people they approved lose their "
        "approver. 409 `NO_LOGIN`; 409 `ACCOUNT_SHARED` when the account also belongs to "
        "another practice; 403 for oneself; only an owner changes an owner and the last "
        "owner who can sign in cannot lose their login (409). Requires `member.remove`."
    ),
    responses=error_responses(403, 404, 409),
)
async def unlink_login(
    staff_id: uuid.UUID, org: StaffRemover, service: StaffLoginServiceDep, ctx: ContextDep
) -> ApiResponse[StaffDetail]:
    return ok(await service.unlink_login(org, staff_id, ctx), "Login removed")
