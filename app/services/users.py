"""The current user's profile, password and sessions."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.context import RequestContext
from app.core.exceptions import (
    AuthenticationError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationError,
)
from app.core.security import hash_password, verify_password
from app.db.models import User, UserSession
from app.db.session import unit_of_work
from app.repositories.sessions import SessionRepository
from app.repositories.users import UserRepository
from app.schemas.auth import ChangePasswordRequest
from app.schemas.user import UserUpdate
from app.services import audit, jobs
from app.services.avatar_files import inspect_avatar
from app.services.storage import StorageError, discard, get_storage, new_key


class UserService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.sessions = SessionRepository(session)
        self.users = UserRepository(session)
        self.storage = get_storage()

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

    async def set_avatar(
        self,
        user: User,
        *,
        file_name: str,
        declared_type: str | None,
        data: bytes,
        ctx: RequestContext,
    ) -> User:
        """Replace the profile picture. The file is stored first and the old one removed only
        after the row points at the new one, so a failure never leaves a person without a picture
        they had, nor a row pointing at a file that is not there."""
        inspected = inspect_avatar(file_name, declared_type, data)
        key = new_key()
        try:
            # A picture belongs to a person, not to one practice: the store's namespace is the user.
            await self.storage.put(user.id, key, data)
        except StorageError as exc:
            raise ServiceUnavailableError(
                "File storage is unavailable. Please try again shortly.", code="STORAGE_UNAVAILABLE"
            ) from exc
        previous: str | None = None
        try:
            async with unit_of_work(self.session):
                user = await self.session.merge(user)
                previous = user.avatar_storage_key
                user.avatar_storage_key = key
                user.avatar_content_type = inspected.content_type
                user.avatar_updated_at = datetime.now(UTC)
                await audit.record(
                    self.session,
                    "user.avatar_changed",
                    ctx=ctx,
                    actor_user_id=user.id,
                    target_type="user",
                    target_id=user.id,
                    meta={
                        "content_type": inspected.content_type,
                        "size_bytes": inspected.size_bytes,
                    },
                )
        except BaseException:
            await discard(self.storage, user.id, [key])
            raise
        if previous:
            await discard(self.storage, user.id, [previous])
        return user

    async def clear_avatar(self, user: User, ctx: RequestContext) -> User:
        previous: str | None = None
        async with unit_of_work(self.session):
            user = await self.session.merge(user)
            previous = user.avatar_storage_key
            user.avatar_storage_key = None
            user.avatar_content_type = None
            user.avatar_updated_at = None
            if previous:
                await audit.record(
                    self.session,
                    "user.avatar_removed",
                    ctx=ctx,
                    actor_user_id=user.id,
                    target_type="user",
                    target_id=user.id,
                )
        if previous:
            await discard(self.storage, user.id, [previous])
        return user

    async def avatar_owner(self, viewer: User, user_id: uuid.UUID) -> User:
        """The person whose picture this is, when the viewer may see it: their own, or that of
        someone in a practice they belong to. Anything else is simply not found."""
        if user_id == viewer.id:
            owner: User | None = viewer
        else:
            if not await self.users.share_a_practice(viewer.id, user_id):
                raise NotFoundError("Picture not found.")
            owner = await self.users.get(user_id)
        if owner is None or owner.avatar_storage_key is None:
            raise NotFoundError("Picture not found.")
        return owner

    async def open_avatar(self, owner: User) -> AsyncIterator[bytes]:
        assert owner.avatar_storage_key is not None
        try:
            return await self.storage.open(owner.id, owner.avatar_storage_key)
        except StorageError as exc:
            raise NotFoundError("Picture not found.") from exc
