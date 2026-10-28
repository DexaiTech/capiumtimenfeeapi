"""Rate rule queries. Every method is scoped to one organization."""

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, delete, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models import (
    Client,
    Grade,
    Job,
    OrganizationMember,
    RateRule,
    Service,
    TimeEntry,
    User,
)
from app.repositories.time_paging import keyset_finish, keyset_query, parse_date
from app.utils.pagination import Page, PageParams


@dataclass(frozen=True, slots=True)
class Scope:
    """What a rate rule applies to; null means "any"."""

    client_id: uuid.UUID | None = None
    service_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    grade_id: uuid.UUID | None = None


_LEVELS: dict[tuple[bool, bool, bool, bool], str] = {
    (True, True, True, False): "client_service_staff",
    (True, True, False, False): "client_service",
    (True, False, True, False): "client_staff",
    (True, False, False, False): "client",
    (False, True, True, False): "service_staff",
    (False, True, False, True): "service_grade",
    (False, False, True, False): "staff",
    (False, False, False, True): "grade",
}


def level_for(scope: Scope) -> str | None:
    """The level of a scope, or None when the combination is not a rate level."""
    return _LEVELS.get(
        (
            scope.client_id is not None,
            scope.service_id is not None,
            scope.user_id is not None,
            scope.grade_id is not None,
        )
    )


def scope_of(rule: RateRule) -> Scope:
    return Scope(rule.client_id, rule.service_id, rule.user_id, rule.grade_id)


def _scope_conditions(scope: Scope) -> list[ColumnElement[bool]]:
    return [
        RateRule.client_id.is_(None)
        if scope.client_id is None
        else RateRule.client_id == scope.client_id,
        RateRule.service_id.is_(None)
        if scope.service_id is None
        else RateRule.service_id == scope.service_id,
        RateRule.user_id.is_(None) if scope.user_id is None else RateRule.user_id == scope.user_id,
        RateRule.grade_id.is_(None)
        if scope.grade_id is None
        else RateRule.grade_id == scope.grade_id,
    ]


def used_clause() -> ColumnElement[bool]:
    """Whether any time entry falls inside the rule's scope and period (correlated to
    ``rate_rules``). A rule that could have priced an entry counts as used: its null
    dimensions match anything."""
    job = aliased(Job)
    member = aliased(OrganizationMember)
    subquery = (
        select(literal(1))
        .select_from(TimeEntry)
        .outerjoin(
            job,
            and_(job.organization_id == TimeEntry.organization_id, job.id == TimeEntry.job_id),
        )
        .join(
            member,
            and_(
                member.organization_id == TimeEntry.organization_id,
                member.user_id == TimeEntry.user_id,
            ),
        )
        .where(
            TimeEntry.organization_id == RateRule.organization_id,
            TimeEntry.entry_date >= RateRule.effective_from,
            or_(RateRule.effective_to.is_(None), TimeEntry.entry_date <= RateRule.effective_to),
            or_(RateRule.user_id.is_(None), TimeEntry.user_id == RateRule.user_id),
            or_(RateRule.client_id.is_(None), TimeEntry.client_id == RateRule.client_id),
            or_(RateRule.service_id.is_(None), job.service_id == RateRule.service_id),
            or_(RateRule.grade_id.is_(None), member.grade_id == RateRule.grade_id),
        )
    )
    return subquery.exists()


@dataclass(frozen=True, slots=True)
class RuleView:
    """A rule with the names a list shows and whether it has been used."""

    rule: RateRule
    client_code: str | None
    client_name: str | None
    service_code: str | None
    service_name: str | None
    user_name: str | None
    staff_code: str | None
    grade_code: str | None
    grade_name: str | None
    is_used: bool


@dataclass(frozen=True, slots=True)
class RuleFilters:
    kind: str | None = None
    levels: Sequence[str] | None = None
    client_id: uuid.UUID | None = None
    service_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    grade_id: uuid.UUID | None = None
    # Only rules in force on this day.
    on_date: date | None = None
    # ``current``, ``future`` or ``ended`` relative to ``today``.
    status: str | None = None
    today: date | None = None
    q: str | None = None


SORTS = ("effective_from", "rate", "created_at")

# sort key -> (sort expression, cursor value parser, key of a loaded row)
_SORTS: dict[str, tuple[Any, Callable[[Any], Any], Callable[[RuleView], Any]]] = {
    "effective_from": (RateRule.effective_from, parse_date, lambda v: v.rule.effective_from),
    "rate": (RateRule.rate, int, lambda v: v.rule.rate),
    "created_at": (
        RateRule.created_at,
        lambda raw: datetime.fromisoformat(str(raw)),
        lambda v: v.rule.created_at,
    ),
}


