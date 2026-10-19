"""The current user's profile, password and sessions."""

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.context import RequestContext
from app.core.exceptions import AuthenticationError, NotFoundError, ValidationError
from app.core.security import hash_password, verify_password
from app.db.models import User, UserSession
from app.db.session import unit_of_work
from app.repositories.sessions import SessionRepository
from app.schemas.auth import ChangePasswordRequest
from app.schemas.user import UserUpdate
from app.services import audit, jobs


class UserService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.sessions = SessionRepository(session)

    async def update_profile(self, user: User, data: UserUpdate) -> User:
        async with unit_of_work(self.session):
            user = await self.session.merge(user)
            for field, value in data.model_dump(exclude_unset=True, exclude_none=True).items():
                setattr(user, field, value)
        return user

    async def change_password(
        self,
        user: User,
        data: ChangePasswordRequest,
        current_session_id: uuid.UUID,
        ctx: RequestContext,
    ) -> None:
        valid, _ = await verify_password(data.current_password, user.password_hash)
        if not valid:
            # 400, not 401: the caller is authenticated and must not be sent to refresh.
            raise AuthenticationError(
                "Your current password is incorrect.",
                code="AUTH_INVALID_CREDENTIALS",
                status_code=400,
            )
        if user.must_change_password and data.password == data.current_password:
            raise ValidationError.for_field(
                "password", "Choose a password different from the temporary one."
            )
        password_hash = await hash_password(data.password)
        async with unit_of_work(self.session):
            user = await self.session.merge(user)
            user.password_hash = password_hash
            # Choosing their own password ends the "temporary password" state.
            user.must_change_password = False
            # Other sessions are revoked; the session check rejects their access tokens.
            await self.sessions.revoke_all_for_user(
                user.id, "password_change", datetime.now(UTC), except_id=current_session_id
            )
            await jobs.enqueue(
                self.session,
                "send_email",
                {"template": "password_changed", "to": user.email, "name": user.full_name},
            )
            await audit.record(
                self.session,
                "auth.password_changed",
                ctx=ctx,
                actor_user_id=user.id,
                target_type="user",
                target_id=user.id,
            )

    async def list_sessions(self, user: User) -> list[UserSession]:
        return await self.sessions.list_active_sessions(user.id)

    async def revoke_session(self, user: User, session_id: uuid.UUID, ctx: RequestContext) -> None:
        async with unit_of_work(self.session):
            # Scoped to the caller: another user's session id is simply not found.
            target = await self.sessions.get_active_session(session_id, user.id)
            if target is None:
                raise NotFoundError("Session not found.")
            await self.sessions.revoke_session(target.id, "user", datetime.now(UTC))
            await audit.record(
                self.session,
                "auth.session_revoked",
                ctx=ctx,
                actor_user_id=user.id,
                target_type="session",
                target_id=target.id,
            )
