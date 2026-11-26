"""Two-factor sign-ins in progress (``mfa_challenges``). Not practice-scoped: a challenge belongs to
an account, like a session."""

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MfaChallenge


class MfaChallengeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, challenge: MfaChallenge) -> MfaChallenge:
        self.session.add(challenge)
        await self.session.flush()
        return challenge

    async def get_for_update(self, token_hash: str) -> MfaChallenge | None:
        """The challenge with this hash, locked until the transaction ends: two submissions of one
        challenge are decided one after the other."""
        return await self.session.scalar(
            select(MfaChallenge).where(MfaChallenge.token_hash == token_hash).with_for_update()
        )

    async def revoke_open(self, user_id: uuid.UUID, now: datetime) -> int:
        """End every unanswered challenge of the account (its two-factor changed or was reset)."""
        result = await self.session.execute(
            update(MfaChallenge)
            .where(
                MfaChallenge.user_id == user_id,
                MfaChallenge.used_at.is_(None),
                MfaChallenge.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        return int(getattr(result, "rowcount", 0) or 0)
