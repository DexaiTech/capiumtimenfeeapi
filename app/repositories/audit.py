import uuid
from collections.abc import Collection, Sequence
from typing import Any

from sqlalchemy import Select, Text, and_, cast, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, OrganizationMember, User
from app.schemas.audit import AuditFilters
from app.utils.pagination import Page, PageParams, paginate


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, entry: AuditLog) -> None:
        self.session.add(entry)
        await self.session.flush()

    async def add_many(self, entries: Sequence[AuditLog]) -> None:
        self.session.add_all(entries)
        await self.session.flush()

    @staticmethod
    def _filtered(organization_id: uuid.UUID, filters: AuditFilters) -> Select[AuditLog]:
        """The entries of one organization that match the filters (unordered).

        ``action`` matches exactly, or as a prefix when it ends with a dot (``member.``).
        ``q`` is a case-insensitive text search over the action, the record type, the address,
        the people (name and email) and the recorded details. Account-level entries have no
        organization and can never match.
        """
        stmt = select(AuditLog).where(AuditLog.organization_id == organization_id)
        if filters.action:
            if filters.action.endswith("."):
                stmt = stmt.where(AuditLog.action.startswith(filters.action, autoescape=True))
            else:
                stmt = stmt.where(AuditLog.action == filters.action)
        if filters.actor_user_id is not None:
            stmt = stmt.where(AuditLog.actor_user_id == filters.actor_user_id)
        if filters.created_from is not None:
            stmt = stmt.where(AuditLog.created_at >= filters.created_from)
        if filters.created_to is not None:
            stmt = stmt.where(AuditLog.created_at <= filters.created_to)
        if filters.target_type:
            stmt = stmt.where(AuditLog.target_type == filters.target_type)
        if filters.target_id is not None:
            stmt = stmt.where(AuditLog.target_id == filters.target_id)
        if filters.q:
            stmt = stmt.where(_search(organization_id, filters.q))
        return stmt

    async def list_page(
        self, organization_id: uuid.UUID, filters: AuditFilters, params: PageParams
    ) -> Page[AuditLog]:
        """One page of the entries of one organization, newest first."""
        return await paginate(
            self.session,
            self._filtered(organization_id, filters),
            sort_column=AuditLog.created_at,
            id_column=AuditLog.id,
            params=params,
            descending=True,
        )

    async def history_page(
        self,
        organization_id: uuid.UUID,
        target_type: str,
        target_id: uuid.UUID,
        filters: AuditFilters,
        params: PageParams,
        *,
        related_key: str | None = None,
        record_types: Collection[str] | None = None,
    ) -> Page[AuditLog]:
        """The entries about one record, newest first.

        With ``related_key`` (``client_id``) the entries that name the record in their details
        are included. ``record_types`` limits the page to entries about records of those types
        (None: no limit).
        """
        about = and_(AuditLog.target_type == target_type, AuditLog.target_id == target_id)
        if related_key is not None:
            # A literal key, so the partial expression index can be used.
            named = AuditLog.meta.op("->>")(literal_column(f"'{related_key}'")) == str(target_id)
            about = or_(about, named)
        stmt = self._filtered(organization_id, filters).where(about)
        if record_types is not None:
            stmt = stmt.where(AuditLog.target_type.in_(record_types))
        return await paginate(
            self.session,
            stmt,
            sort_column=AuditLog.created_at,
            id_column=AuditLog.id,
            params=params,
            descending=True,
        )

    async def export_rows(
        self, organization_id: uuid.UUID, filters: AuditFilters, limit: int
    ) -> list[AuditLog]:
        """The newest ``limit`` entries that match, newest first."""
        stmt = (
            self._filtered(organization_id, filters)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def get_actors(self, user_ids: Collection[uuid.UUID]) -> dict[uuid.UUID, User]:
        """The users behind a page of entries, in one query. Deleted users are included."""
        if not user_ids:
            return {}
        rows = (await self.session.execute(select(User).where(User.id.in_(user_ids)))).scalars()
        return {user.id: user for user in rows}


def _search(organization_id: uuid.UUID, text: str) -> Any:
    """``text`` anywhere in what a person would look for an entry by."""
    people = (
        select(OrganizationMember.user_id)
        .join(User, User.id == OrganizationMember.user_id)
        .where(
            OrganizationMember.organization_id == organization_id,
            or_(
                User.full_name.icontains(text, autoescape=True),
                User.email.icontains(text, autoescape=True),
            ),
        )
    )
    return or_(
        AuditLog.action.icontains(text, autoescape=True),
        AuditLog.target_type.icontains(text, autoescape=True),
        func.host(AuditLog.ip_address, type_=Text).icontains(text, autoescape=True),
        cast(AuditLog.meta, Text).icontains(text, autoescape=True),
        AuditLog.actor_user_id.in_(people),
    )
