"""Dependencies shared by all routers: settings, session, request context, services."""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.context import RequestContext
from app.core.exceptions import AuthorizationError
from app.db.session import get_session, get_sessionmaker
from app.services.rate_limit import RateLimiter

SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_request_context(request: Request) -> RequestContext:
    # The client address is trustworthy only because uvicorn is started with
    # --proxy-headers and --forwarded-allow-ips limited to the reverse proxy.
    return RequestContext(
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


def forbid_when_closed(code: str, message: str) -> Callable[[Settings], None]:
    """Route dependency: answer 403 ``code`` while ``CLOSED_DEPLOYMENT`` is on.

    Declared first on the route so it answers before authentication and body validation.
    """

    def dependency(settings: SettingsDep) -> None:
        if settings.closed_deployment:
            raise AuthorizationError(message, code=code)

    return dependency


def get_rate_limiter() -> RateLimiter:
    return RateLimiter(get_sessionmaker())


ContextDep = Annotated[RequestContext, Depends(get_request_context)]
RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]
