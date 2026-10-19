"""Expense receipt queries. Every method is scoped to one organization."""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Expense, ExpenseClaim, ExpenseReceipt, User
from app.repositories.expense_paging import AnySelect, SortExpr, SortKind, keyset_page
from app.utils.pagination import PageParams


@dataclass(frozen=True, slots=True)
class ReceiptFilters:
    # Whose receipts the caller may see: None = everyone's.
    scope_user_ids: Sequence[uuid.UUID] | None = None
    claim_statuses: Sequence[str] = ()
    claim_id: uuid.UUID | None = None
    expense_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    client_id: uuid.UUID | None = None
    date_from: date | None = None
    date_to: date | None = None
    q: str | None = None


class ExpenseReceiptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, receipt: ExpenseReceipt) -> ExpenseReceipt:
        self.session.add(receipt)
        await self.session.flush()
        await self.session.refresh(receipt)
        return receipt

    async def delete(self, receipt: ExpenseReceipt) -> None:
        await self.session.delete(receipt)
        await self.session.flush()

    async def get(
        self,
        organization_id: uuid.UUID,
        expense_id: uuid.UUID,
        receipt_id: uuid.UUID,
        *,
        lock: bool = False,
    ) -> ExpenseReceipt | None:
        stmt = select(ExpenseReceipt).where(
            ExpenseReceipt.organization_id == organization_id,
            ExpenseReceipt.expense_id == expense_id,
            ExpenseReceipt.id == receipt_id,
        )
        if lock:
            stmt = stmt.with_for_update()
        return (
            await self.session.execute(stmt.execution_options(populate_existing=True))
        ).scalar_one_or_none()

    async def of_expenses(
        self, organization_id: uuid.UUID, expense_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, list[ExpenseReceipt]]:
        """The receipts of each line, oldest first."""
        if not expense_ids:
            return {}
        stmt = (
            select(ExpenseReceipt)
            .where(
                ExpenseReceipt.organization_id == organization_id,
                ExpenseReceipt.expense_id.in_(expense_ids),
            )
            .order_by(ExpenseReceipt.created_at, ExpenseReceipt.id)
        )
        found: dict[uuid.UUID, list[ExpenseReceipt]] = {}
        for receipt in (await self.session.execute(stmt)).scalars():
            found.setdefault(receipt.expense_id, []).append(receipt)
        return found

    async def count_of(self, organization_id: uuid.UUID, expense_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(ExpenseReceipt)
            .where(
                ExpenseReceipt.organization_id == organization_id,
                ExpenseReceipt.expense_id == expense_id,
            )
        )
        return int(await self.session.scalar(stmt) or 0)

    async def keys_of_expense(self, organization_id: uuid.UUID, expense_id: uuid.UUID) -> list[str]:
        stmt = select(ExpenseReceipt.storage_key).where(
            ExpenseReceipt.organization_id == organization_id,
            ExpenseReceipt.expense_id == expense_id,
        )
        return list((await self.session.execute(stmt)).scalars())

    async def keys_of_claim(self, organization_id: uuid.UUID, claim_id: uuid.UUID) -> list[str]:
        stmt = (
            select(ExpenseReceipt.storage_key)
            .join(
                Expense,
                (Expense.organization_id == ExpenseReceipt.organization_id)
                & (Expense.id == ExpenseReceipt.expense_id),
            )
            .where(Expense.organization_id == organization_id, Expense.claim_id == claim_id)
        )
        return list((await self.session.execute(stmt)).scalars())

    # --- The Receipts page ------------------------------------------------------------

    @staticmethod
    def _filtered(
        stmt: AnySelect, organization_id: uuid.UUID, filters: ReceiptFilters
    ) -> AnySelect:
        conditions: list[ColumnElement[bool]] = [ExpenseReceipt.organization_id == organization_id]
        if filters.scope_user_ids is not None:
            conditions.append(ExpenseClaim.user_id.in_(filters.scope_user_ids))
        if filters.claim_statuses:
            conditions.append(ExpenseClaim.status.in_(filters.claim_statuses))
        if filters.claim_id is not None:
            conditions.append(Expense.claim_id == filters.claim_id)
        if filters.expense_id is not None:
            conditions.append(ExpenseReceipt.expense_id == filters.expense_id)
        if filters.user_id is not None:
            conditions.append(ExpenseClaim.user_id == filters.user_id)
        if filters.client_id is not None:
            conditions.append(Expense.client_id == filters.client_id)
        if filters.date_from is not None:
            conditions.append(Expense.incurred_on >= filters.date_from)
        if filters.date_to is not None:
            conditions.append(Expense.incurred_on <= filters.date_to)
        if filters.q:
            conditions.append(
                or_(
                    ExpenseReceipt.file_name.icontains(filters.q, autoescape=True),
                    Expense.description.icontains(filters.q, autoescape=True),
                    Expense.supplier.icontains(filters.q, autoescape=True),
                    ExpenseClaim.title.icontains(filters.q, autoescape=True),
                    User.full_name.icontains(filters.q, autoescape=True),
                )
            )
        return stmt.where(*conditions)

    async def list_page(
        self,
        organization_id: uuid.UUID,
        filters: ReceiptFilters,
        *,
        sort: str,
        descending: bool,
        params: PageParams,
    ) -> tuple[list[tuple[ExpenseReceipt, Expense, ExpenseClaim]], str | None]:
        key: SortExpr
        kind: SortKind
        match sort:
            case "incurred_on":
                key, kind = Expense.incurred_on, "date"
            case "gross":
                key, kind = Expense.gross, "int"
            case _:
                key, kind = ExpenseReceipt.created_at, "dt"
        stmt = (
            select(ExpenseReceipt, Expense, ExpenseClaim)
            .join(
                Expense,
                (Expense.organization_id == ExpenseReceipt.organization_id)
                & (Expense.id == ExpenseReceipt.expense_id),
            )
            .join(
                ExpenseClaim,
                (ExpenseClaim.organization_id == Expense.organization_id)
                & (ExpenseClaim.id == Expense.claim_id),
            )
            .join(User, User.id == ExpenseClaim.user_id)
        )
        stmt = self._filtered(stmt, organization_id, filters)
        rows, next_cursor = await keyset_page(
            self.session,
            stmt.execution_options(populate_existing=True),
            key=key,
            id_column=ExpenseReceipt.id,
            sort=sort,
            kind=kind,
            descending=descending,
            params=params,
        )
        return [(row[0], row[1], row[2]) for row in rows], next_cursor
