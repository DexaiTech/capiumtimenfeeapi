"""Practices (organizations): creation with the system roles and defaults, settings and
soft deletion."""

import re
import unicodedata
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.permissions import OWNER_KEY, SYSTEM_ROLES
from app.db.models import Organization, OrganizationMember, Role, User
from app.db.session import unit_of_work
from app.repositories.members import MemberRepository
from app.repositories.organizations import OrganizationRepository
from app.repositories.roles import RoleRepository
from app.schemas.organization import OrganizationCreate, OrganizationDelete, OrganizationUpdate
from app.services import audit, policy
from app.services.policy import OrgContext
from app.services.practice_defaults import seed_practice_defaults
from app.services.staff_codes import next_staff_code
from app.utils.ids import new_id

MAX_OWNED_ORGANIZATIONS = 10
SLUG_MAX_LENGTH = 40
SLUG_MIN_LENGTH = 3
_SLUG_ATTEMPTS = 5


def slugify(name: str) -> str:
    """A valid slug derived from a name: ASCII, lower case, hyphen-separated, 3 to 40 long."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    slug = slug[:SLUG_MAX_LENGTH].strip("-")
    if len(slug) < SLUG_MIN_LENGTH:
        slug = f"{slug}-org" if slug else "org"
    return slug


def _with_suffix(base: str, number: int) -> str:
    suffix = f"-{number}"
    return base[: SLUG_MAX_LENGTH - len(suffix)].rstrip("-") + suffix


async def lock_and_refresh(session: AsyncSession, ctx: OrgContext, *codes: str) -> OrgContext:
    """Serialise a change to the organization's members or roles and re-check the caller.

    Takes the organization row lock, then re-reads the caller's membership, so every rule
    that follows is decided on state that cannot change before the transaction ends. Must
    run inside a unit of work.
    """
    organization = await OrganizationRepository(session).lock(ctx.organization_id)
    member = await MemberRepository(session).get(ctx.organization_id, ctx.user.id)
    if organization is None or member is None or member.status != "active":
        raise NotFoundError("Organization not found.")
    fresh = replace(
        ctx,
        organization=organization,
        member=member,
        role=member.role,
        permissions=frozenset(p.code for p in member.role.permissions),
    )
    policy.ensure(fresh, *codes)
    return fresh


class OrganizationService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.organizations = OrganizationRepository(session)
        self.members = MemberRepository(session)
        self.roles = RoleRepository(session)

    async def list_mine(self, user: User) -> list[tuple[Organization, Role]]:
        return await self.organizations.list_for_user(user.id)

    async def first_organization(self) -> Organization | None:
        return await self.organizations.first()

    async def get_by_slug(self, user: User, slug: str) -> tuple[Organization, Role]:
        found = await self.organizations.get_by_slug_for_user(slug, user.id)
        if found is None:
            # Unknown slug and "not a member" are indistinguishable to the caller.
            raise NotFoundError("Organization not found.")
        return found

    async def create(
        self, user: User, data: OrganizationCreate, rctx: RequestContext
    ) -> tuple[Organization, Role]:
        async with unit_of_work(self.session):
            await self.organizations.lock_user(user.id)
            if await self.organizations.count_owned(user.id) >= MAX_OWNED_ORGANIZATIONS:
                raise ConflictError(
                    f"You can own at most {MAX_OWNED_ORGANIZATIONS} practices.",
                    details={"code": "organization_limit", "limit": MAX_OWNED_ORGANIZATIONS},
                )
            organization = await self._insert(user, data)
            owner_role = await self._copy_system_roles(organization)
            await seed_practice_defaults(self.session, organization)
            await self.members.add(
                OrganizationMember(
                    organization_id=organization.id,
                    user_id=user.id,
                    role_id=owner_role.id,
                    staff_code=await next_staff_code(self.session, organization.id),
                    work_email=user.email,
                    working_minutes_per_day=organization.minutes_per_day,
                    working_days=list(organization.working_days),
                )
            )
            await audit.record(
                self.session,
                "organization.created",
                ctx=rctx,
                actor_user_id=user.id,
                organization_id=organization.id,
                target_type="organization",
                target_id=organization.id,
                meta={"name": organization.name, "slug": organization.slug},
            )
        return organization, owner_role

    async def _insert(self, user: User, data: OrganizationCreate) -> Organization:
        if data.slug is not None:
            if await self.organizations.slug_exists(data.slug):
                raise ConflictError(
                    "This slug is already taken.",
                    details=[
                        {
                            "field": "slug",
                            "code": "duplicate",
                            "message": "This slug is already taken.",
                        }
                    ],
                )
            # A concurrent request taking the same slug is reported by the unique index.
            return await self._add(user, data, data.slug)

        base = slugify(data.name)
        for _ in range(_SLUG_ATTEMPTS):
            taken = await self.organizations.slugs_starting_with(base)
            slug, number = base, 1
            while slug in taken:
                number += 1
                slug = _with_suffix(base, number)
            try:
                # The savepoint keeps the transaction usable if another request won the slug.
                async with self.session.begin_nested():
                    return await self._add(user, data, slug)
            except IntegrityError:
                continue
        raise ConflictError("Could not find a free slug. Choose one yourself.")

    async def _add(self, user: User, data: OrganizationCreate, slug: str) -> Organization:
        organization = await self.organizations.add(
            Organization(
                id=new_id(),
                name=data.name,
                slug=slug,
                timezone=data.timezone or user.timezone,
                currency=data.currency,
                vat_registered=data.vat_registered,
                created_by=user.id,
            )
        )
        await self.session.refresh(organization)
        return organization

    async def _copy_system_roles(self, organization: Organization) -> Role:
        """Give the organization its own copy of every system role. Returns the Owner role."""
        by_code = {p.code: p for p in await self.roles.list_permissions()}
        owner: Role | None = None
        for definition in SYSTEM_ROLES:
            role = Role(
                id=new_id(),
                organization_id=organization.id,
                name=definition.name,
                system_key=definition.key,
                description=definition.description,
                permissions=[by_code[code] for code in sorted(definition.permissions)],
            )
            self.session.add(role)
            if definition.key == OWNER_KEY:
                owner = role
        await self.session.flush()
        assert owner is not None
        return owner

    async def update(
        self, ctx: OrgContext, data: OrganizationUpdate, rctx: RequestContext
    ) -> Organization:
        async with unit_of_work(self.session):
            ctx = await lock_and_refresh(self.session, ctx, "organization.update")
            organization = ctx.organization
            changes: dict[str, list[Any]] = {}
            for field, value in data.model_dump(exclude_unset=True).items():
                old = getattr(organization, field)
                if (list(old) if isinstance(old, list) else old) != value:
                    changes[field] = [old, value]
                    setattr(organization, field, value)
            if changes:
                await self.session.flush()
                await audit.record(
                    self.session,
                    "organization.updated",
                    ctx=rctx,
                    actor_user_id=ctx.user.id,
                    organization_id=organization.id,
                    target_type="organization",
                    target_id=organization.id,
                    meta={"changes": changes},
                )
        return organization

    async def delete(self, ctx: OrgContext, data: OrganizationDelete, rctx: RequestContext) -> None:
        async with unit_of_work(self.session):
            ctx = await lock_and_refresh(self.session, ctx, "organization.delete")
            organization = ctx.organization
            if data.confirm_name != organization.name:
                raise ValidationError.for_field(
                    "confirm_name", "Type the practice name exactly to confirm."
                )
            organization.deleted_at = datetime.now(UTC)
            await self.session.flush()
            await audit.record(
                self.session,
                "organization.deleted",
                ctx=rctx,
                actor_user_id=ctx.user.id,
                organization_id=organization.id,
                target_type="organization",
                target_id=organization.id,
                meta={"name": organization.name, "slug": organization.slug},
            )
