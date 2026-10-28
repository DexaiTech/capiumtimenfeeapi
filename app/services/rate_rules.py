"""Dated rate rules: charge-out and cost rates by client, service, staff member and grade.

Rules (docs/domain.md, "Rates"):

* A rule applies from ``effective_from`` to ``effective_to`` inclusive (open-ended when null).
  The rules of one kind and scope never overlap: 409 ``RATE_OVERLAP`` (the database enforces it
  with an exclusion constraint, so concurrent requests cannot both succeed).
* A rule that has been used (time falls inside its scope and period) cannot be deleted (409
  ``RATE_IN_USE``), but its amount and both dates can change, so a rate added with the wrong
  start day can be corrected afterwards. Unused rules can be changed and deleted freely.
* Recorded time keeps the rate it was saved with: changing a rule, its start day included, never
  rewrites an entry (use re-rating for unbilled time: it takes the rate from the ladder as it is
  now, so nobody has to enter the time again).
* Every create, change, end-date and delete is appended to ``rate_changes`` (the history) and
  to the audit log in the same transaction.
"""

import uuid
from dataclasses import replace
from datetime import date, timedelta
from typing import Any

from asyncpg.exceptions import ExclusionViolationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.db.models import RateChange, RateRule
from app.db.models.organization import GONE_STATUSES
from app.db.session import unit_of_work
from app.repositories.clients import ClientRepository
from app.repositories.grades import GradeRepository
from app.repositories.members import MemberRepository
from app.repositories.rate_history import ChangeFilters, ChangeView, RateHistoryRepository
from app.repositories.rate_rules import (
    RateRuleRepository,
    RuleFilters,
    RuleView,
    Scope,
    level_for,
    scope_of,
)
from app.repositories.settings import ServiceRepository
from app.schemas.rate_rules import RateRuleCreate, RateRuleUpdate
from app.services import audit, policy
from app.services.policy import OrgContext
from app.utils.pagination import Page, PageParams
from app.utils.practice_dates import end_of_day_utc, start_of_day_utc, today_in

VALID_SCOPES = (
    "client + service + staff, client + service, client + staff, a client (everyone), "
    "staff + service, service + grade, a staff member or a grade"
)


def is_exclusion_violation(exc: IntegrityError) -> bool:
    cause = exc.orig.__cause__ if exc.orig is not None else None
    return isinstance(cause, ExclusionViolationError)


def overlap_error(conflicts: list[RateRule] | None = None) -> ConflictError:
    message = "Another rate of the same kind already covers part of this period."
    return ConflictError(
        message,
        code="RATE_OVERLAP",
        details={
            "conflicts": [
                {
                    "id": str(r.id),
                    "effective_from": r.effective_from.isoformat(),
                    "effective_to": r.effective_to.isoformat() if r.effective_to else None,
                }
                for r in conflicts or []
            ]
        },
    )


def in_use_error(message: str) -> ConflictError:
    return ConflictError(message, code="RATE_IN_USE")


def snapshot(rule: RateRule) -> dict[str, Any]:
    """The values a history row keeps of a rule."""
    return {
        "rate": rule.rate,
        "effective_from": rule.effective_from.isoformat(),
        "effective_to": rule.effective_to.isoformat() if rule.effective_to else None,
    }


def _closable(overlaps: list[RateRule], start: date, end: date | None) -> bool:
    """Whether the new period only overlaps the tail of one earlier rule, which can then be
    shortened (never split or swallowed)."""
    if len(overlaps) != 1:
        return False
    other = overlaps[0]
    if other.effective_from >= start:
        return False
    if other.effective_to is None:
        return end is None
    return end is None or other.effective_to <= end


