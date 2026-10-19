"""Queries of the expenses and payments reports. Read-only; every query is filtered by
``organization_id`` and money sums come back as Python ``int`` (see ``repositories/reports.py``).
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, and_, func, not_, null, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import Null

from app.db.models import (
    Client,
    Expense,
    ExpenseClaim,
    ExpenseType,
    Job,
    OrganizationMember,
    Payment,
    User,
)
from app.repositories.reports import (
    AnySelect,
    DateRange,
    ItemFilter,
    client_conditions,
    first_of_month,
    live_payment,
    total,
    when,
)


@dataclass(frozen=True, slots=True)
class ExpenseFilter:
    """Which expense lines count: those of the claims of ``scope_user_ids`` (``None``: every
    claim of the practice) that match the rest. A claim's status is ``claim_statuses``."""

    scope_user_ids: Sequence[uuid.UUID] | None = None
    claim_statuses: Sequence[str] = ()
    user_id: uuid.UUID | None = None
    client_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    expense_type_id: uuid.UUID | None = None
    is_billable: bool | None = None
    billing_statuses: Sequence[str] = ()


def _present(columns: Sequence[Any]) -> list[Any]:
    """The columns that are real expressions (a ``null()`` placeholder is not grouped by)."""
    return [c for c in columns if not isinstance(c, Null)]


class ReportExtRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _rows(self, stmt: AnySelect) -> Sequence[RowMapping]:
        rows: Sequence[RowMapping] = (await self.session.execute(stmt)).mappings().all()
        return rows

    async def expense_type_exists(self, organization_id: uuid.UUID, type_id: uuid.UUID) -> bool:
        stmt = select(ExpenseType.id).where(
            ExpenseType.organization_id == organization_id, ExpenseType.id == type_id
        )
        return (await self.session.scalar(stmt)) is not None

    # --- Expenses ---------------------------------------------------------------------

    async def expenses(
        self, organization_id: uuid.UUID, period: DateRange, group_by: str, f: ExpenseFilter
    ) -> Sequence[RowMapping]:
        """Rows ``key, code, name, client_name, client_id`` and the expense measures per group."""
        key: Any
        code: Any = null()
        name: Any = null()
        client_name: Any = null()
        client_id: Any = null()
        match group_by:
            case "type":
                key, code, name = Expense.expense_type_id, ExpenseType.code, ExpenseType.name
            case "person":
                key, code, name = ExpenseClaim.user_id, OrganizationMember.initials, User.full_name
            case "client":
                key, code, name = Expense.client_id, Client.code, Client.name
            case "job":
                key, code, name = Expense.job_id, Job.number, Job.name
                client_name, client_id = Client.name, Client.id
            case "month":
                key = first_of_month(Expense.incurred_on)
            case "status":
                key = ExpenseClaim.status
            case _:  # pragma: no cover - validated by the schema
                raise ValueError(group_by)
        stmt = (
            select(
                key.label("key"),
                code.label("code"),
                name.label("name"),
                client_name.label("client_name"),
                client_id.label("client_id"),
                func.count(Expense.id).label("line_count"),
                total(Expense.net).label("net"),
                total(Expense.vat).label("vat"),
                total(Expense.gross).label("gross"),
                total(when(Expense.is_billable.is_(True), Expense.net)).label("billable_net"),
                total(Expense.recharge_amount).label("recharge_amount"),
                total(when(Expense.billing_status == "billed", Expense.recharge_amount)).label(
                    "recharged_amount"
                ),
                total(when(Expense.payer == "staff", Expense.gross)).label("reimbursable"),
            )
            .select_from(Expense)
            .join(
                ExpenseClaim,
                and_(
                    ExpenseClaim.organization_id == Expense.organization_id,
                    ExpenseClaim.id == Expense.claim_id,
                ),
            )
            .where(
                Expense.organization_id == organization_id,
                *period.contains(Expense.incurred_on),
                *self._expense_conditions(f),
            )
        )
        match group_by:
            case "type":
                stmt = stmt.join(
                    ExpenseType,
                    and_(
                        ExpenseType.organization_id == Expense.organization_id,
                        ExpenseType.id == Expense.expense_type_id,
                    ),
                )
            case "person":
                stmt = stmt.join(
                    OrganizationMember,
                    and_(
                        OrganizationMember.organization_id == ExpenseClaim.organization_id,
                        OrganizationMember.user_id == ExpenseClaim.user_id,
                    ),
                ).join(User, User.id == OrganizationMember.user_id)
            case "client" | "job":
                stmt = stmt.outerjoin(
                    Client,
                    and_(
                        Client.organization_id == Expense.organization_id,
                        Client.id == Expense.client_id,
                    ),
                )
                if group_by == "job":
                    stmt = stmt.outerjoin(
                        Job,
                        and_(
                            Job.organization_id == Expense.organization_id,
                            Job.id == Expense.job_id,
                        ),
                    )
        stmt = stmt.group_by(*_present([key, code, name, client_name, client_id]))
        if group_by in ("month", "status"):
            stmt = stmt.order_by(key)
        else:
            stmt = stmt.order_by(
                func.lower(name).nulls_last(), func.lower(client_name), code.nulls_last(), key
            )
        return await self._rows(stmt)

    @staticmethod
    def _expense_conditions(f: ExpenseFilter) -> list[ColumnElement[bool]]:
        conditions: list[ColumnElement[bool]] = []
        if f.scope_user_ids is not None:
            conditions.append(ExpenseClaim.user_id.in_(f.scope_user_ids))
        if f.claim_statuses:
            conditions.append(ExpenseClaim.status.in_(f.claim_statuses))
        if f.user_id is not None:
            conditions.append(ExpenseClaim.user_id == f.user_id)
        if f.client_id is not None:
            conditions.append(Expense.client_id == f.client_id)
        if f.job_id is not None:
            conditions.append(Expense.job_id == f.job_id)
        if f.expense_type_id is not None:
            conditions.append(Expense.expense_type_id == f.expense_type_id)
        if f.is_billable is not None:
            conditions.append(Expense.is_billable.is_(f.is_billable))
        if f.billing_statuses:
            conditions.append(Expense.billing_status.in_(f.billing_statuses))
        return conditions

    # --- Payments ---------------------------------------------------------------------

    async def payments(
        self,
        organization_id: uuid.UUID,
        period: DateRange,
        group_by: str,
        clients: ItemFilter,
        method: str | None,
    ) -> Sequence[RowMapping]:
        """Rows ``key, code, name`` and the payment measures per group: payments received in
        the period, counted as received or as void/reversed."""
        live = live_payment()
        key: Any
        code: Any = null()
        name: Any = null()
        match group_by:
            case "month":
                key = first_of_month(Payment.received_on)
            case "client":
                key, code, name = Payment.client_id, Client.code, Client.name
            case "method":
                key = Payment.method
            case _:  # pragma: no cover - validated by the schema
                raise ValueError(group_by)
        stmt = (
            select(
                key.label("key"),
                code.label("code"),
                name.label("name"),
                total(when(live, 1)).label("payment_count"),
                total(when(live, Payment.amount)).label("received"),
                total(when(live, Payment.allocated_total)).label("allocated"),
                total(when(not_(live), 1)).label("void_count"),
                total(when(not_(live), Payment.amount)).label("void_amount"),
            )
            .select_from(Payment)
            .join(
                Client,
                and_(
                    Client.organization_id == Payment.organization_id,
                    Client.id == Payment.client_id,
                ),
            )
            .where(
                Payment.organization_id == organization_id,
                *period.contains(Payment.received_on),
                *client_conditions(clients),
            )
        )
        if method is not None:
            stmt = stmt.where(Payment.method == method)
        stmt = stmt.group_by(*_present([key, code, name]))
        if group_by == "client":
            stmt = stmt.order_by(func.lower(name), code, key)
        else:
            stmt = stmt.order_by(key)
        return await self._rows(stmt)
