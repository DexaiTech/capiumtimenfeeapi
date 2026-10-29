"""Segment queries. Every method is scoped to one organization."""

import uuid
from collections.abc import Collection

from sqlalchemy import ColumnElement, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client, OrganizationMember, Segment, StaffSegment
from app.db.models.organization import GONE_STATUSES
from app.utils.ids import new_id


def _client_key() -> ColumnElement[str]:
    """What a client's segment is matched on: trimmed and case-folded."""
    return func.lower(func.btrim(Client.segment))


class SegmentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(
        self, organization_id: uuid.UUID, segment_id: uuid.UUID, *, lock: bool = False
    ) -> Segment | None:
        stmt = select(Segment).where(
            Segment.organization_id == organization_id, Segment.id == segment_id
        )
        if lock:
            stmt = stmt.with_for_update()
        return (
            await self.session.execute(stmt.execution_options(populate_existing=True))
        ).scalar_one_or_none()

    async def many(
        self, organization_id: uuid.UUID, segment_ids: Collection[uuid.UUID]
    ) -> list[Segment]:
        if not segment_ids:
            return []
        stmt = select(Segment).where(
            Segment.organization_id == organization_id, Segment.id.in_(list(segment_ids))
        )
        return list((await self.session.execute(stmt)).scalars())

    async def list_all(
        self, organization_id: uuid.UUID, *, is_active: bool | None = None, q: str | None = None
    ) -> list[Segment]:
        stmt = select(Segment).where(Segment.organization_id == organization_id)
        if is_active is not None:
            stmt = stmt.where(Segment.is_active.is_(is_active))
        if q:
            stmt = stmt.where(Segment.name.icontains(q, autoescape=True))
        stmt = stmt.order_by(Segment.sort_order, func.lower(Segment.name), Segment.id)
        return list(
            (await self.session.execute(stmt.execution_options(populate_existing=True))).scalars()
        )

    async def name_taken(
        self, organization_id: uuid.UUID, name: str, exclude_id: uuid.UUID | None = None
    ) -> bool:
        stmt = select(Segment.id).where(
            Segment.organization_id == organization_id,
            func.lower(Segment.name) == name.strip().lower(),
        )
        if exclude_id is not None:
            stmt = stmt.where(Segment.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def client_counts(
        self, organization_id: uuid.UUID, names: Collection[str]
    ) -> dict[str, int]:
        """Live clients per segment name (lower-cased)."""
        keys = [n.strip().lower() for n in names]
        if not keys:
            return {}
        stmt = (
            select(_client_key(), func.count())
            .where(
                Client.organization_id == organization_id,
                Client.deleted_at.is_(None),
                _client_key().in_(keys),
            )
            .group_by(_client_key())
        )
        return {row[0]: int(row[1]) for row in (await self.session.execute(stmt)).all()}

    async def staff_counts(
        self, organization_id: uuid.UUID, segment_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """Staff (not removed or left) looking after each segment."""
        if not segment_ids:
            return {}
        stmt = (
            select(StaffSegment.segment_id, func.count())
            .join(
                OrganizationMember,
                (OrganizationMember.organization_id == StaffSegment.organization_id)
                & (OrganizationMember.user_id == StaffSegment.user_id),
            )
            .where(
                StaffSegment.organization_id == organization_id,
                StaffSegment.segment_id.in_(list(segment_ids)),
                OrganizationMember.status.not_in(GONE_STATUSES),
            )
            .group_by(StaffSegment.segment_id)
        )
        return {row[0]: int(row[1]) for row in (await self.session.execute(stmt)).all()}

    async def add(self, segment: Segment) -> Segment:
        self.session.add(segment)
        await self.session.flush()
        await self.session.refresh(segment)
        return segment

    async def save(self, segment: Segment) -> Segment:
        await self.session.flush()
        await self.session.refresh(segment)
        return segment

    async def delete(self, organization_id: uuid.UUID, segment_id: uuid.UUID) -> None:
        await self.session.execute(
            delete(Segment).where(
                Segment.organization_id == organization_id, Segment.id == segment_id
            )
        )

    async def rename_on_clients(self, organization_id: uuid.UUID, old: str, new: str) -> int:
        """Give the live clients of the segment its new name; returns how many changed."""
        result = await self.session.execute(
            update(Client)
            .where(
                Client.organization_id == organization_id,
                Client.deleted_at.is_(None),
                _client_key() == old.strip().lower(),
                Client.segment != new,
            )
            .values(segment=new)
        )
        return int(getattr(result, "rowcount", 0) or 0)

    # --- Staff ------------------------------------------------------------------------

    async def member_exists(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        stmt = select(OrganizationMember.id).where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id == user_id,
        )
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def segment_ids_of(
        self, organization_id: uuid.UUID, user_id: uuid.UUID, *, lock: bool = False
    ) -> set[uuid.UUID]:
        stmt = select(StaffSegment.segment_id).where(
            StaffSegment.organization_id == organization_id, StaffSegment.user_id == user_id
        )
        if lock:
            stmt = stmt.with_for_update()
        return set((await self.session.execute(stmt)).scalars())

    async def segments_of(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> list[Segment]:
        stmt = (
            select(Segment)
            .join(
                StaffSegment,
                (StaffSegment.organization_id == Segment.organization_id)
                & (StaffSegment.segment_id == Segment.id),
            )
            .where(StaffSegment.organization_id == organization_id, StaffSegment.user_id == user_id)
            .order_by(Segment.sort_order, func.lower(Segment.name), Segment.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def link(
        self, organization_id: uuid.UUID, user_id: uuid.UUID, segment_ids: Collection[uuid.UUID]
    ) -> None:
        self.session.add_all(
            StaffSegment(
                id=new_id(), organization_id=organization_id, user_id=user_id, segment_id=s
            )
            for s in segment_ids
        )
        await self.session.flush()

    async def unlink(
        self, organization_id: uuid.UUID, user_id: uuid.UUID, segment_ids: Collection[uuid.UUID]
    ) -> None:
        if not segment_ids:
            return
        await self.session.execute(
            delete(StaffSegment).where(
                StaffSegment.organization_id == organization_id,
                StaffSegment.user_id == user_id,
                StaffSegment.segment_id.in_(list(segment_ids)),
            )
        )

    async def clients_of(self, organization_id: uuid.UUID, names: Collection[str]) -> int:
        """Live clients in any of the segments (each counted once)."""
        keys = [n.strip().lower() for n in names]
        if not keys:
            return 0
        stmt = select(func.count()).where(
            Client.organization_id == organization_id,
            Client.deleted_at.is_(None),
            _client_key().in_(keys),
        )
        return int(await self.session.scalar(stmt) or 0)
