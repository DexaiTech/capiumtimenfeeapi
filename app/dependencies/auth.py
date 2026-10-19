"""Authentication dependencies: who is calling."""

import uuid
from dataclasses import dataclass
from typing import Annotated

import structlog
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AuthenticationError, AuthorizationError
from app.core.tokens import decode_access_token
from app.db.models import User
from app.dependencies.common import SessionDep, SettingsDep
from app.repositories.sessions import SessionRepository
from app.repositories.users import UserRepository

# auto_error=False: a missing header is reported through our own envelope.
_bearer = HTTPBearer(auto_error=False, description="Access token from /auth/login")


@dataclass(frozen=True, slots=True)
class Principal:
    user: User
    session_id: uuid.UUID


API_PREFIX = "/api/v1"


def allowed_while_password_change_required(method: str, path: str) -> bool:
    """The only routes a person with ``must_change_password`` may use: everything under
    ``/auth``, reading their own profile and changing the password."""
    if path.startswith(f"{API_PREFIX}/auth/"):
        return True
    if path == f"{API_PREFIX}/users/me":
        return method == "GET"
    return path == f"{API_PREFIX}/users/me/password" and method == "POST"


def ensure_password_current(user: User) -> None:
    if user.must_change_password:
        raise AuthorizationError(
            "Change your temporary password to continue.", code="PASSWORD_CHANGE_REQUIRED"
        )


async def authenticate(
    credentials: HTTPAuthorizationCredentials | None, session: AsyncSession, settings: Settings
) -> Principal:
    """The caller behind a bearer token. Knows nothing about which route is being called."""
    if credentials is None or not credentials.credentials:
        raise AuthenticationError()
    claims = decode_access_token(settings, credentials.credentials)
    user = await UserRepository(session).get(claims.user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("Invalid access token.", code="AUTH_TOKEN_INVALID")
    if claims.issued_at < int(user.password_changed_at.timestamp()):
        raise AuthenticationError("Invalid access token.", code="AUTH_TOKEN_INVALID")
    # One primary-key lookup buys immediate logout and session revocation.
    active = await SessionRepository(session).get_active_session(claims.session_id, user.id)
    if active is None:
        raise AuthenticationError("Your session has ended.", code="AUTH_TOKEN_INVALID")
    structlog.contextvars.bind_contextvars(user_id=str(user.id))
    return Principal(user=user, session_id=claims.session_id)


async def get_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: SessionDep,
    settings: SettingsDep,
) -> Principal:
    principal = await authenticate(credentials, session, settings)
    # The one place that gates a temporary password, for every authenticated route.
    if not allowed_while_password_change_required(request.method, request.url.path):
        ensure_password_current(principal.user)
    return principal


async def get_verified_principal(
    principal: Annotated[Principal, Depends(get_principal)],
) -> Principal:
    if not principal.user.email_verified:
        raise AuthorizationError(
            "Verify your email address to continue.", code="AUTH_EMAIL_NOT_VERIFIED"
        )
    return principal


def require_browser_origin(request: Request, settings: SettingsDep) -> None:
    """CSRF defence for the two endpoints that read the refresh cookie.

    A cross-site form cannot set a custom header, and a cross-site script cannot send
    one without a CORS preflight that the allowlist refuses.
    """
    if request.headers.get("x-requested-with") != "XMLHttpRequest":
        raise AuthorizationError("Missing required header.")
    origin = (request.headers.get("origin") or "").rstrip("/")
    if origin not in settings.allowed_origins:
        raise AuthorizationError("Origin not allowed.")


PrincipalDep = Annotated[Principal, Depends(get_principal)]
VerifiedPrincipalDep = Annotated[Principal, Depends(get_verified_principal)]