def _view_select() -> Any:
    return (
        select(
            RateRule,
            Client.code,
            Client.name,
            Service.code,
            Service.name,
            User.full_name,
            OrganizationMember.staff_code,
            Grade.code,
            Grade.name,
            used_clause().label("is_used"),
        )
        .outerjoin(
            Client,
            and_(
                Client.organization_id == RateRule.organization_id, Client.id == RateRule.client_id
            ),
        )
        .outerjoin(
            Service,
            and_(
                Service.organization_id == RateRule.organization_id,
                Service.id == RateRule.service_id,
            ),
        )
        .outerjoin(User, User.id == RateRule.user_id)
        .outerjoin(
            OrganizationMember,
            and_(
                OrganizationMember.organization_id == RateRule.organization_id,
                OrganizationMember.user_id == RateRule.user_id,
            ),
        )
        .outerjoin(
            Grade,
            and_(Grade.organization_id == RateRule.organization_id, Grade.id == RateRule.grade_id),
        )
        .execution_options(populate_existing=True)
    )


def _to_view(row: Any) -> RuleView:
    return RuleView(*row)


class RateRuleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- Reads ----------------------------------------------------------------------------

    async def get(
        self, organization_id: uuid.UUID, rule_id: uuid.UUID, *, lock: bool = False
    ) -> RateRule | None:
        stmt = select(RateRule).where(
            RateRule.organization_id == organization_id, RateRule.id == rule_id
        )
        if lock:
            stmt = stmt.with_for_update()
        stmt = stmt.execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def view(self, organization_id: uuid.UUID, rule_id: uuid.UUID) -> RuleView | None:
        stmt = _view_select().where(
            RateRule.organization_id == organization_id, RateRule.id == rule_id
        )
        row = (await self.session.execute(stmt)).first()
        return _to_view(row) if row else None

    def _filtered(self, organization_id: uuid.UUID, f: RuleFilters) -> Any:
        stmt = _view_select().where(RateRule.organization_id == organization_id)
        if f.kind is not None:
            stmt = stmt.where(RateRule.kind == f.kind)
        if f.levels:
            stmt = stmt.where(RateRule.level.in_(list(f.levels)))
        if f.client_id is not None:
            stmt = stmt.where(RateRule.client_id == f.client_id)
        if f.service_id is not None:
            stmt = stmt.where(RateRule.service_id == f.service_id)
        if f.user_id is not None:
            stmt = stmt.where(RateRule.user_id == f.user_id)
        if f.grade_id is not None:
            stmt = stmt.where(RateRule.grade_id == f.grade_id)
        if f.on_date is not None:
            stmt = stmt.where(
                RateRule.effective_from <= f.on_date,
                or_(RateRule.effective_to.is_(None), RateRule.effective_to >= f.on_date),
            )
        if f.status is not None and f.today is not None:
            today = f.today
            if f.status == "current":
                stmt = stmt.where(
                    RateRule.effective_from <= today,
                    or_(RateRule.effective_to.is_(None), RateRule.effective_to >= today),
                )
            elif f.status == "future":
                stmt = stmt.where(RateRule.effective_from > today)
            elif f.status == "ended":
                stmt = stmt.where(RateRule.effective_to < today)
        if f.q:
            stmt = stmt.where(
                or_(
                    Client.name.icontains(f.q, autoescape=True),
                    Client.code.icontains(f.q, autoescape=True),
                    Service.name.icontains(f.q, autoescape=True),
                    Service.code.icontains(f.q, autoescape=True),
                    User.full_name.icontains(f.q, autoescape=True),
                    OrganizationMember.staff_code.icontains(f.q, autoescape=True),
                    Grade.name.icontains(f.q, autoescape=True),
                    Grade.code.icontains(f.q, autoescape=True),
                )
            )
        return stmt

    async def list_page(
        self,
        organization_id: uuid.UUID,
        filters: RuleFilters,
        *,
        sort: str,
        descending: bool,
        params: PageParams,
    ) -> Page[RuleView]:
        sort_expr, parse, key = _SORTS.get(sort, _SORTS["effective_from"])
        stmt = keyset_query(
            self._filtered(organization_id, filters),
            sort_expr=sort_expr,
            id_column=RateRule.id,
            params=params,
            descending=descending,
            parse=parse,
        )
        rows = [_to_view(row) for row in (await self.session.execute(stmt)).all()]
        items, next_cursor = keyset_finish(rows, params, lambda v: (key(v), v.rule.id))
        return Page(items=items, next_cursor=next_cursor)

    async def list_all(self, organization_id: uuid.UUID, filters: RuleFilters) -> list[RuleView]:
        """A bounded list (one person's rules, one client's): newest period first."""
        stmt = self._filtered(organization_id, filters).order_by(
            RateRule.effective_from.desc(), RateRule.id
        )
        return [_to_view(row) for row in (await self.session.execute(stmt)).all()]

    async def overlapping(
        self,
        organization_id: uuid.UUID,
        kind: str,
        scope: Scope,
        start: date,
        end: date | None,
        *,
        exclude_id: uuid.UUID | None = None,
    ) -> list[RateRule]:
        """Rules of the same kind and scope whose period shares a day with ``[start, end]``."""
        conditions = [
            RateRule.organization_id == organization_id,
            RateRule.kind == kind,
            *_scope_conditions(scope),
            or_(RateRule.effective_to.is_(None), RateRule.effective_to >= start),
        ]
        if end is not None:
            conditions.append(RateRule.effective_from <= end)
        if exclude_id is not None:
            conditions.append(RateRule.id != exclude_id)
        stmt = (
            select(RateRule)
            .where(*conditions)
            .order_by(RateRule.effective_from)
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def same_start(
        self, organization_id: uuid.UUID, kind: str, scope: Scope, start: date
    ) -> RateRule | None:
        stmt = select(RateRule).where(
            RateRule.organization_id == organization_id,
            RateRule.kind == kind,
            *_scope_conditions(scope),
            RateRule.effective_from == start,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def previous(
        self, organization_id: uuid.UUID, kind: str, scope: Scope, before: date
    ) -> RateRule | None:
        """The rule of the scope with the latest start before ``before``."""
        stmt = (
            select(RateRule)
            .where(
                RateRule.organization_id == organization_id,
                RateRule.kind == kind,
                *_scope_conditions(scope),
                RateRule.effective_from < before,
            )
            .order_by(RateRule.effective_from.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def following(
        self, organization_id: uuid.UUID, kind: str, scope: Scope, after: date
    ) -> RateRule | None:
        """The rule of the scope with the earliest start after ``after``."""
        stmt = (
            select(RateRule)
            .where(
                RateRule.organization_id == organization_id,
                RateRule.kind == kind,
                *_scope_conditions(scope),
                RateRule.effective_from > after,
            )
            .order_by(RateRule.effective_from)
            .limit(1)
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def in_force(
        self, organization_id: uuid.UUID, kind: str, scope: Scope, day: date
    ) -> RateRule | None:
        stmt = select(RateRule).where(
            RateRule.organization_id == organization_id,
            RateRule.kind == kind,
            *_scope_conditions(scope),
            RateRule.effective_from <= day,
            or_(RateRule.effective_to.is_(None), RateRule.effective_to >= day),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def staff_rules_in_force(
        self, organization_id: uuid.UUID, day: date
    ) -> dict[tuple[uuid.UUID, str], RateRule]:
        """``(user_id, kind) -> rule`` for every person's own (``staff`` level) rule on ``day``."""
        stmt = select(RateRule).where(
            RateRule.organization_id == organization_id,
            RateRule.level == "staff",
            RateRule.effective_from <= day,
            or_(RateRule.effective_to.is_(None), RateRule.effective_to >= day),
        )
        found: dict[tuple[uuid.UUID, str], RateRule] = {}
        for rule in (await self.session.execute(stmt)).scalars():
            assert rule.user_id is not None
            found[(rule.user_id, rule.kind)] = rule
        return found

    async def is_used(self, organization_id: uuid.UUID, rule_id: uuid.UUID) -> bool:
        stmt = select(RateRule.id).where(
            RateRule.organization_id == organization_id, RateRule.id == rule_id, used_clause()
        )
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def used_ids(
        self, organization_id: uuid.UUID, rule_ids: Sequence[uuid.UUID]
    ) -> set[uuid.UUID]:
        if not rule_ids:
            return set()
        stmt = select(RateRule.id).where(
            RateRule.organization_id == organization_id,
            RateRule.id.in_(list(rule_ids)),
            used_clause(),
        )
        return set((await self.session.execute(stmt)).scalars())

    async def count_for_grade(self, organization_id: uuid.UUID, grade_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(RateRule)
            .where(RateRule.organization_id == organization_id, RateRule.grade_id == grade_id)
        )
        return int((await self.session.execute(stmt)).scalar_one())

    # --- Writes ---------------------------------------------------------------------------

    async def lock_scope(self, organization_id: uuid.UUID, kind: str, scope: Scope) -> None:
        """Serialise changes to one scope (held until the transaction ends)."""
        key = (
            f"rate:{organization_id}:{kind}:{scope.client_id}:{scope.service_id}:"
            f"{scope.user_id}:{scope.grade_id}"
        )
        await self.session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0)))
        )

    async def add(self, rule: RateRule) -> RateRule:
        self.session.add(rule)
        await self.session.flush()
        await self.session.refresh(rule)
        return rule

    async def save(self, rule: RateRule) -> RateRule:
        await self.session.flush()
        await self.session.refresh(rule)
        return rule

    async def delete(self, organization_id: uuid.UUID, rule_id: uuid.UUID) -> None:
        await self.session.execute(
            delete(RateRule).where(
                RateRule.organization_id == organization_id, RateRule.id == rule_id
            )
        )
