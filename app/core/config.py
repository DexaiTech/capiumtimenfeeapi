"""Application settings.

The only module that reads the environment. Everything else calls ``get_settings()``.
"""

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ASYNC_SCHEME = "postgresql+asyncpg"
_SYNC_SCHEMES = {
    "postgres",
    "postgresql",
    "postgresql+psycopg2",
    "postgresql+psycopg",
    _ASYNC_SCHEME,
}


_BARE_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
# The directory that holds ``app/``; a relative STORAGE_DIR is resolved against it.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _encode_userinfo(netloc: str) -> str:
    """Percent-encode a user name or password that holds characters a URL cannot carry.

    A password such as ``p@ss`` pasted into ``DB_URL`` unencoded makes the parser read
    ``ss@host`` as the host name. The credentials end at the last ``@``. Text that is
    already percent-encoded is left alone.
    """
    userinfo, at, hostport = netloc.rpartition("@")
    if not at:
        return netloc
    user, colon, password = userinfo.partition(":")

    def encode(text: str) -> str:
        return quote(_BARE_PERCENT.sub("%25", text), safe="%")

    return f"{encode(user)}{colon}{encode(password)}@{hostport}"


def normalise_db_url(url: str, *, scheme: str = _ASYNC_SCHEME) -> str:
    """Rewrite a PostgreSQL URL to the given scheme and translate ``sslmode`` for asyncpg."""
    parts = urlsplit(url)
    if parts.scheme not in _SYNC_SCHEMES:
        raise ValueError("must be a PostgreSQL URL")
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "sslmode" and scheme == _ASYNC_SCHEME:
            query.append(("ssl", value))
        else:
            query.append((key, value))
    netloc = _encode_userinfo(parts.netloc)
    return urlunsplit((scheme, netloc, parts.path, urlencode(query), parts.fragment))


