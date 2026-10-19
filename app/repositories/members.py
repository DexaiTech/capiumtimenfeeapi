"""Organization member queries. Every method is scoped to one organization."""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import contains_eager, selectinload

from app.core.permissions import OWNER_KEY
from app.db.models import OrganizationMember, Role, User
from app.db.models.organization import GONE_STATUSES
from app.utils.pagination import Page, PageParams, decode_cursor, encode_cursor


class MemberRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, member: OrganizationMember) -> OrganizationMember:
        self.session.add(member)
        await self.session.flush()
        return member

    async def get(
        self, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> OrganizationMember | None:
        """One membership in any status, read fresh, with user, role and permissions loaded."""
        stmt = (
            select(OrganizationMember)
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id == user_id,
            )
            .options(
                selectinload(OrganizationMember.user),
                selectinload(OrganizationMember.role).selectinload(Role.permissions),
            )
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_page(
        self,
        organization_id: uuid.UUID,
        *,
        statuses: Sequence[str],
        role_id: uuid.UUID | None,
        q: str | None,
        params: PageParams,
    ) -> Page[OrganizationMember]:
        """One page ordered by ``(lower(full_name), user_id)``, keyset-paginated."""
        sort_key = func.lower(User.full_name)
        stmt = (
            select(OrganizationMember, sort_key.label("sort_key"))
            .join(User, User.id == OrganizationMember.user_id)
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.status.in_(statuses),
            )
            .options(
                contains_eager(OrganizationMember.user),
                selectinload(OrganizationMember.role),
            )
        )
        if role_id is not None:
            stmt = stmt.where(OrganizationMember.role_id == role_id)
        if q:
            stmt = stmt.where(
                or_(
                    User.full_name.icontains(q, autoescape=True),
                    User.email.icontains(q, autoescape=True),
                    OrganizationMember.work_email.icontains(q, autoescape=True),
                )
            )
        if params.cursor:
            value, last_user_id = decode_cursor(params.cursor)
            stmt = stmt.where(
                tuple_(sort_key, OrganizationMember.user_id) > (str(value), last_user_id)
            )
        stmt = stmt.order_by(sort_key, OrganizationMember.user_id).limit(params.limit + 1)
        rows = (await self.session.execute(stmt)).all()
        next_cursor = None
        if len(rows) > params.limit:
            rows = rows[: params.limit]
            next_cursor = encode_cursor(rows[-1][1], rows[-1][0].user_id)
        return Page(items=[row[0] for row in rows], next_cursor=next_cursor)

    async def count_active_owners(self, organization_id: uuid.UUID) -> int:
        """Active Owners who can sign in (staff without a login cannot administer anything)."""
        stmt = (
            select(func.count())
            .select_from(OrganizationMember)
            .join(Role, Role.id == OrganizationMember.role_id)
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.status == "active",
                OrganizationMember.login_enabled.is_(True),
                Role.system_key == OWNER_KEY,
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def initials_taken(
        self,
        organization_id: uuid.UUID,
        initials: str,
        *,
        exclude_user_id: uuid.UUID | None = None,
    ) -> bool:
        """Whether a non-removed member of the practice already uses these initials (any case).

        Mirrors the partial unique index ``uq_organization_members_organization_id_initials``.
        """
        stmt = select(OrganizationMember.user_id).where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.status != "removed",
            OrganizationMember.initials.is_not(None),
            func.upper(OrganizationMember.initials) == initials.upper(),
        )
        if exclude_user_id is not None:
            stmt = stmt.where(OrganizationMember.user_id != exclude_user_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def other_organizations(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """Whether the person is also a (current) member of another organization."""
        stmt = select(OrganizationMember.organization_id).where(
            OrganizationMember.user_id == user_id,
            OrganizationMember.organization_id != organization_id,
            OrganizationMember.status.not_in(GONE_STATUSES),
        )
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def clear_manager(self, organization_id: uuid.UUID, manager_user_id: uuid.UUID) -> int:
        """Nobody keeps reporting to a member who has left. Returns how many were cleared."""
        result = await self.session.execute(
            update(OrganizationMember)
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.manager_user_id == manager_user_id,
            )
            .values(manager_user_id=None)
        )
        return int(getattr(result, "rowcount", 0) or 0)
