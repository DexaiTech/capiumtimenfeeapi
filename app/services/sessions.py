"""Session and refresh-token lifecycle: create, rotate with reuse detection, revoke."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.context import RequestContext
from app.core.security import generate_token, hash_token
from app.db.models import RefreshToken, User, UserSession
from app.repositories.sessions import SessionRepository
from app.repositories.users import UserRepository
from app.utils.ids import new_id

_USER_AGENT_MAX = 400


class RotationStatus(Enum):
    OK = "ok"
    INVALID = "invalid"
    REUSED = "reused"


@dataclass(slots=True)
class Rotation:
    status: RotationStatus
    user: User | None = None
    session: UserSession | None = None
    refresh_token: str | None = None


class SessionService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.settings = settings
        self.sessions = SessionRepository(session)
        self.users = UserRepository(session)

    async def create(self, user: User, ctx: RequestContext) -> tuple[UserSession, str]:
        now = datetime.now(UTC)
        user_session = await self.sessions.add_session(
            UserSession(
                id=new_id(),
                user_id=user.id,
                ip_address=ctx.ip_address,
                user_agent=(ctx.user_agent or "")[:_USER_AGENT_MAX] or None,
                last_used_at=now,
                expires_at=now + timedelta(days=self.settings.refresh_token_expire_days),
            )
        )
        _, raw = await self._issue(user_session)
        return user_session, raw

    async def _issue(self, user_session: UserSession) -> tuple[RefreshToken, str]:
        raw = generate_token()
        token = await self.sessions.add_refresh_token(
            RefreshToken(
                id=new_id(),
                session_id=user_session.id,
                token_hash=hash_token(raw),
                expires_at=user_session.expires_at,
            )
        )
        return token, raw

    async def rotate(self, raw_token: str) -> Rotation:
        """Exchange a refresh token for a new one. Must run inside a unit of work."""
        now = datetime.now(UTC)
        token = await self.sessions.get_refresh_token_for_update(hash_token(raw_token))
        if token is None or token.expires_at <= now:
            return Rotation(RotationStatus.INVALID)
        user_session = await self.sessions.get_session(token.session_id)
        if (
            user_session is None
            or user_session.revoked_at is not None
            or user_session.expires_at <= now
        ):
            return Rotation(RotationStatus.INVALID)
        user = await self.users.get(user_session.user_id)
        if user is None or not user.is_active:
            return Rotation(RotationStatus.INVALID)

        if token.used_at is not None:
            grace = timedelta(seconds=self.settings.refresh_grace_seconds)
            if now - token.used_at > grace:
                # A used token outside the grace window means a copy exists elsewhere.
                await self.sessions.revoke_session(user_session.id, "reuse_detected", now)
                return Rotation(RotationStatus.REUSED, user=user, session=user_session)
            # Two tabs or a retried request: treat as the same refresh.
            _, raw = await self._issue(user_session)
        else:
            successor, raw = await self._issue(user_session)
            token.used_at = now
            token.replaced_by_id = successor.id
        user_session.last_used_at = now
        return Rotation(RotationStatus.OK, user=user, session=user_session, refresh_token=raw)

    async def revoke_by_token(self, raw_token: str) -> UserSession | None:
        token = await self.sessions.get_refresh_token_for_update(hash_token(raw_token))
        if token is None:
            return None
        user_session = await self.sessions.get_session(token.session_id)
        if user_session is None:
            return None
        await self.sessions.revoke_session(user_session.id, "logout", datetime.now(UTC))
        return user_session

    async def revoke_all(
        self, user_id: uuid.UUID, reason: str, *, except_id: uuid.UUID | None = None
    ) -> int:
        return await self.sessions.revoke_all_for_user(
            user_id, reason, datetime.now(UTC), except_id=except_id
        )