class Settings(BaseSettings):
    # Tests configure everything through the environment and must never read a real .env.
    model_config = SettingsConfigDict(
        env_file=None if os.environ.get("APP_ENV") == "test" else ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_debug: bool = False
    app_name: str = "Capium Time & Fees"

    db_url: SecretStr
    db_direct_url: SecretStr | None = None
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_pool_pre_ping: bool = False
    db_pool_recycle_seconds: int = Field(default=1800, ge=60)

    jwt_secret_key: SecretStr
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    access_token_expire_minutes: int = Field(default=15, ge=1, le=60)
    refresh_token_expire_days: int = Field(default=30, ge=1, le=365)
    refresh_grace_seconds: int = 20

    frontend_url: str
    backend_url: str
    cors_origins: str = ""

    # Empty SMTP_HOST turns real email off: the mock gateway records messages instead of
    # sending them. A closed deployment works without email (administrators hand out
    # temporary passwords).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from_address: str = "no-reply@localhost"
    smtp_from_name: str = "Capium Time & Fees"
    smtp_timeout_seconds: int = 15
    # Which gateway carries email: ``smtp`` (real) or ``mock`` (records every message in
    # ``message_log``). Unset: smtp when SMTP_HOST is set, else mock. SMS is always the mock.
    messaging_backend: Literal["smtp", "mock"] | None = None

    # Where uploaded files (ticket attachments) are kept. Empty: ``storage`` next to ``app/``.
    # A relative path is resolved against the directory that holds ``app/``. Mount a volume here.
    storage_dir: str = ""

    # Which implementation of the central client service the API talks to. ``mock`` is an
    # in-memory catalogue of UK companies (state is per process: for development and demos).
    central_client_gateway: Literal["mock"] = "mock"
    # The other integrations (docs/modules/integrations.md), each behind its own interface; ``mock``
    # is the only implementation so far (state per process: for development and demos).
    central_user_gateway: Literal["mock"] = "mock"
    practice_management_gateway: Literal["mock"] = "mock"
    bookkeeping_gateway: Literal["mock"] = "mock"
    subscription_gateway: Literal["mock"] = "mock"
    # How long a practice's subscription status is trusted before it is asked for again.
    subscription_cache_seconds: int = Field(default=60, ge=1, le=3600)

    # Hardening: a global ceiling per member (or per address when signed out) and a cap
    # on request bodies everywhere except the upload endpoints, which have their own.
    api_rate_limit_enabled: bool = True
    api_rate_limit_user_per_minute: int = Field(default=600, ge=1)
    api_rate_limit_ip_per_minute: int = Field(default=120, ge=1)
    # Where the global ceiling is counted. "memory" costs no database round trip per request but
    # is per API process; "database" is shared by every process, at one extra statement per
    # request. (Sign-in, registration and reset limits always use the database.)
    api_rate_limit_store: Literal["memory", "database"] = "memory"
    max_request_body_kb: int = Field(default=1024, ge=1)
    # Multiplies the authentication limits (register, login, reset, ...). Only for scratch
    # stacks that register many accounts, such as the end-to-end tests; production refuses it.
    auth_rate_limit_factor: int = Field(default=1, ge=1, le=1000)

    # One practice, no public sign-up: registration and practice creation answer 403 and
    # staff accounts are made by an administrator. False makes the product multi-practice
    # SaaS, where anyone may register and create a practice.
    closed_deployment: bool = True

    worker_concurrency: int = 5
    worker_poll_interval_seconds: float = 2.0
    worker_lease_seconds: int = 300

    @field_validator("db_url", "db_direct_url")
    @classmethod
    def _normalise_db_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value():
            return None if value is None else value
        return SecretStr(normalise_db_url(value.get_secret_value()))

    @field_validator("frontend_url", "backend_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def _check_messaging(self) -> Self:
        if self.messaging_backend == "smtp" and not self.smtp_host:
            raise ValueError("MESSAGING_BACKEND=smtp needs SMTP_HOST")
        return self

    @model_validator(mode="after")
    def _check_production(self) -> Self:
        if self.app_env != "production":
            return self
        problems = []
        if self.app_debug:
            problems.append("APP_DEBUG must be false in production")
        if len(self.jwt_secret_key.get_secret_value()) < 32:
            problems.append("JWT_SECRET_KEY must be at least 32 characters in production")
        if self.smtp_host and (not self.smtp_user or not self.smtp_password.get_secret_value()):
            problems.append("SMTP_USER and SMTP_PASSWORD are required in production")
        if self.auth_rate_limit_factor != 1:
            problems.append("AUTH_RATE_LIMIT_FACTOR must be 1 in production")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def effective_messaging_backend(self) -> Literal["smtp", "mock"]:
        return self.messaging_backend or ("smtp" if self.smtp_host else "mock")

    @property
    def allowed_origins(self) -> list[str]:
        origins = [o.strip().rstrip("/") for o in self.cors_origins.split(",") if o.strip()]
        return origins or [self.frontend_url]

    @property
    def database_url(self) -> str:
        return self.db_url.get_secret_value()

    @property
    def direct_database_url(self) -> str:
        """SQLAlchemy URL for migrations; falls back to the pooled URL."""
        if self.db_direct_url and self.db_direct_url.get_secret_value():
            return self.db_direct_url.get_secret_value()
        return self.database_url

    @property
    def listen_database_url(self) -> str:
        """Plain ``postgresql://`` URL for the raw asyncpg LISTEN connection.

        From ``DB_DIRECT_URL`` (a pooler in transaction mode cannot LISTEN), falling back
        to ``DB_URL``. asyncpg's own DSN parser wants ``sslmode`` again, not ``ssl``.
        """
        parts = urlsplit(self.direct_database_url)
        query = [
            ("sslmode" if key == "ssl" else key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
        return urlunsplit(("postgresql", parts.netloc, parts.path, urlencode(query), ""))

    @property
    def storage_path(self) -> Path:
        configured = Path(self.storage_dir.strip() or "storage")
        return configured if configured.is_absolute() else PROJECT_ROOT / configured

    @property
    def refresh_cookie_name(self) -> str:
        return "__Secure-ctf_refresh" if self.cookies_secure else "ctf_refresh"

    @property
    def cookies_secure(self) -> bool:
        return self.app_env == "production" or self.frontend_url.startswith("https://")


@lru_cache
def get_settings() -> Settings:
    return Settings()
