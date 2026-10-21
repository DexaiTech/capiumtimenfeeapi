"""The current user: profile, password and sessions."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse

from app.core.exceptions import FileUploadError
from app.dependencies.auth import PrincipalDep, VerifiedPrincipalDep
from app.dependencies.common import ContextDep, SessionDep, SettingsDep
from app.schemas.auth import ChangePasswordRequest
from app.schemas.common import ApiResponse, error_responses, ok
from app.schemas.user import SessionRead, UserRead, UserUpdate
from app.services.avatar_files import MAX_AVATAR_BODY_BYTES, AvatarTooLargeError
from app.services.users import UserService
from app.utils.multipart import FormPart, parse_form_data

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


async def _read_avatar_upload(request: Request) -> FormPart:
    """The ``file`` part of the upload. The body is read under the cap of one picture plus its
    envelope (the global body limit exempts this endpoint); reading stops at the cap."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_AVATAR_BODY_BYTES:
        raise AvatarTooLargeError()
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_AVATAR_BODY_BYTES:
            raise AvatarTooLargeError()
    parts = parse_form_data(request.headers.get("content-type", ""), bytes(body))
    files = [p for p in parts if p.name == "file"]
    if len(files) != 1 or files[0].filename is None:
        raise FileUploadError("Send one file in a form field named 'file'.")
    if not files[0].filename.strip():
        raise FileUploadError("No file was chosen.")
    return files[0]


@router.post(
    "/me/avatar",
    summary="Set the profile picture",
    description=(
        "`multipart/form-data` with the picture in the field `file`: PNG, JPEG, WebP or GIF, at "
        "most 2 MB (413 `FILE_TOO_LARGE`). The extension must be an image one, the content must "
        "be what it says (magic bytes) and a declared content type must fit (422 "
        "`FILE_TYPE_NOT_ALLOWED`; 422 `FILE_EMPTY`). Replaces the previous picture. 503 "
        "`STORAGE_UNAVAILABLE` when the file store cannot take it."
    ),
    responses=error_responses(400, 403, 413, 422, 503),
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {"file": {"type": "string", "format": "binary"}},
                        "required": ["file"],
                    }
                }
            },
        }
    },
)
async def set_avatar(
    request: Request, principal: VerifiedPrincipalDep, service: UserServiceDep, ctx: ContextDep
) -> ApiResponse[UserRead]:
    part = await _read_avatar_upload(request)
    assert part.filename is not None
    user = await service.set_avatar(
        principal.user,
        file_name=part.filename,
        declared_type=part.content_type,
        data=part.data,
        ctx=ctx,
    )
    return ok(UserRead.from_model(user), "Picture updated")


@router.delete(
    "/me/avatar",
    summary="Remove the profile picture",
    description="Back to the initials. Not an error when there is no picture.",
    responses=error_responses(403),
)
async def clear_avatar(
    principal: VerifiedPrincipalDep, service: UserServiceDep, ctx: ContextDep
) -> ApiResponse[UserRead]:
    user = await service.clear_avatar(principal.user, ctx)
    return ok(UserRead.from_model(user), "Picture removed")


@router.get(
    "/{user_id}/avatar",
    summary="Download a profile picture",
    description=(
        "Streams the picture of yourself or of someone in a practice you belong to, with "
        "`X-Content-Type-Options: nosniff` and the content type detected on upload. The request "
        "needs the `Authorization` header, so fetch it and show it from a blob URL. 404 when the "
        "person has no picture or you share no practice with them."
    ),
    responses={
        **error_responses(404),
        200: {"content": {"application/octet-stream": {}}, "description": "The picture"},
    },
    response_class=Response,
)
async def download_avatar(
    user_id: uuid.UUID, principal: PrincipalDep, service: UserServiceDep
) -> StreamingResponse:
    owner = await service.avatar_owner(principal.user, user_id)
    content = await service.open_avatar(owner)
    return StreamingResponse(
        content,
        media_type=owner.avatar_content_type or "application/octet-stream",
        headers={
            "Content-Disposition": "inline",
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
        },
    )
