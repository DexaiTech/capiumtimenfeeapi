"""Two global guards that run before any route: a request-rate ceiling and a body-size cap.

Both answer with the standard error envelope and run inside the CORS and request-id
middleware, so browsers can read the answer and the request id is in it.
"""

import re
from collections.abc import Callable

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import get_settings
from app.core.exceptions import AppException, AuthenticationError, RateLimitError
from app.core.handlers import error_response
from app.core.logging import get_logger
from app.core.tokens import decode_access_token
from app.db.session import get_sessionmaker
from app.services.rate_limit import RateLimiter

log = get_logger(__name__)

WINDOW_SECONDS = 60
_WRITES = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Probes, the interactive docs and CORS preflights are not counted.
_RATE_EXEMPT = re.compile(r"^(/health/|/docs|/openapi\.json|/redoc)")
# The only endpoints that accept large bodies: bulk imports (client lists), ticket attachments
# and expense receipts, which enforce their own, larger limits.
_UPLOADS = re.compile(
    r"^/api/v1/organizations/[^/]+/"
    r"(?:imports/[a-z_]+|tickets/[^/]+/attachments|expenses/[^/]+/receipts)$"
)


class RequestTooLargeError(AppException):
    status_code = 413
    code = "REQUEST_TOO_LARGE"
    message = "The request is too large."


def _bearer(scope: Scope) -> str | None:
    value = Headers(scope=scope).get("authorization", "")
    scheme, _, token = value.partition(" ")
    return token.strip() or None if scheme.lower() == "bearer" else None


def rate_limit_key(scope: Scope) -> tuple[str, int]:
    """The counter key and its limit: per member when the token is valid, else per address."""
    settings = get_settings()
    token = _bearer(scope)
    if token:
        try:
            claims = decode_access_token(settings, token)
        except AuthenticationError:
            pass  # an invalid or expired token is counted like no token at all
        else:
            return f"api:user:{claims.user_id}", settings.api_rate_limit_user_per_minute
    client = scope.get("client")
    address = client[0] if client else "unknown"
    return f"api:ip:{address}", settings.api_rate_limit_ip_per_minute


class ApiRateLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        settings = get_settings()
        if (
            scope["type"] != "http"
            or not settings.api_rate_limit_enabled
            or scope["method"] == "OPTIONS"
            or _RATE_EXEMPT.match(scope["path"])
        ):
            await self.app(scope, receive, send)
            return
        key, limit = rate_limit_key(scope)
        try:
            await RateLimiter(get_sessionmaker()).hit(
                key, limit=limit, window_seconds=WINDOW_SECONDS
            )
        except RateLimitError as exc:
            log.warning("api_rate_limited", scope_key=key.split(":")[1])
            response = error_response(
                exc.status_code, exc.code, exc.message, exc.details, exc.headers
            )
            await response(scope, receive, send)
            return
        except Exception:
            # A failing counter must not take the API down with it.
            log.error("api_rate_limit_unavailable", exc_info=True)
        await self.app(scope, receive, send)


class RequestSizeLimitMiddleware:
    def __init__(
        self, app: ASGIApp, *, exempt: Callable[[str], bool] = lambda p: bool(_UPLOADS.match(p))
    ) -> None:
        self.app = app
        self.exempt = exempt

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in _WRITES or self.exempt(scope["path"]):
            await self.app(scope, receive, send)
            return
        limit = get_settings().max_request_body_kb * 1024
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            await self._reject(scope, receive, send, limit)
            return

        received = 0
        exceeded = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # A chunked body has no declared length: stop reading at the cap.
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            if not exceeded:  # whatever the app answered to a cut-off body is discarded
                await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not exceeded:
                raise
        if exceeded:
            await self._reject(scope, receive, send, limit)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, limit: int) -> None:
        error = RequestTooLargeError(details={"max_bytes": limit})
        response = error_response(error.status_code, error.code, error.message, error.details)
        await response(scope, receive, send)
