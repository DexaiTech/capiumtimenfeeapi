import uuid
from datetime import datetime

from sqlalchemy import and_, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RefreshToken, User, UserSession


class SessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add_session(self, user_session: UserSession) -> UserSession:
        self.session.add(user_session)
        await self.session.flush()
        return user_session

    async def add_refresh_token(self, token: RefreshToken) -> RefreshToken:
        self.session.add(token)
        await self.session.flush()
        return token

    async def get_session(self, session_id: uuid.UUID) -> UserSession | None:
        return await self.session.get(UserSession, session_id)

    async def get_active_session(
        self, session_id: uuid.UUID, user_id: uuid.UUID
    ) -> UserSession | None:
        stmt = select(UserSession).where(
            UserSession.id == session_id,
            UserSession.user_id == user_id,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > func.now(),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_caller(
        self, user_id: uuid.UUID, session_id: uuid.UUID
    ) -> tuple[User, UserSession | None] | None:
        """The user and, when it is still live, the session of an access token: one statement
        instead of two (every authenticated request pays this). ``None`` for an unknown or
        deleted user; the session is ``None`` when it was revoked, expired or is someone else's."""
        stmt = (
            select(User, UserSession)
            .outerjoin(
                UserSession,
                and_(
                    UserSession.id == session_id,
                    UserSession.user_id == User.id,
                    UserSession.revoked_at.is_(None),
                    UserSession.expires_at > func.now(),
                ),
            )
            .where(User.id == user_id, User.deleted_at.is_(None))
        )
        row = (await self.session.execute(stmt)).one_or_none()
        return (row[0], row[1]) if row else None

    async def list_active_sessions(self, user_id: uuid.UUID) -> list[UserSession]:
        stmt = (
            select(UserSession)
            .where(
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > func.now(),
            )
            .order_by(UserSession.last_used_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars())

    async def get_refresh_token_for_update(self, token_hash: str) -> RefreshToken | None:
        """Locks the row so two concurrent refreshes of one token are serialised."""
        stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def revoke_session(self, session_id: uuid.UUID, reason: str, now: datetime) -> bool:
        stmt = (
            update(UserSession)
            .where(UserSession.id == session_id, UserSession.revoked_at.is_(None))
            .values(revoked_at=now, revoked_reason=reason)
        )
        result: CursorResult[tuple[()]] = await self.session.execute(stmt)  # type: ignore[assignment]
        return result.rowcount > 0

    async def revoke_all_for_user(
        self, user_id: uuid.UUID, reason: str, now: datetime, *, except_id: uuid.UUID | None = None
    ) -> int:
        stmt = (
            update(UserSession)
            .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
            .values(revoked_at=now, revoked_reason=reason)
        )
        if except_id is not None:
            stmt = stmt.where(UserSession.id != except_id)
        result: CursorResult[tuple[()]] = await self.session.execute(stmt)  # type: ignore[assignment]
        return result.rowcount
