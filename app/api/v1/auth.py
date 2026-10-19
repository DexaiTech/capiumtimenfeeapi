"""Authentication routes. HTTP only: every rule lives in ``AuthService``."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from app.core.config import Settings
from app.dependencies.auth import PrincipalDep, require_browser_origin
from app.dependencies.common import (
    ContextDep,
    RateLimiterDep,
    SessionDep,
    SettingsDep,
    forbid_when_closed,
)
from app.schemas.auth import (
    EmailRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    RevokedCount,
    TokenRequest,
    TokenResponse,
)
from app.schemas.common import ApiResponse, error_responses, ok
from app.schemas.user import UserRead
from app.services.auth import AuthService, TokenBundle

router = APIRouter(prefix="/auth", tags=["Authentication"])

REFRESH_COOKIE_PATH = "/api/v1/auth"


def get_auth_service(
    session: SessionDep, settings: SettingsDep, limiter: RateLimiterDep
) -> AuthService:
    return AuthService(session, settings, limiter)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
NoRegistrationWhenClosed = Depends(
    forbid_when_closed(
        "REGISTRATION_DISABLED",
        "Registration is closed. Ask your administrator for an account.",
    )
)


def _set_refresh_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        settings.refresh_cookie_name,
        token,
        max_age=settings.refresh_token_expire_days * 86_400,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=settings.cookies_secure,
        samesite="lax",
    )


def _clear_refresh_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        settings.refresh_cookie_name,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=settings.cookies_secure,
        samesite="lax",
    )


def _token_response(bundle: TokenBundle, settings: Settings) -> ApiResponse[TokenResponse]:
    return ok(
        TokenResponse(
            access_token=bundle.access_token,
            expires_in=bundle.expires_in,
            user=UserRead.from_model(bundle.user),
        )
    )


@router.post(
    "/register",
    dependencies=[NoRegistrationWhenClosed],
    status_code=status.HTTP_202_ACCEPTED,
    summary="Create an account",
    description=(
        "Creates an unverified account and sends a verification email. The response is the "
        "same whether or not the email address is already registered."
    ),
    responses=error_responses(),
)
async def register(
    body: RegisterRequest, service: AuthServiceDep, ctx: ContextDep
) -> ApiResponse[None]:
    await service.register(body, ctx)
    return ok(message="Check your inbox to verify your email")


@router.post(
    "/login",
    summary="Sign in",
    description="Returns a short-lived access token and sets the httpOnly refresh cookie.",
    responses=error_responses(403),
)
async def login(
    body: LoginRequest,
    response: Response,
    service: AuthServiceDep,
    settings: SettingsDep,
    ctx: ContextDep,
) -> ApiResponse[TokenResponse]:
    bundle = await service.login(body, ctx)
    _set_refresh_cookie(response, settings, bundle.refresh_token)
    return _token_response(bundle, settings)


@router.post(
    "/refresh",
    summary="Exchange the refresh cookie for a new access token",
    description=(
        "Rotates the refresh token. Requires the `X-Requested-With: XMLHttpRequest` header "
        "and an allowed `Origin`. Reuse of an old token revokes the session."
    ),
    dependencies=[Depends(require_browser_origin)],
    responses=error_responses(403),
)
async def refresh(
    request: Request,
    response: Response,
    service: AuthServiceDep,
    settings: SettingsDep,
    ctx: ContextDep,
) -> ApiResponse[TokenResponse]:
    bundle = await service.refresh(request.cookies.get(settings.refresh_cookie_name), ctx)
    _set_refresh_cookie(response, settings, bundle.refresh_token)
    return _token_response(bundle, settings)


@router.post(
    "/logout",
    summary="Sign out of this device",
    description="Revokes the current session and clears the refresh cookie. Always succeeds.",
    dependencies=[Depends(require_browser_origin)],
    responses=error_responses(403),
)
async def logout(
    request: Request,
    response: Response,
    service: AuthServiceDep,
    settings: SettingsDep,
    ctx: ContextDep,
) -> ApiResponse[None]:
    await service.logout(request.cookies.get(settings.refresh_cookie_name), ctx)
    _clear_refresh_cookie(response, settings)
    return ok(message="Signed out")


@router.post(
    "/logout-all",
    summary="Sign out of every device",
    description="Revokes every session of the current user, including this one.",
    responses=error_responses(),
)
async def logout_all(
    response: Response,
    principal: PrincipalDep,
    service: AuthServiceDep,
    settings: SettingsDep,
    ctx: ContextDep,
) -> ApiResponse[RevokedCount]:
    revoked = await service.logout_all(principal.user, ctx)
    _clear_refresh_cookie(response, settings)
    return ok(RevokedCount(revoked=revoked), "Signed out everywhere")


@router.post(
    "/verify-email",
    summary="Verify an email address",
    description="Consumes the single-use token from the verification email.",
    responses=error_responses(),
)
async def verify_email(
    body: TokenRequest, service: AuthServiceDep, ctx: ContextDep
) -> ApiResponse[None]:
    await service.verify_email(body.token, ctx)
    return ok(message="Email verified")


@router.post(
    "/resend-verification",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send a new verification email",
    description="Always answers 202; an email is sent only to an existing, unverified account.",
    responses=error_responses(),
)
async def resend_verification(
    body: EmailRequest, service: AuthServiceDep, ctx: ContextDep
) -> ApiResponse[None]:
    await service.resend_verification(body.email, ctx)
    return ok(message="If the address needs verifying, a new email is on its way")


@router.post(
    "/forgot-password",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request a password reset",
    description="Always answers 202; a reset link is sent only when the account exists.",
    responses=error_responses(),
)
async def forgot_password(
    body: EmailRequest, service: AuthServiceDep, ctx: ContextDep
) -> ApiResponse[None]:
    await service.forgot_password(body.email, ctx)
    return ok(message="If an account exists for this address, a reset link is on its way")


@router.post(
    "/reset-password",
    summary="Set a new password with a reset token",
    description="Consumes the single-use token and signs the user out of every device.",
    responses=error_responses(),
)
async def reset_password(
    body: ResetPasswordRequest, service: AuthServiceDep, ctx: ContextDep
) -> ApiResponse[None]:
    await service.reset_password(body, ctx)
    return ok(message="Password updated. Sign in with your new password.")
