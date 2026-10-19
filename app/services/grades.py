"""Staff grades (docs/modules/team-rates.md, "Grades").

A grade names a level of staff (Partner, Manager, ...) with default cost and charge-out rates
and a default chargeable target. The default rates are the baseline of the grade's rate level:
dated rate rules for the grade override them while in force. Changing a default rate is
recorded in the rate history like any other rate change, and never rewrites recorded time.

* Name and code are unique per practice (case-insensitive): 409 ``GRADE_NAME_TAKEN`` /
  ``GRADE_CODE_TAKEN``.
* A grade held by staff (whatever their status) or named by a rate is deactivated, not deleted.
* Writing needs ``rate.manage``; the cost default is shown only with ``rate.read``.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import NotFoundError
from app.db.models import Grade, RateChange
from app.db.session import unit_of_work
from app.repositories.grades import GradeFilters, GradeRepository
from app.repositories.rate_history import RateHistoryRepository
from app.schemas.grades import GradeCreate, GradeRead, GradeUpdate
from app.schemas.settings import DeleteOutcome
from app.services import policy
from app.services.common import apply_changes, duplicate_error, record_audit
from app.services.policy import OrgContext
from app.utils.pagination import Page, PageParams

# Grade column -> the kind of rate it is the default of.
_DEFAULT_RATES = {"default_cost_rate": "cost", "default_charge_rate": "charge"}


class GradeService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.grades = GradeRepository(session)
        self.history = RateHistoryRepository(session)

    @staticmethod
    def can_see_costs(ctx: OrgContext) -> bool:
        return policy.has_permission(ctx, "rate.read")

    async def _reads(self, ctx: OrgContext, grades: list[Grade]) -> list[GradeRead]:
        ids = [g.id for g in grades]
        counts = await self.grades.staff_counts(ctx.organization_id, ids)
        used = await self.grades.used_ids(ctx.organization_id, ids)
        show_cost = self.can_see_costs(ctx)
        return [
            GradeRead.from_model(
                g, staff_count=counts.get(g.id, 0), can_delete=g.id not in used, show_cost=show_cost
            )
            for g in grades
        ]

    async def _read(self, ctx: OrgContext, grade: Grade) -> GradeRead:
        [read] = await self._reads(ctx, [grade])
        return read

    # --- Reads ----------------------------------------------------------------------------

    async def list_grades(
        self,
        ctx: OrgContext,
        filters: GradeFilters,
        *,
        sort: str,
        descending: bool,
        params: PageParams,
    ) -> Page[GradeRead]:
        page = await self.grades.list_page(
            ctx.organization_id, filters, sort=sort, descending=descending, params=params
        )
        return Page(items=await self._reads(ctx, page.items), next_cursor=page.next_cursor)

    async def get(self, ctx: OrgContext, grade_id: uuid.UUID) -> GradeRead:
        grade = await self.grades.get(ctx.organization_id, grade_id)
        if grade is None:
            raise NotFoundError("Grade not found.")
        return await self._read(ctx, grade)

    # --- Writes ---------------------------------------------------------------------------

    async def create(self, ctx: OrgContext, data: GradeCreate, rctx: RequestContext) -> GradeRead:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            await self._ensure_unique(org_id, data.name, data.code, exclude_id=None)
            grade = await self.grades.add(Grade(organization_id=org_id, **data.model_dump()))
            await self._log_defaults(ctx, grade, {}, create=True)
            await record_audit(
                self.session,
                ctx,
                rctx,
                "grade.created",
                "grade",
                grade.id,
                {"code": grade.code, "name": grade.name},
            )
            result = await self._read(ctx, grade)
        return result

    async def update(
        self, ctx: OrgContext, grade_id: uuid.UUID, data: GradeUpdate, rctx: RequestContext
    ) -> GradeRead:
        org_id = ctx.organization_id
        values = data.model_dump(exclude_unset=True)
        async with unit_of_work(self.session):
            grade = await self.grades.get(org_id, grade_id, lock=True)
            if grade is None:
                raise NotFoundError("Grade not found.")
            await self._ensure_unique(
                org_id, values.get("name"), values.get("code"), exclude_id=grade.id
            )
            changes = apply_changes(grade, values)
            grade = await self.grades.save(grade)
            if changes:
                await self._log_defaults(ctx, grade, changes, create=False)
                await record_audit(
                    self.session,
                    ctx,
                    rctx,
                    "grade.updated",
                    "grade",
                    grade.id,
                    {"code": grade.code, "name": grade.name, "changes": changes},
                )
            result = await self._read(ctx, grade)
        return result

    async def set_active(
        self, ctx: OrgContext, grade_id: uuid.UUID, active: bool, rctx: RequestContext
    ) -> GradeRead:
        return await self.update(ctx, grade_id, GradeUpdate(is_active=active), rctx)

    async def delete(
        self, ctx: OrgContext, grade_id: uuid.UUID, rctx: RequestContext
    ) -> DeleteOutcome:
        """Delete an unused grade; deactivate one that staff or rates refer to."""
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            grade = await self.grades.get(org_id, grade_id, lock=True)
            if grade is None:
                raise NotFoundError("Grade not found.")
            meta = {"code": grade.code, "name": grade.name}
            if await self.grades.is_used(org_id, grade.id):
                if grade.is_active:
                    grade.is_active = False
                    await self.grades.save(grade)
                    await record_audit(
                        self.session, ctx, rctx, "grade.deactivated", "grade", grade.id, meta
                    )
                return DeleteOutcome(outcome="deactivated", id=grade.id)
            await self.grades.delete(org_id, grade.id)
            await record_audit(self.session, ctx, rctx, "grade.deleted", "grade", grade.id, meta)
        return DeleteOutcome(outcome="deleted", id=grade_id)

    # --- Internals ------------------------------------------------------------------------

    async def _ensure_unique(
        self, org_id: uuid.UUID, name: str | None, code: str | None, *, exclude_id: uuid.UUID | None
    ) -> None:
        if code is not None and await self.grades.code_taken(org_id, code, exclude_id=exclude_id):
            raise duplicate_error(
                "code", "A grade with this code already exists.", "GRADE_CODE_TAKEN"
            )
        if name is not None and await self.grades.name_taken(org_id, name, exclude_id=exclude_id):
            raise duplicate_error(
                "name", "A grade with this name already exists.", "GRADE_NAME_TAKEN"
            )

    async def _log_defaults(
        self,
        ctx: OrgContext,
        grade: Grade,
        changes: dict[str, list[object]],
        *,
        create: bool,
    ) -> None:
        """Append the change of each default rate to the rate history."""
        for column, kind in _DEFAULT_RATES.items():
            new = getattr(grade, column)
            if create:
                old = None
                if new is None:
                    continue
            elif column in changes:
                old = changes[column][0]
            else:
                continue
            action = "created" if old is None else "deleted" if new is None else "updated"
            await self.history.add(
                RateChange(
                    organization_id=ctx.organization_id,
                    rule_id=None,
                    action=action,
                    kind=kind,
                    level="grade",
                    grade_id=grade.id,
                    old=None if old is None else {"rate": old, "default": True},
                    new=None if new is None else {"rate": new, "default": True},
                    actor_user_id=ctx.user.id,
                    source="grade_default",
                )
            )
