"""Request ID, log context and the access log line, as one pure ASGI middleware."""

import re
import time
import uuid

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.context import request_id_var
from app.core.logging import get_logger

log = get_logger("access")

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_QUIET_PATHS = ("/health/",)


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        inbound = Headers(scope=scope).get(REQUEST_ID_HEADER, "")
        # Accept a well-formed inbound id so a trace can span services; otherwise generate.
        request_id = inbound if _VALID_REQUEST_ID.fullmatch(inbound) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path = scope["path"]
            if not path.startswith(_QUIET_PATHS):
                # The route template, not the raw path: no ids or tokens in logs.
                route = path
                for name, value in scope.get("path_params", {}).items():
                    route = route.replace(str(value), "{" + name + "}")
                log.info(
                    "request",
                    method=scope["method"],
                    route=route,
                    status_code=status_code,
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                )
            request_id_var.reset(token)
