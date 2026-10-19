"""Staff repository: the rows of the staff list and the uniqueness checks of staff records.

Staff are ``organization_members`` joined to their user, role, grade and approver. Every method
is scoped to one organization.
"""

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import ColumnElement, and_, case, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models import Grade, OrganizationMember, Role, User
from app.repositories.time_paging import keyset_finish, keyset_query, parse_date
from app.utils.pagination import Page, PageParams

# What the staff API calls each state, and the stored statuses it covers: the V1 members API
# wrote ``suspended`` and ``removed``, the staff API writes ``inactive`` and ``left``.
STATUS_ALIASES: dict[str, tuple[str, ...]] = {
    "active": ("active",),
    "inactive": ("inactive", "suspended"),
    "left": ("left", "removed"),
}
_NO_GRADE = 2_147_483_647
_EARLIEST = date(1900, 1, 1)
_LATEST = date(9999, 12, 31)


def normalised_status(stored: str) -> str:
    """``active``, ``inactive`` or ``left`` for any stored status."""
    for name, stored_values in STATUS_ALIASES.items():
        if stored in stored_values:
            return name
    return stored


def _status_rank(stored: str) -> int:
    return {"active": 0, "inactive": 1}.get(normalised_status(stored), 2)


@dataclass(frozen=True, slots=True)
class StaffRow:
    member: OrganizationMember
    user: User
    role: Role
    grade: Grade | None
    approver_name: str | None


@dataclass(frozen=True, slots=True)
class StaffFilters:
    q: str | None = None
    # Stored statuses to keep (see ``STATUS_ALIASES``); empty = every status.
    statuses: Sequence[str] = ()
    grade_id: uuid.UUID | None = None
    approver_id: uuid.UUID | None = None
    role_id: uuid.UUID | None = None
    has_login: bool | None = None
    # Active and not past their leaving date (the people time can be recorded for).
    selectable: bool | None = None
    today: date | None = None


# sort key -> (sort expression, cursor value parser, key of a loaded row)
_SORTS: dict[str, tuple[Any, Callable[[Any], Any], Callable[[StaffRow], Any]]] = {
    "name": (func.lower(User.full_name), str, lambda r: r.user.full_name.lower()),
    "code": (
        func.upper(OrganizationMember.staff_code),
        str,
        lambda r: r.member.staff_code.upper(),
    ),
    "email": (
        func.lower(func.coalesce(OrganizationMember.work_email, User.email)),
        str,
        lambda r: (r.member.work_email or r.user.email).lower(),
    ),
    "grade": (
        func.coalesce(Grade.sort_order, _NO_GRADE),
        int,
        lambda r: r.grade.sort_order if r.grade else _NO_GRADE,
    ),
    "status": (
        case(
            (OrganizationMember.status == "active", 0),
            (OrganizationMember.status.in_(STATUS_ALIASES["inactive"]), 1),
            else_=2,
        ),
        int,
        lambda r: _status_rank(r.member.status),
    ),
    "start_date": (
        func.coalesce(OrganizationMember.start_date, _EARLIEST),
        parse_date,
        lambda r: r.member.start_date or _EARLIEST,
    ),
    "leaving_date": (
        func.coalesce(OrganizationMember.leaving_date, _LATEST),
        parse_date,
        lambda r: r.member.leaving_date or _LATEST,
    ),
}
STAFF_SORTS = tuple(_SORTS)


def _base_select() -> Any:
    approver = aliased(User)
    return (
        select(OrganizationMember, User, Role, Grade, approver.full_name)
        .join(User, User.id == OrganizationMember.user_id)
        .join(Role, Role.id == OrganizationMember.role_id)
        .outerjoin(
            Grade,
            and_(
                Grade.organization_id == OrganizationMember.organization_id,
                Grade.id == OrganizationMember.grade_id,
            ),
        )
        .outerjoin(approver, approver.id == OrganizationMember.manager_user_id)
        .execution_options(populate_existing=True)
    )


