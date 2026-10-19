"""Expense claim queries: the claims with their totals, the approval queue, the summary and the
history of decisions. Every method is scoped to one organization.

A claim's totals are never stored: ``totals_for`` sums the lines of the claims on a page, one
query for the whole page. The list is one keyset-paginated query; what a page needs besides it (the
totals, the names of the people) is fetched in batches, never per row."""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import BigInteger, ColumnElement, case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Expense, ExpenseClaim, ExpenseClaimEvent, OrganizationMember, User
from app.repositories.expense_paging import AnySelect, SortExpr, SortKind, keyset_page
from app.utils.pagination import PageParams

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class ClaimFilters:
    statuses: Sequence[str] = ()
    user_id: uuid.UUID | None = None
    # Whose claims the caller may see: None = everyone's.
    scope_user_ids: Sequence[uuid.UUID] | None = None
    exclude_user_id: uuid.UUID | None = None
    # These five match a line of the claim (the same line for all of them).
    date_from: date | None = None
    date_to: date | None = None
    client_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    is_billable: bool | None = None
    q: str | None = None


@dataclass(frozen=True, slots=True)
class ClaimTotalsRow:
    line_count: int = 0
    net: int = 0
    vat: int = 0
    gross: int = 0
    recharge: int = 0
    reimbursable: int = 0
    first_date: date | None = None
    last_date: date | None = None


@dataclass(frozen=True, slots=True)
class StatusCount:
    count: int = 0
    gross: int = 0


def _line_matches(filters: ClaimFilters) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    if filters.date_from is not None:
        conditions.append(Expense.incurred_on >= filters.date_from)
    if filters.date_to is not None:
        conditions.append(Expense.incurred_on <= filters.date_to)
    if filters.client_id is not None:
        conditions.append(Expense.client_id == filters.client_id)
    if filters.job_id is not None:
        conditions.append(Expense.job_id == filters.job_id)
    if filters.is_billable is not None:
        conditions.append(Expense.is_billable.is_(filters.is_billable))
    return conditions


def claim_gross() -> ColumnElement[int]:
    """The gross total of the claim (a correlated subquery), the key of the ``total`` sort."""
    return (
        select(func.coalesce(func.sum(Expense.gross), 0).cast(BigInteger))
        .where(
            Expense.organization_id == ExpenseClaim.organization_id,
            Expense.claim_id == ExpenseClaim.id,
        )
        .correlate(ExpenseClaim)
        .scalar_subquery()
    )


class ExpenseClaimRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, claim: ExpenseClaim) -> ExpenseClaim:
        self.session.add(claim)
        await self.session.flush()
        await self.session.refresh(claim)
        return claim

    async def save(self, claim: ExpenseClaim) -> ExpenseClaim:
        await self.session.flush()
        await self.session.refresh(claim)
        return claim

    async def delete(self, claim: ExpenseClaim) -> None:
        await self.session.delete(claim)
        await self.session.flush()

    async def get(
        self, organization_id: uuid.UUID, claim_id: uuid.UUID, *, lock: bool = False
    ) -> ExpenseClaim | None:
        stmt = select(ExpenseClaim).where(
            ExpenseClaim.organization_id == organization_id, ExpenseClaim.id == claim_id
        )
        if lock:
            stmt = stmt.with_for_update()
        return (
            await self.session.execute(stmt.execution_options(populate_existing=True))
        ).scalar_one_or_none()

    async def lock_many(
        self, organization_id: uuid.UUID, claim_ids: Collection[uuid.UUID]
    ) -> list[ExpenseClaim]:
        """The practice's claims among ``claim_ids``, locked in id order (no deadlocks)."""
        if not claim_ids:
            return []
        stmt = (
            select(ExpenseClaim)
            .where(ExpenseClaim.organization_id == organization_id, ExpenseClaim.id.in_(claim_ids))
            .order_by(ExpenseClaim.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars())

    # --- The list ---------------------------------------------------------------------

    @staticmethod
    def _filtered(stmt: AnySelect, organization_id: uuid.UUID, filters: ClaimFilters) -> AnySelect:
        stmt = stmt.where(ExpenseClaim.organization_id == organization_id)
        if filters.statuses:
            stmt = stmt.where(ExpenseClaim.status.in_(filters.statuses))
        if filters.user_id is not None:
            stmt = stmt.where(ExpenseClaim.user_id == filters.user_id)
        if filters.scope_user_ids is not None:
            stmt = stmt.where(ExpenseClaim.user_id.in_(filters.scope_user_ids))
        if filters.exclude_user_id is not None:
            stmt = stmt.where(ExpenseClaim.user_id != filters.exclude_user_id)
        matches = _line_matches(filters)
        if matches:
            stmt = stmt.where(
                exists().where(
                    Expense.organization_id == ExpenseClaim.organization_id,
                    Expense.claim_id == ExpenseClaim.id,
                    *matches,
                )
            )
        if filters.q:
            stmt = stmt.where(
                or_(
                    ExpenseClaim.title.icontains(filters.q, autoescape=True),
                    User.full_name.icontains(filters.q, autoescape=True),
                    exists().where(
                        Expense.organization_id == ExpenseClaim.organization_id,
                        Expense.claim_id == ExpenseClaim.id,
                        or_(
                            Expense.description.icontains(filters.q, autoescape=True),
                            Expense.supplier.icontains(filters.q, autoescape=True),
                        ),
                    ),
                )
            )
        return stmt

    async def list_page(
        self,
        organization_id: uuid.UUID,
        filters: ClaimFilters,
        *,
        sort: str,
        descending: bool,
        params: PageParams,
    ) -> tuple[list[ExpenseClaim], str | None]:
        """One page of claims ordered by ``(sort key, id)``; the cursor carries the sort."""
        key: SortExpr
        kind: SortKind
        match sort:
            case "submitted_at":
                key, kind = func.coalesce(ExpenseClaim.submitted_at, _EPOCH), "dt"
            case "total":
                key, kind = claim_gross(), "int"
            case "user":
                key, kind = func.lower(User.full_name), "str"
            case _:
                key, kind = ExpenseClaim.created_at, "dt"
        stmt = self._filtered(
            select(ExpenseClaim).join(User, User.id == ExpenseClaim.user_id),
            organization_id,
            filters,
        )
        rows, next_cursor = await keyset_page(
            self.session,
            stmt.execution_options(populate_existing=True),
            key=key,
            id_column=ExpenseClaim.id,
            sort=sort,
            kind=kind,
            descending=descending,
            params=params,
        )
        return [row[0] for row in rows], next_cursor

    async def totals_for(
        self, organization_id: uuid.UUID, claim_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, ClaimTotalsRow]:
        if not claim_ids:
            return {}
        stmt = (
            select(
                Expense.claim_id,
                func.count(),
                func.coalesce(func.sum(Expense.net), 0),
                func.coalesce(func.sum(Expense.vat), 0),
                func.coalesce(func.sum(Expense.gross), 0),
                func.coalesce(func.sum(Expense.recharge_amount), 0),
                func.coalesce(
                    func.sum(case((Expense.payer == "staff", Expense.gross), else_=0)), 0
                ),
                func.min(Expense.incurred_on),
                func.max(Expense.incurred_on),
            )
            .where(Expense.organization_id == organization_id, Expense.claim_id.in_(claim_ids))
            .group_by(Expense.claim_id)
        )
        return {
            row[0]: ClaimTotalsRow(
                line_count=int(row[1]),
                net=int(row[2]),
                vat=int(row[3]),
                gross=int(row[4]),
                recharge=int(row[5]),
                reimbursable=int(row[6]),
                first_date=row[7],
                last_date=row[8],
            )
            for row in (await self.session.execute(stmt)).all()
        }

    async def status_counts(
        self,
        organization_id: uuid.UUID,
        *,
        scope_user_ids: Sequence[uuid.UUID] | None,
        exclude_user_id: uuid.UUID | None = None,
        statuses: Sequence[str] = (),
    ) -> dict[str, StatusCount]:
        """Per status: the claims and the gross of their lines (``None`` scope = everyone's)."""
        stmt = (
            select(
                ExpenseClaim.status,
                func.count(func.distinct(ExpenseClaim.id)),
                func.coalesce(func.sum(Expense.gross), 0),
            )
            .select_from(ExpenseClaim)
            .outerjoin(
                Expense,
                (Expense.organization_id == ExpenseClaim.organization_id)
                & (Expense.claim_id == ExpenseClaim.id),
            )
            .where(ExpenseClaim.organization_id == organization_id)
            .group_by(ExpenseClaim.status)
        )
        if scope_user_ids is not None:
            stmt = stmt.where(ExpenseClaim.user_id.in_(scope_user_ids))
        if exclude_user_id is not None:
            stmt = stmt.where(ExpenseClaim.user_id != exclude_user_id)
        if statuses:
            stmt = stmt.where(ExpenseClaim.status.in_(statuses))
        return {
            row[0]: StatusCount(count=int(row[1]), gross=int(row[2]))
            for row in (await self.session.execute(stmt)).all()
        }

    async def names(
        self, organization_id: uuid.UUID, user_ids: Collection[uuid.UUID | None]
    ) -> dict[uuid.UUID, str]:
        """Full names of members (any status) of the practice."""
        wanted = {user_id for user_id in user_ids if user_id is not None}
        if not wanted:
            return {}
        stmt = (
            select(User.id, User.full_name)
            .join(OrganizationMember, OrganizationMember.user_id == User.id)
            .where(OrganizationMember.organization_id == organization_id, User.id.in_(list(wanted)))
        )
        return {row[0]: row[1] for row in (await self.session.execute(stmt)).all()}

    async def managers_of(
        self, organization_id: uuid.UUID, user_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, uuid.UUID | None]:
        """The manager (approver) of each of these members."""
        if not user_ids:
            return {}
        stmt = select(OrganizationMember.user_id, OrganizationMember.manager_user_id).where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id.in_(list(user_ids)),
        )
        return {row[0]: row[1] for row in (await self.session.execute(stmt)).all()}

    # --- History ----------------------------------------------------------------------

    async def add_event(self, event: ExpenseClaimEvent) -> ExpenseClaimEvent:
        self.session.add(event)
        await self.session.flush()
        await self.session.refresh(event)
        return event

    async def events_of(
        self, organization_id: uuid.UUID, claim_id: uuid.UUID
    ) -> list[ExpenseClaimEvent]:
        stmt = (
            select(ExpenseClaimEvent)
            .where(
                ExpenseClaimEvent.organization_id == organization_id,
                ExpenseClaimEvent.claim_id == claim_id,
            )
            .order_by(ExpenseClaimEvent.created_at, ExpenseClaimEvent.id)
        )
        return list((await self.session.execute(stmt)).scalars())