def rule_meta(view: RuleView) -> dict[str, Any]:
    rule = view.rule
    return {
        "kind": rule.kind,
        "level": rule.level,
        "client_id": str(rule.client_id) if rule.client_id else None,
        "client_name": view.client_name,
        "service_id": str(rule.service_id) if rule.service_id else None,
        "service_name": view.service_name,
        "user_id": str(rule.user_id) if rule.user_id else None,
        "member_name": view.user_name,
        "grade_id": str(rule.grade_id) if rule.grade_id else None,
        "grade_name": view.grade_name,
        "rate": rule.rate,
        "effective_from": rule.effective_from.isoformat(),
        "effective_to": rule.effective_to.isoformat() if rule.effective_to else None,
    }


class RateRuleService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.rules = RateRuleRepository(session)
        self.history = RateHistoryRepository(session)
        self.clients = ClientRepository(session)
        self.services = ServiceRepository(session)
        self.members = MemberRepository(session)
        self.grades = GradeRepository(session)

    @staticmethod
    def can_see_costs(ctx: OrgContext) -> bool:
        """Cost rates are shown only with ``rate.read`` (``rate.manage`` does not imply it)."""
        return policy.has_permission(ctx, "rate.read")

    # --- Reads ----------------------------------------------------------------------------

    async def list_rules(
        self,
        ctx: OrgContext,
        filters: RuleFilters,
        *,
        sort: str,
        descending: bool,
        params: PageParams,
    ) -> tuple[Page[RuleView], date]:
        today = today_in(ctx.organization.timezone)
        page = await self.rules.list_page(
            ctx.organization_id,
            replace(filters, today=today),
            sort=sort,
            descending=descending,
            params=params,
        )
        return page, today

    async def get(self, ctx: OrgContext, rule_id: uuid.UUID) -> RuleView:
        view = await self.rules.view(ctx.organization_id, rule_id)
        if view is None:
            raise NotFoundError("Rate not found.")
        return view

    async def history_page(
        self,
        ctx: OrgContext,
        filters: ChangeFilters,
        *,
        date_from: date | None,
        date_to: date | None,
        params: PageParams,
    ) -> Page[ChangeView]:
        tz = ctx.organization.timezone
        filters = replace(
            filters,
            from_at=start_of_day_utc(date_from, tz) if date_from else None,
            to_at=end_of_day_utc(date_to, tz) if date_to else None,
        )
        return await self.history.list_page(ctx.organization_id, filters, params)

    # --- Writes (the V2 endpoints) ------------------------------------------------------------

    async def create(self, ctx: OrgContext, data: RateRuleCreate, rctx: RequestContext) -> RuleView:
        scope = Scope(data.client_id, data.service_id, data.user_id, data.grade_id)
        try:
            async with unit_of_work(self.session):
                rule = await self.write_rule(
                    ctx,
                    rctx,
                    kind=data.kind,
                    scope=scope,
                    rate=data.rate,
                    start=data.effective_from,
                    end=data.effective_to,
                    close_previous=data.close_previous,
                    audit_action="rate.created",
                )
                view = await self.rules.view(ctx.organization_id, rule.id)
        except IntegrityError as exc:
            if is_exclusion_violation(exc):
                raise overlap_error() from exc
            raise
        assert view is not None
        return view

    async def update(
        self, ctx: OrgContext, rule_id: uuid.UUID, data: RateRuleUpdate, rctx: RequestContext
    ) -> RuleView:
        try:
            async with unit_of_work(self.session):
                view, _ = await self.update_rule(
                    ctx, rctx, rule_id, data, audit_action="rate.updated"
                )
        except IntegrityError as exc:
            if is_exclusion_violation(exc):
                raise overlap_error() from exc
            raise
        return view

    async def delete(self, ctx: OrgContext, rule_id: uuid.UUID, rctx: RequestContext) -> None:
        async with unit_of_work(self.session):
            await self.delete_rule(ctx, rctx, rule_id, audit_action="rate.deleted")

    # --- Primitives (shared with the V1 endpoints) -------------------------------------------

    def level_of(self, kind: str, scope: Scope) -> str:
        level = level_for(scope)
        if level is None:
            raise ValidationError.for_field(
                "scope", f"A rate applies to {VALID_SCOPES}.", code="invalid_scope"
            )
        if kind == "cost" and level not in ("staff", "grade"):
            raise ValidationError.for_field(
                "kind", "Cost rates exist only for a staff member or a grade.", code="invalid_kind"
            )
        return level

    async def check_references(self, org_id: uuid.UUID, scope: Scope) -> None:
        """Every id of the scope must belong to this practice (422 on the field otherwise)."""
        if scope.client_id is not None and await self.clients.get(org_id, scope.client_id) is None:
            raise ValidationError.for_field("client_id", "This client does not exist.")
        if (
            scope.service_id is not None
            and await self.services.get(org_id, scope.service_id) is None
        ):
            raise ValidationError.for_field("service_id", "This service does not exist.")
        if scope.user_id is not None:
            member = await self.members.get(org_id, scope.user_id)
            if member is None:
                raise ValidationError.for_field(
                    "user_id", "Choose a staff member of this practice."
                )
            if member.status in GONE_STATUSES:
                raise ConflictError("This person has left the practice.", code="MEMBER_REMOVED")
        if scope.grade_id is not None and await self.grades.get(org_id, scope.grade_id) is None:
            raise ValidationError.for_field("grade_id", "This grade does not exist.")

    async def write_rule(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        *,
        kind: str,
        scope: Scope,
        rate: int,
        start: date,
        end: date | None,
        close_previous: bool = False,
        audit_action: str | None,
        source: str = "rates",
    ) -> RateRule:
        """Insert a rule inside the caller's unit of work.

        With ``close_previous`` an earlier rule of the same scope that runs into the new period
        is ended the day before it (only when that is a plain shortening: otherwise 409).
        """
        org_id = ctx.organization_id
        level = self.level_of(kind, scope)
        await self.check_references(org_id, scope)
        await self.rules.lock_scope(org_id, kind, scope)
        overlaps = await self.rules.overlapping(org_id, kind, scope, start, end)
        if overlaps:
            if not close_previous or not _closable(overlaps, start, end):
                raise overlap_error(overlaps)
            earlier = overlaps[0]
            old = snapshot(earlier)
            earlier.effective_to = start - timedelta(days=1)
            await self.rules.save(earlier)
            await self._log(ctx, "end_dated", earlier, old, snapshot(earlier), source)
            view = await self.rules.view(org_id, earlier.id)
            assert view is not None
            await self._audit(
                ctx,
                rctx,
                "rate.updated",
                earlier,
                {
                    **rule_meta(view),
                    "changes": {
                        "effective_to": [old["effective_to"], earlier.effective_to.isoformat()]
                    },
                },
            )
        rule = await self.rules.add(
            RateRule(
                organization_id=org_id,
                kind=kind,
                level=level,
                client_id=scope.client_id,
                service_id=scope.service_id,
                user_id=scope.user_id,
                grade_id=scope.grade_id,
                rate=rate,
                effective_from=start,
                effective_to=end,
                created_by=ctx.user.id,
            )
        )
        await self._log(ctx, "created", rule, None, snapshot(rule), source)
        if audit_action:
            view = await self.rules.view(org_id, rule.id)
            assert view is not None
            await self._audit(ctx, rctx, audit_action, rule, rule_meta(view))
        return rule

    async def delete_rule(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        rule_id: uuid.UUID,
        *,
        audit_action: str | None,
        source: str = "rates",
        absorb: bool = False,
        meta: dict[str, Any] | None = None,
    ) -> RuleView:
        """Delete an unused rule inside the caller's unit of work (409 ``RATE_IN_USE`` when used).

        With ``absorb`` the rule before it (if it ended the day before this one started) takes
        over the days the deleted rule covered, as a person's staff-rate timeline did in V1.
        """
        org_id = ctx.organization_id
        found = await self.rules.get(org_id, rule_id)
        if found is None:
            raise NotFoundError("Rate not found.")
        scope = scope_of(found)
        await self.rules.lock_scope(org_id, found.kind, scope)
        rule = await self.rules.get(org_id, rule_id, lock=True)
        if rule is None:
            raise NotFoundError("Rate not found.")
        view = await self.rules.view(org_id, rule_id)
        assert view is not None
        if view.is_used:
            raise in_use_error(
                "This rate has been used on time entries and cannot be deleted. "
                "End-date it instead."
            )
        old = snapshot(rule)
        await self.rules.delete(org_id, rule_id)
        await self._log(ctx, "deleted", rule, old, None, source)
        if absorb:
            earlier = await self.rules.previous(org_id, rule.kind, scope, rule.effective_from)
            if (
                earlier is not None
                and earlier.effective_to is not None
                and earlier.effective_to == rule.effective_from - timedelta(days=1)
            ):
                before = snapshot(earlier)
                earlier.effective_to = rule.effective_to
                await self.rules.save(earlier)
                await self._log(ctx, "end_dated", earlier, before, snapshot(earlier), source)
        if audit_action:
            await self._audit(ctx, rctx, audit_action, rule, {**rule_meta(view), **(meta or {})})
        return view

    async def update_rule(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        rule_id: uuid.UUID,
        data: RateRuleUpdate,
        *,
        audit_action: str | None,
        source: str = "rates",
    ) -> tuple[RuleView, dict[str, list[Any]]]:
        """Change a rule's amount and period inside the caller's unit of work.

        Returns the rule and ``{field: [old, new]}`` for what changed (empty = nothing).
        """
        org_id = ctx.organization_id
        found = await self.rules.get(org_id, rule_id)
        if found is None:
            raise NotFoundError("Rate not found.")
        scope = scope_of(found)
        await self.rules.lock_scope(org_id, found.kind, scope)
        rule = await self.rules.get(org_id, rule_id, lock=True)
        if rule is None:
            raise NotFoundError("Rate not found.")
        fields = data.model_fields_set
        start = data.effective_from if data.effective_from is not None else rule.effective_from
        end = data.effective_to if "effective_to" in fields else rule.effective_to
        rate = data.rate if data.rate is not None else rule.rate
        if end is not None and end < start:
            raise ValidationError.for_field("effective_to", "Must not be before effective_from.")
        overlaps = await self.rules.overlapping(
            org_id, rule.kind, scope, start, end, exclude_id=rule.id
        )
        if overlaps:
            raise overlap_error(overlaps)
        before = snapshot(rule)
        rule.rate, rule.effective_from, rule.effective_to = rate, start, end
        after = snapshot(rule)
        changes = {k: [before[k], after[k]] for k in after if before[k] != after[k]}
        if changes:
            await self.rules.save(rule)
            action = "end_dated" if set(changes) == {"effective_to"} else "updated"
            await self._log(ctx, action, rule, before, after, source)
        view = await self.rules.view(org_id, rule.id)
        assert view is not None
        if changes and audit_action:
            await self._audit(
                ctx, rctx, audit_action, rule, {**rule_meta(view), "changes": changes}
            )
        return view, changes

    # --- Internals ------------------------------------------------------------------------
    async def _log(
        self,
        ctx: OrgContext,
        action: str,
        rule: RateRule,
        old: dict[str, Any] | None,
        new: dict[str, Any] | None,
        source: str,
    ) -> None:
        await self.history.add(
            RateChange(
                organization_id=ctx.organization_id,
                rule_id=rule.id,
                action=action,
                kind=rule.kind,
                level=rule.level,
                client_id=rule.client_id,
                service_id=rule.service_id,
                user_id=rule.user_id,
                grade_id=rule.grade_id,
                old=old,
                new=new,
                actor_user_id=ctx.user.id,
                source=source,
            )
        )

    async def _audit(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        action: str,
        rule: RateRule,
        meta: dict[str, Any],
    ) -> None:
        await audit.record(
            self.session,
            action,
            ctx=rctx,
            actor_user_id=ctx.user.id,
            organization_id=ctx.organization_id,
            target_type="rate_rule",
            target_id=rule.id,
            meta=meta,
        )
