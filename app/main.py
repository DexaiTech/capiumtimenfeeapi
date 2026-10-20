"""Application factory."""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api import health
from app.api.router import api_router
from app.core.config import get_settings
from app.core.handlers import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine, get_engine
from app.middleware.compression import JsonGZipMiddleware
from app.middleware.hardening import ApiRateLimitMiddleware, RequestSizeLimitMiddleware
from app.middleware.request_context import REQUEST_ID_HEADER, RequestContextMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware

DESCRIPTION = """
Time recording, work in progress, billing and fee accounting for accountancy practices.

Money is always an integer number of minor units (pence) in the practice currency, VAT
rates are basis points (2000 = 20%) and durations are whole minutes.

Every response uses one envelope: `{success, data, message, meta}` on success and
`{success: false, error: {code, message, details, request_id}}` on failure.
"""


# Connections opened at startup, so the first requests do not each pay the TCP (and TLS and
# authentication) setup to a database that may be far away.
POOL_WARMUP_CONNECTIONS = 3


async def warm_pool(connections: int = POOL_WARMUP_CONNECTIONS) -> None:
    """Open a few pool connections at once. A database that is down must not stop the API from
    starting (``/health/ready`` reports it), so a failure is only logged."""
    log = get_logger(__name__)
    started = time.perf_counter()

    async def ping() -> None:
        async with get_engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
            await asyncio.sleep(0)  # hold it until the others are open too

    results = await asyncio.gather(*(ping() for _ in range(connections)), return_exceptions=True)
    failed = [r for r in results if isinstance(r, BaseException)]
    if failed:
        log.warning("db_pool_warmup_failed", error=str(failed[0]))
    else:
        log.info(
            "db_pool_warm",
            connections=connections,
            ms=round((time.perf_counter() - started) * 1000),
        )


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    log = get_logger(__name__)
    settings = get_settings()
    log.info("api_started", env=settings.app_env)
    await warm_pool()
    yield
    await dispose_engine()
    log.info("api_stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(debug=settings.app_debug)
    # Interactive docs are off in production unless explicitly debugging is allowed there,
    # which the settings forbid; the schema itself is served everywhere else.
    docs_enabled = not settings.is_production
    app = FastAPI(
        title=f"{settings.app_name} API",
        version="0.1.0",
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    # Added last = outermost: the request id exists before anything else runs. The two
    # guards sit innermost, so their answers still carry CORS and security headers.
    app.add_middleware(RequestSizeLimitMiddleware)
    app.add_middleware(ApiRateLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.cookies_secure)
    # Large JSON lists and reports shrink to a fraction on the wire; small answers and file
    # downloads are left alone.
    app.add_middleware(JsonGZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Requested-With", REQUEST_ID_HEADER],
        expose_headers=[REQUEST_ID_HEADER, "Retry-After"],
        max_age=600,
    )
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(api_router)
    return app


app = create_app()
