import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EmailVerificationToken, PasswordResetToken

type OneTimeToken = EmailVerificationToken | PasswordResetToken


class AuthTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, token: OneTimeToken) -> None:
        self.session.add(token)
        await self.session.flush()

    async def get_usable[T: (EmailVerificationToken, PasswordResetToken)](
        self, model: type[T], token_hash: str
    ) -> T | None:
        """An unused, unexpired token, locked so it cannot be consumed twice."""
        stmt = (
            select(model)
            .where(
                model.token_hash == token_hash,
                model.used_at.is_(None),
                model.expires_at > func.now(),
            )
            .with_for_update()
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def invalidate_for_user(
        self, model: type[OneTimeToken], user_id: uuid.UUID, now: datetime
    ) -> None:
        await self.session.execute(
            update(model)
            .where(model.user_id == user_id, model.used_at.is_(None))
            .values(used_at=now)
        )