def _conditions(organization_id: uuid.UUID, filters: StaffFilters) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = [OrganizationMember.organization_id == organization_id]
    if filters.statuses:
        conditions.append(OrganizationMember.status.in_(list(filters.statuses)))
    if filters.grade_id is not None:
        conditions.append(OrganizationMember.grade_id == filters.grade_id)
    if filters.approver_id is not None:
        conditions.append(OrganizationMember.manager_user_id == filters.approver_id)
    if filters.role_id is not None:
        conditions.append(OrganizationMember.role_id == filters.role_id)
    if filters.has_login is not None:
        conditions.append(OrganizationMember.login_enabled.is_(filters.has_login))
    if filters.selectable is not None and filters.today is not None:
        selectable = and_(
            OrganizationMember.status == "active",
            or_(
                OrganizationMember.leaving_date.is_(None),
                OrganizationMember.leaving_date >= filters.today,
            ),
        )
        conditions.append(selectable if filters.selectable else not_(selectable))
    if filters.q:
        conditions.append(
            or_(
                User.full_name.icontains(filters.q, autoescape=True),
                OrganizationMember.staff_code.icontains(filters.q, autoescape=True),
                OrganizationMember.work_email.icontains(filters.q, autoescape=True),
                and_(
                    OrganizationMember.login_enabled.is_(True),
                    User.email.icontains(filters.q, autoescape=True),
                ),
            )
        )
    return conditions


class StaffRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_row(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> StaffRow | None:
        stmt = _base_select().where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id == user_id,
        )
        row = (await self.session.execute(stmt)).first()
        return StaffRow(*row) if row else None

    async def list_page(
        self,
        organization_id: uuid.UUID,
        filters: StaffFilters,
        *,
        sort: str,
        descending: bool,
        params: PageParams,
    ) -> Page[StaffRow]:
        sort_expr, parse, key = _SORTS.get(sort, _SORTS["name"])
        stmt = keyset_query(
            _base_select().where(*_conditions(organization_id, filters)),
            sort_expr=sort_expr,
            id_column=OrganizationMember.user_id,
            params=params,
            descending=descending,
            parse=parse,
        )
        rows = [StaffRow(*row) for row in (await self.session.execute(stmt)).all()]
        items, next_cursor = keyset_finish(rows, params, lambda r: (key(r), r.member.user_id))
        return Page(items=items, next_cursor=next_cursor)

    async def staff_code_taken(
        self, organization_id: uuid.UUID, code: str, *, exclude_user_id: uuid.UUID | None = None
    ) -> bool:
        stmt = select(OrganizationMember.user_id).where(
            OrganizationMember.organization_id == organization_id,
            func.upper(OrganizationMember.staff_code) == code.upper(),
        )
        if exclude_user_id is not None:
            stmt = stmt.where(OrganizationMember.user_id != exclude_user_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def email_taken(
        self, organization_id: uuid.UUID, email: str, *, exclude_user_id: uuid.UUID | None = None
    ) -> bool:
        """Whether another active staff member has this work email or signs in with it."""
        lowered = email.lower()
        stmt = (
            select(OrganizationMember.user_id)
            .join(User, User.id == OrganizationMember.user_id)
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.status == "active",
                or_(
                    func.lower(OrganizationMember.work_email) == lowered,
                    and_(
                        OrganizationMember.login_enabled.is_(True),
                        func.lower(User.email) == lowered,
                    ),
                ),
            )
        )
        if exclude_user_id is not None:
            stmt = stmt.where(OrganizationMember.user_id != exclude_user_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def login_member_by_email(
        self, organization_id: uuid.UUID, email: str
    ) -> OrganizationMember | None:
        """The staff member of the practice who signs in with this address."""
        stmt = (
            select(OrganizationMember)
            .join(User, User.id == OrganizationMember.user_id)
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.login_enabled.is_(True),
                OrganizationMember.status == "active",
                func.lower(User.email) == email.lower(),
                User.deleted_at.is_(None),
            )
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()
