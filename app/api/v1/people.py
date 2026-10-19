"""Accounts made by an administrator. HTTP only: every rule lives in ``PeopleService``."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.dependencies.common import ContextDep, SessionDep, SettingsDep
from app.dependencies.organization import require
from app.schemas.common import ApiResponse, error_responses, ok
from app.schemas.organization import MemberRead, PasswordReset, PersonCreate, PersonCreated
from app.services.people import PeopleService
from app.services.policy import OrgContext
from app.services.sessions import SessionService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Members"])

_NO_STORE = {"Cache-Control": "no-store"}


def get_people_service(session: SessionDep, settings: SettingsDep) -> PeopleService:
    return PeopleService(session, SessionService(session, settings))


PeopleServiceDep = Annotated[PeopleService, Depends(get_people_service)]


@router.post(
    "/people",
    status_code=status.HTTP_201_CREATED,
    summary="Create a person",
    description=(
        "Creates a verified account with a generated 16-character password and adds it to "
        "the practice in `role_id`, with optional `initials` (unique in the practice, "
        "upper-cased), `manager_user_id` (an active member who approves their timesheets), "
        "`job_title` and `weekly_capacity_minutes` (default 2250 = 37.5 hours). The password "
        "is in this response only; the person must change it at first sign-in. With "
        "`send_email` (default true) the person also gets "
        "an email with a one-time link (valid 7 days) to choose their own password; the "
        "password itself is never emailed. 409 `EMAIL_TAKEN` when the address already belongs "
        "to an account, 409 `INITIALS_TAKEN` when another member uses the initials, 403 when "
        "the role holds permissions the caller lacks or is Owner and the caller is not an "
        "Owner, 422 for an unknown role or manager. Works with and without "
        "`CLOSED_DEPLOYMENT`. "
        "Requires `member.invite`."
    ),
    responses=error_responses(403, 404, 409),
)
async def create_person(
    body: PersonCreate,
    response: Response,
    org: Annotated[OrgContext, Depends(require("member.invite"))],
    service: PeopleServiceDep,
    settings: SettingsDep,
    ctx: ContextDep,
) -> ApiResponse[PersonCreated]:
    member, password, email_sent = await service.create(org, body, ctx)
    response.headers.update(_NO_STORE)
    return ok(
        PersonCreated(
            member=MemberRead.from_model(member),
            temporary_password=password,
            email_sent=email_sent,
        ),
        "Person created",
    )


@router.post(
    "/members/{user_id}/reset-password",
    summary="Reset a member's password",
    description=(
        "Sets a new generated password, shown in this response only, makes the person change "
        "it at next sign-in and signs them out of every device. 409 `USE_CHANGE_PASSWORD` for "
        "oneself; 409 `ACCOUNT_SHARED` when the person also belongs to another practice; "
        "403 for a member whose role holds permissions the caller lacks (an Owner can only be "
        "reset by an Owner). Requires `member.update`."
    ),
    responses=error_responses(403, 404, 409),
)
async def reset_password(
    user_id: uuid.UUID,
    response: Response,
    org: Annotated[OrgContext, Depends(require("member.update"))],
    service: PeopleServiceDep,
    ctx: ContextDep,
) -> ApiResponse[PasswordReset]:
    password = await service.reset_password(org, user_id, ctx)
    response.headers.update(_NO_STORE)
    return ok(PasswordReset(temporary_password=password), "Password reset")
