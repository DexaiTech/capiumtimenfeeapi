import uuid

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OrganizationMember, User


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, user_id: uuid.UUID) -> User | None:
        stmt = select(User).where(User.id == user_id, User.deleted_at.is_(None))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def share_a_practice(self, user_id: uuid.UUID, other_id: uuid.UUID) -> bool:
        """Both people are members of one practice."""
        mine = OrganizationMember.__table__.alias("mine")
        theirs = OrganizationMember.__table__.alias("theirs")
        stmt = select(
            exists().where(
                mine.c.user_id == user_id,
                theirs.c.user_id == other_id,
                mine.c.organization_id == theirs.c.organization_id,
            )
        )
        return bool((await self.session.execute(stmt)).scalar())

    async def get_by_email(self, email: str) -> User | None:
        stmt = select(User).where(
            func.lower(User.email) == email.lower(), User.deleted_at.is_(None)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add(self, user: User) -> User:
        self.session.add(user)
        await self.session.flush()
        return user
