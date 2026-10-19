"""The current user: profile, password and sessions."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.dependencies.auth import PrincipalDep, VerifiedPrincipalDep
from app.dependencies.common import ContextDep, SessionDep, SettingsDep
from app.schemas.auth import ChangePasswordRequest
from app.schemas.common import ApiResponse, error_responses, ok
from app.schemas.user import SessionRead, UserRead, UserUpdate
from app.services.users import UserService

router = APIRouter(prefix="/users", tags=["Users"])


def get_user_service(session: SessionDep, settings: SettingsDep) -> UserService:
    return UserService(session, settings)


UserServiceDep = Annotated[UserService, Depends(get_user_service)]


@router.get(
    "/me",
    summary="Get the current user",
    description="Available before the email address is verified.",
    responses=error_responses(),
)
async def get_me(principal: PrincipalDep) -> ApiResponse[UserRead]:
    return ok(UserRead.from_model(principal.user))


@router.patch(
    "/me",
    summary="Update the profile",
    description="Updates the name and timezone of the current user.",
    responses=error_responses(403),
)
async def update_me(
    body: UserUpdate, principal: VerifiedPrincipalDep, service: UserServiceDep
) -> ApiResponse[UserRead]:
    user = await service.update_profile(principal.user, body)
    return ok(UserRead.from_model(user), "Profile updated")


@router.post(
    "/me/password",
    summary="Change the password",
    description="Requires the current password. Signs out every other device.",
    responses=error_responses(400, 403),
)
async def change_password(
    body: ChangePasswordRequest,
    principal: VerifiedPrincipalDep,
    service: UserServiceDep,
    ctx: ContextDep,
) -> ApiResponse[None]:
    await service.change_password(principal.user, body, principal.session_id, ctx)
    return ok(message="Password changed")


@router.get(
    "/me/sessions",
    summary="List active sessions",
    description="Every device currently signed in. Not paginated: the set is small and bounded.",
    responses=error_responses(),
)
async def list_sessions(
    principal: PrincipalDep, service: UserServiceDep
) -> ApiResponse[list[SessionRead]]:
    sessions = await service.list_sessions(principal.user)
    return ok([SessionRead.from_model(s, principal.session_id) for s in sessions])


@router.delete(
    "/me/sessions/{session_id}",
    summary="Revoke a session",
    description="Signs one device out. Revoking the current session signs this device out.",
    responses=error_responses(404),
)
async def revoke_session(
    session_id: uuid.UUID, principal: PrincipalDep, service: UserServiceDep, ctx: ContextDep
) -> ApiResponse[None]:
    await service.revoke_session(principal.user, session_id, ctx)
    return ok(message="Session revoked")
