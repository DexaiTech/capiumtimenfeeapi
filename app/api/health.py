"""Liveness and readiness probes, outside the versioned API."""

from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter
from sqlalchemy import text

from app.core.exceptions import ServiceUnavailableError
from app.core.logging import get_logger
from app.dependencies.common import SessionDep
from app.schemas.common import ApiResponse, ok

router = APIRouter(prefix="/health", tags=["Health"])
log = get_logger(__name__)

_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


@lru_cache
def expected_revision() -> str | None:
    return ScriptDirectory.from_config(Config(str(_ALEMBIC_INI))).get_current_head()


@router.get("/live", summary="Process is up")
async def live() -> ApiResponse[None]:
    return ok(message="ok")


@router.get("/ready", summary="Database reachable and migrations at head")
async def ready(session: SessionDep) -> ApiResponse[None]:
    try:
        current = await session.scalar(text("SELECT version_num FROM alembic_version"))
    except Exception as exc:
        log.error("readiness_failed", exc_info=exc)
        raise ServiceUnavailableError(headers={"Retry-After": "5"}) from exc
    if current != expected_revision():
        log.error("readiness_migrations_behind", current=current, expected=expected_revision())
        raise ServiceUnavailableError(headers={"Retry-After": "5"})
    return ok(message="ready")
