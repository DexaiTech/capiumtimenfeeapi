"""Organization queries. Membership-based lookups return the caller's membership too."""

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.permissions import OWNER_KEY, effective_permissions
from app.db.models import Organization, OrganizationMember, Role, User
from app.db.models.organization import GONE_STATUSES


def _permissions_of(member: OrganizationMember) -> frozenset[str]:
    """The member's permissions; ``role`` and ``permission_overrides`` must be loaded."""
    return effective_permissions(
        (p.code for p in member.role.permissions),
        ((o.permission_code, o.effect) for o in member.permission_overrides),
    )


class OrganizationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, organization: Organization) -> Organization:
        self.session.add(organization)
        await self.session.flush()
        return organization

    async def list_for_user(
        self, user_id: uuid.UUID
    ) -> list[tuple[Organization, Role, frozenset[str]]]:
        """Organizations the user is an *active* member of, with their role and permissions (the
        role's, with what was granted to or denied the person)."""
        stmt = (
            select(Organization, OrganizationMember)
            .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
            .where(
                Organization.deleted_at.is_(None),
                OrganizationMember.user_id == user_id,
                OrganizationMember.status == "active",
            )
            .options(
                selectinload(OrganizationMember.role).selectinload(Role.permissions),
                selectinload(OrganizationMember.permission_overrides),
            )
            .order_by(func.lower(Organization.name), Organization.name, Organization.id)
        )
        rows = (await self.session.execute(stmt)).all()
        return [(row[0], row[1].role, _permissions_of(row[1])) for row in rows]

    async def get_by_slug_for_user(
        self, slug: str, user_id: uuid.UUID
    ) -> tuple[Organization, Role, frozenset[str]] | None:
        stmt = (
            select(Organization, OrganizationMember)
            .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
            .where(
                Organization.slug == slug,
                Organization.deleted_at.is_(None),
                OrganizationMember.user_id == user_id,
                OrganizationMember.status == "active",
            )
            .options(
                selectinload(OrganizationMember.role).selectinload(Role.permissions),
                selectinload(OrganizationMember.permission_overrides),
            )
        )
        row = (await self.session.execute(stmt)).one_or_none()
        return (row[0], row[1].role, _permissions_of(row[1])) if row else None

    async def first(self) -> Organization | None:
        """The oldest live organization (the only one in a closed deployment)."""
        stmt = (
            select(Organization)
            .where(Organization.deleted_at.is_(None))
            .order_by(Organization.created_at, Organization.id)
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def lock(self, organization_id: uuid.UUID) -> Organization | None:
        """Lock the organization row until the transaction ends and return its current state.

        Serialises changes to an organization's members and roles, which is what makes the
        last-owner check safe. ``FOR NO KEY UPDATE`` does not block the foreign-key checks
        of unrelated inserts.
        """
        stmt = (
            select(Organization)
            .where(Organization.id == organization_id, Organization.deleted_at.is_(None))
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def lock_user(self, user_id: uuid.UUID) -> None:
        """Serialise organization creation per user so the ownership limit cannot be raced."""
        await self.session.execute(
            select(User.id).where(User.id == user_id).with_for_update(key_share=True)
        )

    async def count_owned(self, user_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(OrganizationMember)
            .join(Organization, Organization.id == OrganizationMember.organization_id)
            .join(Role, Role.id == OrganizationMember.role_id)
            .where(
                OrganizationMember.user_id == user_id,
                OrganizationMember.status.not_in(GONE_STATUSES),
                Role.system_key == OWNER_KEY,
                Organization.deleted_at.is_(None),
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def slug_exists(self, slug: str) -> bool:
        stmt = select(Organization.id).where(
            Organization.slug == slug, Organization.deleted_at.is_(None)
        )
        return (await self.session.execute(stmt)).first() is not None

    async def slugs_starting_with(self, base: str) -> set[str]:
        """Live slugs equal to ``base`` or of the form ``base-…``."""
        stmt = select(Organization.slug).where(
            Organization.deleted_at.is_(None),
            or_(
                Organization.slug == base, Organization.slug.startswith(f"{base}-", autoescape=True)
            ),
        )
        return set((await self.session.execute(stmt)).scalars())
