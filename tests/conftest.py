"""Test configuration.

Runs against a disposable PostgreSQL whose database name contains the word ``test`` (for example
``capium_test`` or ``capium_test_j``) and a local SMTP sink. The real ``.env`` is never read.
"""

import atexit
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from email import message_from_bytes
from email.message import Message
from pathlib import Path

import pytest


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


TEST_DB_URL = os.environ.get("TEST_DB_URL", "postgresql://postgres@127.0.0.1:5432/capium_tf_test")
if not re.search(r"(^|_)test(_|$)", TEST_DB_URL.rsplit("/", 1)[-1].split("?")[0]):
    raise RuntimeError("Refusing to run: the test database name must contain the word 'test'.")

SMTP_PORT = _free_port()
FRONTEND_URL = "http://localhost:5173"
# Uploaded files (ticket attachments) go to a scratch directory that every test starts empty.
STORAGE_DIR = Path(tempfile.mkdtemp(prefix="capium-test-storage-"))
atexit.register(shutil.rmtree, STORAGE_DIR, ignore_errors=True)

# Set before any application module is imported; settings are cached on first use.
os.environ.update(
    APP_ENV="test",
    APP_DEBUG="false",
    DB_URL=TEST_DB_URL,
    JWT_SECRET_KEY="test-secret-key-0123456789-0123456789-0123456789",
    FRONTEND_URL=FRONTEND_URL,
    BACKEND_URL="http://localhost:8000",
    CORS_ORIGINS=FRONTEND_URL,
    SMTP_HOST="127.0.0.1",
    SMTP_PORT=str(SMTP_PORT),
    SMTP_USER="",
    SMTP_PASSWORD="",
    SMTP_FROM_ADDRESS="no-reply@example.com",
    # The suite fires thousands of requests from one address; tests of the limiter opt in.
    API_RATE_LIMIT_ENABLED="false",
    CLOSED_DEPLOYMENT="false",
    STORAGE_DIR=str(STORAGE_DIR),
)
os.environ.pop("DB_DIRECT_URL", None)

from aiosmtpd.controller import Controller  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.session import dispose_engine, get_sessionmaker  # noqa: E402
from app.main import app  # noqa: E402
from app.services.central_clients import (  # noqa: E402
    MockCentralClientGateway,
    get_central_gateway,
    reset_central_gateway,
)
from app.services.rate_limit import memory_rate_limiter  # noqa: E402
from app.workers.runner import Worker  # noqa: E402
from tests.integration_fixtures import integration_gateways  # noqa: E402, F401

ROOT = Path(__file__).resolve().parents[1]
BROWSER_HEADERS = {"X-Requested-With": "XMLHttpRequest", "Origin": FRONTEND_URL}
PASSWORD = "correct horse battery"


class Mailbox:
    """Collects messages delivered to the local SMTP sink."""

    def __init__(self) -> None:
        self.messages: list[Message] = []

    async def handle_DATA(self, server, session, envelope) -> str:  # noqa: N802
        self.messages.append(message_from_bytes(envelope.content))
        return "250 OK"

    def clear(self) -> None:
        self.messages.clear()

    def to(self, address: str) -> list[Message]:
        return [m for m in self.messages if m["To"] == address]

    @staticmethod
    def text(message: Message) -> str:
        part = message.get_body(("plain",)) if hasattr(message, "get_body") else None
        if part is None:
            for candidate in message.walk():
                if candidate.get_content_type() == "text/plain":
                    part = candidate
                    break
        assert part is not None
        return part.get_payload(decode=True).decode()

    def token_for(self, address: str) -> str:
        """The one-time token in the newest email to ``address``."""
        match = re.search(r"token=([A-Za-z0-9_-]+)", self.text(self.to(address)[-1]))
        assert match, "no token link in email"
        return match.group(1)


@pytest.fixture(scope="session", autouse=True)
def _migrated_database() -> Iterator[None]:
    """Build the schema from migrations, exactly as a deployment would."""
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    for command in (["downgrade", "base"], ["upgrade", "head"]):
        subprocess.run(  # noqa: S603
            [sys.executable, "-m", "alembic", *command],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
        )
    yield


@pytest.fixture(scope="session")
def mailbox() -> Iterator[Mailbox]:
    box = Mailbox()
    # server_hostname avoids a slow FQDN lookup on every connection.
    controller = Controller(box, hostname="127.0.0.1", port=SMTP_PORT, server_hostname="localhost")
    controller.start()
    yield box
    controller.stop()


@pytest.fixture(autouse=True)
def _fresh_memory_rate_limiter() -> None:
    """The global ceiling is counted in this process: every test starts from zero."""
    memory_rate_limiter.reset()


@pytest.fixture(scope="session", autouse=True)
async def _engine() -> AsyncIterator[None]:
    yield
    await dispose_engine()


def _empty_storage() -> None:
    for entry in STORAGE_DIR.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
async def _clean(mailbox: Mailbox) -> AsyncIterator[None]:
    yield
    mailbox.clear()
    _empty_storage()
    async with get_sessionmaker()() as session:
        await session.execute(
            text(
                # Every practice table cascades from organizations.
                "TRUNCATE users, user_sessions, refresh_tokens, email_verification_tokens, "
                "password_reset_tokens, background_jobs, rate_limit_counters, audit_logs, "
                "organizations CASCADE"
            )
        )
        await session.commit()


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


@pytest.fixture(autouse=True)
def central_gateway() -> Iterator[MockCentralClientGateway]:
    """The mock central client service, clean for every test: the 25-client catalogue, switched
    on, with an empty change feed. Tests push events and switch it off through this fixture."""
    reset_central_gateway()
    gateway = get_central_gateway()
    assert isinstance(gateway, MockCentralClientGateway)
    yield gateway
    reset_central_gateway()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://localhost:8000") as http:
        yield http


@pytest.fixture
def worker() -> Worker:
    return Worker(get_sessionmaker(), get_settings(), worker_id="test-worker")


async def run_jobs(worker: Worker) -> int:
    total = 0
    while processed := await worker.run_once():
        total += processed
    return total


async def register(client: AsyncClient, email: str = "ada@example.com", **overrides) -> None:
    body = {
        "email": email,
        "password": PASSWORD,
        "password_confirm": PASSWORD,
        "full_name": "Ada Lovelace",
        **overrides,
    }
    response = await client.post("/api/v1/auth/register", json=body)
    assert response.status_code == 202, response.text


async def login(client: AsyncClient, email: str = "ada@example.com", password: str = PASSWORD):
    return await client.post("/api/v1/auth/login", json={"email": email, "password": password})


@pytest.fixture
async def verified_user(client: AsyncClient, worker: Worker, mailbox: Mailbox) -> dict:
    """A registered, verified, logged-in user. Returns auth headers and the user."""
    await register(client)
    await run_jobs(worker)
    token = mailbox.token_for("ada@example.com")
    assert (
        await client.post("/api/v1/auth/verify-email", json={"token": token})
    ).status_code == 200
    await run_jobs(worker)  # deliver the welcome email so later assertions start clean
    mailbox.clear()
    data = (await login(client)).json()["data"]
    return {
        "headers": {"Authorization": f"Bearer {data['access_token']}"},
        "user": data["user"],
        "access_token": data["access_token"],
    }
