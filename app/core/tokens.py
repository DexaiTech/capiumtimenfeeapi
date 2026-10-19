"""Access-token (JWT) encoding and decoding."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from app.core.config import Settings
from app.core.exceptions import AuthenticationError


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    user_id: uuid.UUID
    session_id: uuid.UUID
    issued_at: int
    expires_at: int


def create_access_token(
    settings: Settings, *, user_id: uuid.UUID, session_id: uuid.UUID
) -> tuple[str, int]:
    """Return ``(token, expires_in_seconds)``. The token carries identity only."""
    now = datetime.now(UTC)
    lifetime = timedelta(minutes=settings.access_token_expire_minutes)
    payload = {
        "sub": str(user_id),
        "sid": str(session_id),
        "iat": int(now.timestamp()),
        "exp": int((now + lifetime).timestamp()),
        "typ": "access",
    }
    token = jwt.encode(
        payload, settings.jwt_secret_key.get_secret_value(), algorithm=settings.jwt_algorithm
    )
    return token, int(lifetime.total_seconds())


def decode_access_token(settings: Settings, token: str) -> AccessTokenClaims:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key.get_secret_value(),
            # The algorithm is fixed by configuration, never taken from the token header.
            algorithms=[settings.jwt_algorithm],
            options={"require": ["sub", "sid", "iat", "exp", "typ"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Your session has expired.", code="AUTH_TOKEN_EXPIRED") from exc
    except jwt.PyJWTError as exc:
        raise AuthenticationError("Invalid access token.", code="AUTH_TOKEN_INVALID") from exc
    if payload.get("typ") != "access":
        raise AuthenticationError("Invalid access token.", code="AUTH_TOKEN_INVALID")
    try:
        return AccessTokenClaims(
            user_id=uuid.UUID(payload["sub"]),
            session_id=uuid.UUID(payload["sid"]),
            issued_at=int(payload["iat"]),
            expires_at=int(payload["exp"]),
        )
    except (ValueError, TypeError) as exc:
        raise AuthenticationError("Invalid access token.", code="AUTH_TOKEN_INVALID") from exc
