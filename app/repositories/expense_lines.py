"""Expense line queries. Every method is scoped to one organization.

``wip_eligible_expenses`` is the one function work in progress calls (docs/modules/expenses.md,
"Work in progress"); ``wip_expense_conditions`` is the same rule as SQL conditions for the WIP
queries that aggregate."""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import ColumnElement, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models import Expense, ExpenseClaim, ExpenseReceipt, User
from app.repositories.expense_paging import AnySelect, SortExpr, SortKind, keyset_page
from app.utils.pagination import PageParams


@dataclass(frozen=True, slots=True)
class LineFilters:
    # Whose lines the caller may see: None = everyone's.
    scope_user_ids: Sequence[uuid.UUID] | None = None
    claim_statuses: Sequence[str] = ()
    claim_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    client_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    expense_type_id: uuid.UUID | None = None
    is_billable: bool | None = None
    billing_statuses: Sequence[str] = ()
    date_from: date | None = None
    date_to: date | None = None
    q: str | None = None
    # Pence: only lines above it that are not mileage and have no receipt (the receipt rule).
    missing_receipt_over: int | None = None


@dataclass(frozen=True, slots=True)
class WipExpense:
    """An expense line that is work in progress: what WIP and billing need of it."""

    id: uuid.UUID
    claim_id: uuid.UUID
    user_id: uuid.UUID
    client_id: uuid.UUID
    job_id: uuid.UUID | None
    incurred_on: date
    description: str
    supplier: str | None
    net: int
    vat: int
    recharge_amount: int
    vat_rate_id: uuid.UUID | None
    vat_rate_bp: int | None


def wip_expense_conditions(organization_id: uuid.UUID) -> list[ColumnElement[bool]]:
    """The expense WIP rule as conditions over ``Expense``: the claim is approved, the line is
    billable, has a client, is unbilled and has something to recharge.

    The claim is an alias, so the conditions work whether or not the caller's own query already
    has ``expense_claims`` in its FROM clause (it would otherwise be correlated away)."""
    claim = aliased(ExpenseClaim)
    approved = (
        select(claim.id)
        .where(
            claim.organization_id == Expense.organization_id,
            claim.id == Expense.claim_id,
            claim.status == "approved",
        )
        .exists()
    )
    return [
        Expense.organization_id == organization_id,
        Expense.is_billable.is_(True),
        Expense.client_id.is_not(None),
        Expense.billing_status == "unbilled",
        Expense.recharge_amount > 0,
        approved,
    ]


async def wip_eligible_expenses(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    client_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    ids: Collection[uuid.UUID] | None = None,
    lock: bool = False,
) -> list[WipExpense]:
    """The practice's expense lines that are work in progress, oldest first (by line date, then
    id). ``client_id``, ``job_id`` and the inclusive ``date_from``/``date_to`` narrow them; ``ids``
    keeps only those lines; ``lock`` takes the lines ``FOR UPDATE`` (billing, before allocating)."""
    stmt = (
        select(Expense, ExpenseClaim.user_id)
        .join(
            ExpenseClaim,
            (ExpenseClaim.organization_id == Expense.organization_id)
            & (ExpenseClaim.id == Expense.claim_id),
        )
        .where(*wip_expense_conditions(organization_id))
        .order_by(Expense.incurred_on, Expense.id)
    )
    if client_id is not None:
        stmt = stmt.where(Expense.client_id == client_id)
    if job_id is not None:
        stmt = stmt.where(Expense.job_id == job_id)
    if date_from is not None:
        stmt = stmt.where(Expense.incurred_on >= date_from)
    if date_to is not None:
        stmt = stmt.where(Expense.incurred_on <= date_to)
    if ids is not None:
        stmt = stmt.where(Expense.id.in_(list(ids)))
    if lock:
        stmt = stmt.with_for_update(of=Expense)
    found = (await session.execute(stmt.execution_options(populate_existing=True))).all()
    result: list[WipExpense] = []
    for line, claimant_id in found:
        assert line.client_id is not None
        result.append(
            WipExpense(
                id=line.id,
                claim_id=line.claim_id,
                user_id=claimant_id,
                client_id=line.client_id,
                job_id=line.job_id,
                incurred_on=line.incurred_on,
                description=line.description,
                supplier=line.supplier,
                net=line.net,
                vat=line.vat,
                recharge_amount=line.recharge_amount,
                vat_rate_id=line.vat_rate_id,
                vat_rate_bp=line.vat_rate_bp,
            )
        )
    return result


class ExpenseLineRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, line: Expense) -> Expense:
        self.session.add(line)
        await self.session.flush()
        await self.session.refresh(line)
        return line

    async def save(self, line: Expense) -> Expense:
        await self.session.flush()
        await self.session.refresh(line)
        return line

    async def delete(self, line: Expense) -> None:
        await self.session.delete(line)
        await self.session.flush()

    async def get(
        self, organization_id: uuid.UUID, line_id: uuid.UUID, *, lock: bool = False
    ) -> Expense | None:
        stmt = select(Expense).where(
            Expense.organization_id == organization_id, Expense.id == line_id
        )
        if lock:
            stmt = stmt.with_for_update()
        return (
            await self.session.execute(stmt.execution_options(populate_existing=True))
        ).scalar_one_or_none()

    async def lines_of(self, organization_id: uuid.UUID, claim_id: uuid.UUID) -> list[Expense]:
        stmt = (
            select(Expense)
            .where(Expense.organization_id == organization_id, Expense.claim_id == claim_id)
            .order_by(Expense.incurred_on, Expense.id)
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def lines_of_many(
        self, organization_id: uuid.UUID, claim_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, list[Expense]]:
        if not claim_ids:
            return {}
        stmt = (
            select(Expense)
            .where(Expense.organization_id == organization_id, Expense.claim_id.in_(claim_ids))
            .order_by(Expense.incurred_on, Expense.id)
            .execution_options(populate_existing=True)
        )
        found: dict[uuid.UUID, list[Expense]] = {}
        for line in (await self.session.execute(stmt)).scalars():
            found.setdefault(line.claim_id, []).append(line)
        return found

    async def lock_lines_of(self, organization_id: uuid.UUID, claim_id: uuid.UUID) -> list[Expense]:
        """The claim's lines, locked in id order and read fresh."""
        stmt = (
            select(Expense)
            .where(Expense.organization_id == organization_id, Expense.claim_id == claim_id)
            .order_by(Expense.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return sorted(
            (await self.session.execute(stmt)).scalars(), key=lambda x: (x.incurred_on, x.id)
        )

    async def count_in_claim(self, organization_id: uuid.UUID, claim_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(Expense)
            .where(Expense.organization_id == organization_id, Expense.claim_id == claim_id)
        )
        return int(await self.session.scalar(stmt) or 0)

    # --- The finance list --------------------------------------------------------------

    @staticmethod
    def _filtered(stmt: AnySelect, organization_id: uuid.UUID, filters: LineFilters) -> AnySelect:
        stmt = stmt.where(Expense.organization_id == organization_id)
        if filters.scope_user_ids is not None:
            stmt = stmt.where(ExpenseClaim.user_id.in_(filters.scope_user_ids))
        if filters.claim_statuses:
            stmt = stmt.where(ExpenseClaim.status.in_(filters.claim_statuses))
        if filters.claim_id is not None:
            stmt = stmt.where(Expense.claim_id == filters.claim_id)
        if filters.user_id is not None:
            stmt = stmt.where(ExpenseClaim.user_id == filters.user_id)
        if filters.client_id is not None:
            stmt = stmt.where(Expense.client_id == filters.client_id)
        if filters.job_id is not None:
            stmt = stmt.where(Expense.job_id == filters.job_id)
        if filters.expense_type_id is not None:
            stmt = stmt.where(Expense.expense_type_id == filters.expense_type_id)
        if filters.is_billable is not None:
            stmt = stmt.where(Expense.is_billable.is_(filters.is_billable))
        if filters.billing_statuses:
            stmt = stmt.where(Expense.billing_status.in_(filters.billing_statuses))
        if filters.date_from is not None:
            stmt = stmt.where(Expense.incurred_on >= filters.date_from)
        if filters.date_to is not None:
            stmt = stmt.where(Expense.incurred_on <= filters.date_to)
        if filters.missing_receipt_over is not None:
            stmt = stmt.where(
                Expense.gross > filters.missing_receipt_over,
                Expense.miles.is_(None),
                ~exists().where(
                    ExpenseReceipt.organization_id == Expense.organization_id,
                    ExpenseReceipt.expense_id == Expense.id,
                ),
            )
        if filters.q:
            stmt = stmt.where(
                or_(
                    Expense.description.icontains(filters.q, autoescape=True),
                    Expense.supplier.icontains(filters.q, autoescape=True),
                    ExpenseClaim.title.icontains(filters.q, autoescape=True),
                    User.full_name.icontains(filters.q, autoescape=True),
                )
            )
        return stmt

    @staticmethod
    def base_select() -> AnySelect:
        """Lines with their claim and the claimant, for the lists."""
        return (
            select(Expense, ExpenseClaim)
            .join(
                ExpenseClaim,
                (ExpenseClaim.organization_id == Expense.organization_id)
                & (ExpenseClaim.id == Expense.claim_id),
            )
            .join(User, User.id == ExpenseClaim.user_id)
        )

    async def list_page(
        self,
        organization_id: uuid.UUID,
        filters: LineFilters,
        *,
        sort: str,
        descending: bool,
        params: PageParams,
    ) -> tuple[list[tuple[Expense, ExpenseClaim]], str | None]:
        key: SortExpr
        kind: SortKind
        match sort:
            case "gross":
                key, kind = Expense.gross, "int"
            case "created_at":
                key, kind = Expense.created_at, "dt"
            case _:
                key, kind = Expense.incurred_on, "date"
        stmt = self._filtered(self.base_select(), organization_id, filters)
        rows, next_cursor = await keyset_page(
            self.session,
            stmt.execution_options(populate_existing=True),
            key=key,
            id_column=Expense.id,
            sort=sort,
            kind=kind,
            descending=descending,
            params=params,
        )
        return [(row[0], row[1]) for row in rows], next_cursor

    async def exists_in_claim(
        self, organization_id: uuid.UUID, claim_id: uuid.UUID, line_id: uuid.UUID
    ) -> bool:
        stmt = select(
            exists().where(
                Expense.organization_id == organization_id,
                Expense.claim_id == claim_id,
                Expense.id == line_id,
            )
        )
        return bool(await self.session.scalar(stmt))
