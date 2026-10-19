"""Helpers for the tests that run Alembic against the test database."""

import asyncio
import os
import subprocess
import sys

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.db.session import dispose_engine
from tests.conftest import ROOT

__all__ = ["alembic", "head_revision", "migrate"]


def head_revision() -> str:
    """The newest revision in ``alembic/versions``: what ``upgrade head`` leaves behind."""
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return head


def alembic(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


async def migrate(*args: str) -> subprocess.CompletedProcess[str]:
    """Run alembic off the event loop, then drop pooled connections: they hold prepared
    statements of the schema as it was."""
    try:
        return await asyncio.to_thread(alembic, *args)
    finally:
        await dispose_engine()
