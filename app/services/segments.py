"""Client segments (Gold, Retail, ...) and the staff who look after them.

A practice defines its segments in the settings. Clients keep the segment as text matched to a
segment ignoring case, so a value nobody defined (an old import, say) still works.

* The name is unique per practice, ignoring case: 409 ``SEGMENT_NAME_TAKEN``.
* Renaming a segment renames it on its live clients.
* A segment that clients use is deactivated, not deleted; a deactivated one is no longer
  offered for staff or clients, whoever has it keeps it.
* Reading is open to every member (the client form offers the list); writing needs
  ``settings.manage``. Linking staff to segments needs ``member.update``.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import NotFoundError, ValidationError
from app.db.models import Segment
from app.db.session import unit_of_work
from app.repositories.segments import SegmentRepository
from app.schemas.segments import (
    SegmentCreate,
    SegmentRead,
    SegmentUpdate,
    StaffSegmentsRead,
)
from app.schemas.settings import DeleteOutcome
from app.services.common import apply_changes, duplicate_error, record_audit
from app.services.policy import OrgContext


class SegmentService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.segments = SegmentRepository(session)

    async def _reads(self, ctx: OrgContext, segments: list[Segment]) -> list[SegmentRead]:
        org_id = ctx.organization_id
        clients = await self.segments.client_counts(org_id, [s.name for s in segments])
        staff = await self.segments.staff_counts(org_id, [s.id for s in segments])
        return [
            SegmentRead.from_model(
                s, client_count=clients.get(s.name.lower(), 0), staff_count=staff.get(s.id, 0)
            )
            for s in segments
        ]

    async def _read(self, ctx: OrgContext, segment: Segment) -> SegmentRead:
        [read] = await self._reads(ctx, [segment])
        return read

    # --- Reads ----------------------------------------------------------------------------

    async def list_segments(
        self, ctx: OrgContext, *, is_active: bool | None = None, q: str | None = None
    ) -> list[SegmentRead]:
        rows = await self.segments.list_all(ctx.organization_id, is_active=is_active, q=q)
        return await self._reads(ctx, rows)

    async def get(self, ctx: OrgContext, segment_id: uuid.UUID) -> SegmentRead:
        segment = await self.segments.get(ctx.organization_id, segment_id)
        if segment is None:
            raise NotFoundError("Segment not found.")
        return await self._read(ctx, segment)

    # --- Writes ---------------------------------------------------------------------------

    async def create(
        self, ctx: OrgContext, data: SegmentCreate, rctx: RequestContext
    ) -> SegmentRead:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            await self._ensure_unique(org_id, data.name, exclude_id=None)
            segment = await self.segments.add(Segment(organization_id=org_id, **data.model_dump()))
            await record_audit(
                self.session, ctx, rctx, "segment.created", "segment", segment.id,
                {"name": segment.name},
            )  # fmt: skip
            result = await self._read(ctx, segment)
        return result

    async def update(
        self, ctx: OrgContext, segment_id: uuid.UUID, data: SegmentUpdate, rctx: RequestContext
    ) -> SegmentRead:
        org_id = ctx.organization_id
        values = data.model_dump(exclude_unset=True)
        async with unit_of_work(self.session):
            segment = await self.segments.get(org_id, segment_id, lock=True)
            if segment is None:
                raise NotFoundError("Segment not found.")
            old_name = segment.name
            if "name" in values:
                await self._ensure_unique(org_id, values["name"], exclude_id=segment.id)
            changes = apply_changes(segment, values)
            segment = await self.segments.save(segment)
            meta: dict[str, object] = {"name": segment.name, "changes": changes}
            if "name" in changes:
                meta["clients_renamed"] = await self.segments.rename_on_clients(
                    org_id, old_name, segment.name
                )
            if changes:
                await record_audit(
                    self.session, ctx, rctx, "segment.updated", "segment", segment.id, meta
                )
            result = await self._read(ctx, segment)
        return result

    async def set_active(
        self, ctx: OrgContext, segment_id: uuid.UUID, active: bool, rctx: RequestContext
    ) -> SegmentRead:
        return await self.update(ctx, segment_id, SegmentUpdate(is_active=active), rctx)

    async def delete(
        self, ctx: OrgContext, segment_id: uuid.UUID, rctx: RequestContext
    ) -> DeleteOutcome:
        """Delete an unused segment; deactivate one that clients still have."""
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            segment = await self.segments.get(org_id, segment_id, lock=True)
            if segment is None:
                raise NotFoundError("Segment not found.")
            meta = {"name": segment.name}
            used = await self.segments.client_counts(org_id, [segment.name])
            if used:
                if segment.is_active:
                    segment.is_active = False
                    await self.segments.save(segment)
                    await record_audit(
                        self.session, ctx, rctx, "segment.deactivated", "segment", segment.id, meta
                    )
                return DeleteOutcome(outcome="deactivated", id=segment.id)
            await self.segments.delete(org_id, segment.id)
            await record_audit(
                self.session, ctx, rctx, "segment.deleted", "segment", segment.id, meta
            )
        return DeleteOutcome(outcome="deleted", id=segment_id)

    # --- Staff ----------------------------------------------------------------------------

    async def staff_segments(self, ctx: OrgContext, user_id: uuid.UUID) -> StaffSegmentsRead:
        org_id = ctx.organization_id
        if not await self.segments.member_exists(org_id, user_id):
            raise NotFoundError("Staff member not found.")
        rows = await self.segments.segments_of(org_id, user_id)
        return StaffSegmentsRead(
            segments=await self._reads(ctx, rows),
            client_count=await self.segments.clients_of(org_id, [s.name for s in rows]),
        )

    async def set_staff_segments(
        self,
        ctx: OrgContext,
        user_id: uuid.UUID,
        segment_ids: list[uuid.UUID],
        rctx: RequestContext,
    ) -> StaffSegmentsRead:
        """Replace the segments of a staff member. Only active segments can be added; those
        already held stay, even when deactivated."""
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            if not await self.segments.member_exists(org_id, user_id):
                raise NotFoundError("Staff member not found.")
            found = {s.id: s for s in await self.segments.many(org_id, segment_ids)}
            missing = [i for i in segment_ids if i not in found]
            if missing:
                raise ValidationError.for_field("segment_ids", "A segment does not exist.")
            current = await self.segments.segment_ids_of(org_id, user_id, lock=True)
            wanted = set(segment_ids)
            added = wanted - current
            if any(not found[i].is_active for i in added):
                raise ValidationError.for_field("segment_ids", "A segment is inactive.")
            removed = current - wanted
            await self.segments.unlink(org_id, user_id, removed)
            await self.segments.link(org_id, user_id, added)
            if added or removed:
                names = {s.id: s.name for s in await self.segments.many(org_id, current | wanted)}
                await record_audit(
                    self.session,
                    ctx,
                    rctx,
                    "staff.segments_updated",
                    "member",
                    user_id,
                    {
                        "added": sorted(names[i] for i in added),
                        "removed": sorted(names[i] for i in removed),
                    },
                )
        return await self.staff_segments(ctx, user_id)

    # --- Internals ------------------------------------------------------------------------

    async def _ensure_unique(
        self, org_id: uuid.UUID, name: str, *, exclude_id: uuid.UUID | None
    ) -> None:
        if await self.segments.name_taken(org_id, name, exclude_id=exclude_id):
            raise duplicate_error(
                "name", "A segment with this name already exists.", "SEGMENT_NAME_TAKEN"
            )
