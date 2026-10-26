"""Report queries. Read-only aggregates; every query is filtered by ``organization_id``.

Money sums are cast back to ``BIGINT`` (PostgreSQL widens ``sum(bigint)`` to numeric), so
every figure arrives as a Python ``int``.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import (
    BigInteger,
    ColumnElement,
    Integer,
    Select,
    Subquery,
    and_,
    case,
    cast,
    extract,
    false,
    func,
    literal,
    literal_column,
    null,
    or_,
    select,
    union_all,
)
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import Null

from app.db.models import (
    ActivityCode,
    Client,
    CreditNote,
    CreditNoteLine,
    Disbursement,
    Invoice,
    InvoiceLine,
    Job,
    OrganizationMember,
    Payment,
    PaymentAllocation,
    Service,
    TimeEntry,
    Timesheet,
    User,
    VatRate,
    WipAllocation,
    WipWriteOff,
)
from app.db.models.organization import GONE_STATUSES

type AnySelect = Select[*tuple[Any, ...]]


@dataclass(frozen=True, slots=True)
class ItemFilter:
    """Filters on WIP items (time entries and disbursements) and what they belong to.

    ``partner_user_id``/``manager_user_id`` match the responsible person: the job's, falling
    back to the client's. ``user_id`` and ``activity_code_id`` only match time, so they leave
    disbursements out.
    """

    client_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    partner_user_id: uuid.UUID | None = None
    manager_user_id: uuid.UUID | None = None
    service_id: uuid.UUID | None = None
    activity_code_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class DateRange:
    """Inclusive bounds; ``None`` is unbounded."""

    start: date | None
    end: date | None

    def contains(self, column: Any) -> list[ColumnElement[bool]]:
        conditions: list[ColumnElement[bool]] = []
        if self.start is not None:
            conditions.append(column >= self.start)
        if self.end is not None:
            conditions.append(column <= self.end)
        return conditions


def _int(value: int) -> ColumnElement[int]:
    """An integer inlined into the SQL text, so an expression grouped by renders identically
    in SELECT and GROUP BY (bound parameters would be numbered apart)."""
    return literal_column(str(int(value)), Integer)


def first_of_month(day: Any) -> ColumnElement[date]:
    result: ColumnElement[date] = day - (cast(extract("day", day), Integer) - _int(1))
    return result


def total(expr: Any) -> ColumnElement[int]:
    """``sum`` as a BIGINT, 0 over no rows."""
    return cast(func.coalesce(func.sum(expr), 0), BigInteger)


def when(condition: Any, value: Any) -> ColumnElement[Any]:
    return case((condition, value), else_=0)


def live_payment() -> ColumnElement[bool]:
    """A payment that counts as received: not voided and, where payments can be reversed, not
    reversed (``reversed_at``)."""
    reversed_at = getattr(Payment, "reversed_at", None)
    if reversed_at is None:
        return Payment.voided_at.is_(None)
    return and_(Payment.voided_at.is_(None), reversed_at.is_(None))


def _responsible(job_column: Any, client_column: Any) -> ColumnElement[Any]:
    return func.coalesce(job_column, client_column)


def item_conditions(
    f: ItemFilter,
    *,
    client_col: Any,
    job_col: Any,
    user_col: Any,
    activity_col: Any,
) -> list[ColumnElement[bool]]:
    """Filter conditions for a query that has ``Job`` and ``Client`` joined (Job outer)."""
    conditions: list[ColumnElement[bool]] = []
    if f.client_id is not None:
        conditions.append(client_col == f.client_id)
    if f.job_id is not None:
        conditions.append(job_col == f.job_id)
    if f.user_id is not None:
        conditions.append(user_col == f.user_id if user_col is not None else false())
    if f.activity_code_id is not None:
        conditions.append(
            activity_col == f.activity_code_id if activity_col is not None else false()
        )
    if f.service_id is not None:
        conditions.append(Job.service_id == f.service_id)
    if f.partner_user_id is not None:
        conditions.append(
            _responsible(Job.partner_user_id, Client.partner_user_id) == f.partner_user_id
        )
    if f.manager_user_id is not None:
        conditions.append(
            _responsible(Job.manager_user_id, Client.manager_user_id) == f.manager_user_id
        )
    return conditions


def client_conditions(
    f: ItemFilter,
) -> list[ColumnElement[bool]]:
    """Client-level filters (documents that have a client but no job)."""
    conditions: list[ColumnElement[bool]] = []
    if f.client_id is not None:
        conditions.append(Client.id == f.client_id)
    if f.partner_user_id is not None:
        conditions.append(Client.partner_user_id == f.partner_user_id)
    if f.manager_user_id is not None:
        conditions.append(Client.manager_user_id == f.manager_user_id)
    return conditions


def _time_select(organization_id: uuid.UUID, *columns: Any) -> AnySelect:
    """Time entries with their job (outer) and client (outer) joined."""
    return (
        select(*columns)
        .select_from(TimeEntry)
        .outerjoin(
            Job,
            and_(Job.organization_id == TimeEntry.organization_id, Job.id == TimeEntry.job_id),
        )
        .outerjoin(
            Client,
            and_(
                Client.organization_id == TimeEntry.organization_id,
                Client.id == TimeEntry.client_id,
            ),
        )
        .where(TimeEntry.organization_id == organization_id)
    )


def _cleared_after(
    organization_id: uuid.UUID, as_at: date, item_fk: str, item_id: Any
) -> ColumnElement[bool]:
    """The item has a live allocation that cleared it *after* ``as_at``: an issued invoice
    dated later, or a write-off dated later. Such an item was still WIP at the end of
    ``as_at`` although it is ``billed``/``written_off`` now."""
    alloc = aliased(WipAllocation)
    inv = aliased(Invoice)
    wo = aliased(WipWriteOff)
    return (
        select(alloc.id)
        .outerjoin(
            inv, and_(inv.organization_id == alloc.organization_id, inv.id == alloc.invoice_id)
        )
        .outerjoin(
            wo, and_(wo.organization_id == alloc.organization_id, wo.id == alloc.write_off_id)
        )
        .where(
            alloc.organization_id == organization_id,
            getattr(alloc, item_fk) == item_id,
            alloc.reversed_at.is_(None),
            or_(
                and_(inv.status == "issued", inv.issue_date > as_at),
                wo.written_off_on > as_at,
            ),
        )
        .exists()
    )


def _wip_status(
    organization_id: uuid.UUID, as_at: date, status_col: Any, item_fk: str, item_id: Any
) -> ColumnElement[bool]:
    """WIP as at the end of ``as_at``: unbilled now, or billed/written off since then."""
    return or_(
        status_col == "unbilled",
        and_(
            status_col.in_(("billed", "written_off")),
            _cleared_after(organization_id, as_at, item_fk, item_id),
        ),
    )


def wip_items(
    organization_id: uuid.UUID, as_at: date, require_approval: bool, f: ItemFilter
) -> Subquery:
    """The WIP ledger as at the end of a day: one row per billable item dated on or before
    it that was not yet cleared then.

    An item counts when it is ``unbilled``, or when it is ``billed``/``written_off`` by an
    invoice issued or a write-off dated after the day (so a month-end WIP run later still
    shows what was unbilled at month end). Items ``in_draft`` are not WIP (as in the WIP
    module). Columns: ``client_id, job_id, item_date, minutes, time_value,
    disbursement_value``. Time on a timesheet that is not approved is left out when the
    practice requires approval.
    """
    time_q = _time_select(
        organization_id,
        TimeEntry.client_id.label("client_id"),
        TimeEntry.job_id.label("job_id"),
        TimeEntry.entry_date.label("item_date"),
        TimeEntry.minutes.label("minutes"),
        TimeEntry.charge_value.label("time_value"),
        literal(0, BigInteger).label("disbursement_value"),
    ).where(
        TimeEntry.is_billable.is_(True),
        _wip_status(
            organization_id, as_at, TimeEntry.billing_status, "time_entry_id", TimeEntry.id
        ),
        TimeEntry.entry_date <= as_at,
        *item_conditions(
            f,
            client_col=TimeEntry.client_id,
            job_col=TimeEntry.job_id,
            user_col=TimeEntry.user_id,
            activity_col=TimeEntry.activity_code_id,
        ),
    )
    if require_approval:
        time_q = time_q.where(
            TimeEntry.timesheet_id.in_(
                select(Timesheet.id).where(
                    Timesheet.organization_id == organization_id,
                    or_(Timesheet.status == "approved", Timesheet.approval_exempt.is_(True)),
                )
            )
        )
    disbursement_q = (
        select(
            Disbursement.client_id.label("client_id"),
            Disbursement.job_id.label("job_id"),
            Disbursement.incurred_on.label("item_date"),
            literal(0, Integer).label("minutes"),
            literal(0, BigInteger).label("time_value"),
            Disbursement.amount.label("disbursement_value"),
        )
        .select_from(Disbursement)
        .outerjoin(
            Job,
            and_(
                Job.organization_id == Disbursement.organization_id,
                Job.id == Disbursement.job_id,
            ),
        )
        .join(
            Client,
            and_(
                Client.organization_id == Disbursement.organization_id,
                Client.id == Disbursement.client_id,
            ),
        )
        .where(
            Disbursement.organization_id == organization_id,
            Disbursement.is_billable.is_(True),
            _wip_status(
                organization_id,
                as_at,
                Disbursement.billing_status,
                "disbursement_id",
                Disbursement.id,
            ),
            Disbursement.incurred_on <= as_at,
            *item_conditions(
                f,
                client_col=Disbursement.client_id,
                job_col=Disbursement.job_id,
                user_col=None,
                activity_col=None,
            ),
        )
    )
    return union_all(time_q, disbursement_q).subquery("wip_items")


def age_buckets(item_date: Any, value: Any, as_at: date) -> dict[str, ColumnElement[int]]:
    """Value split by age in days (``as_at - date``): 0-30, 31-60, 61-90, 91+."""
    d30, d60, d90 = (as_at - timedelta(days=n) for n in (30, 60, 90))
    return {
        "age_0_30": total(when(item_date >= d30, value)),
        "age_31_60": total(when(and_(item_date < d30, item_date >= d60), value)),
        "age_61_90": total(when(and_(item_date < d60, item_date >= d90), value)),
        "age_91_plus": total(when(item_date < d90, value)),
    }


def invoice_balances(organization_id: uuid.UUID, as_at: date, f: ItemFilter) -> Subquery:
    """What each issued invoice dated on or before ``as_at`` was owed at the end of that day.

    ``gross - payments received by then - credit notes issued by then``. Void and draft
    invoices are left out. Columns: ``invoice_id, client_id, due_date, balance``.
    """
    paid = (
        select(
            PaymentAllocation.invoice_id.label("invoice_id"),
            total(PaymentAllocation.amount).label("amount"),
        )
        .join(
            Payment,
            and_(
                Payment.organization_id == PaymentAllocation.organization_id,
                Payment.id == PaymentAllocation.payment_id,
            ),
        )
        .where(
            PaymentAllocation.organization_id == organization_id,
            Payment.received_on <= as_at,
            Payment.voided_at.is_(None),
        )
        .group_by(PaymentAllocation.invoice_id)
        .subquery("paid")
    )
    credited = (
        select(
            CreditNote.invoice_id.label("invoice_id"),
            total(CreditNote.gross_total).label("amount"),
        )
        .where(CreditNote.organization_id == organization_id, CreditNote.issue_date <= as_at)
        .group_by(CreditNote.invoice_id)
        .subquery("credited")
    )
    balance = (
        Invoice.gross_total - func.coalesce(paid.c.amount, 0) - func.coalesce(credited.c.amount, 0)
    )
    return (
        select(
            Invoice.id.label("invoice_id"),
            Invoice.client_id.label("client_id"),
            Invoice.due_date.label("due_date"),
            cast(balance, BigInteger).label("balance"),
        )
        .select_from(Invoice)
        .join(
            Client,
            and_(Client.organization_id == Invoice.organization_id, Client.id == Invoice.client_id),
        )
        .outerjoin(paid, paid.c.invoice_id == Invoice.id)
        .outerjoin(credited, credited.c.invoice_id == Invoice.id)
        .where(
            Invoice.organization_id == organization_id,
            Invoice.status == "issued",
            Invoice.issue_date <= as_at,
            *client_conditions(f),
        )
        .subquery("balances")
    )


def debt_buckets(due_date: Any, value: Any, as_at: date) -> dict[str, ColumnElement[int]]:
    """Value split by days past the due date as at ``as_at``; not yet due is ``current``."""
    d30, d60, d90 = (as_at - timedelta(days=n) for n in (30, 60, 90))
    return {
        "current": total(when(due_date >= as_at, value)),
        "days_1_30": total(when(and_(due_date < as_at, due_date >= d30), value)),
        "days_31_60": total(when(and_(due_date < d30, due_date >= d60), value)),
        "days_61_90": total(when(and_(due_date < d60, due_date >= d90), value)),
        "days_91_plus": total(when(due_date < d90, value)),
    }


def _allocation_select(
    organization_id: uuid.UUID, period: DateRange, *columns: Any, with_client: bool = True
) -> AnySelect:
    """Live WIP allocations of issued invoices and write-offs dated in ``period``, joined to
    their item, invoice or write-off, the item's job (outer) and client."""
    client_id = func.coalesce(TimeEntry.client_id, Disbursement.client_id)
    job_id = func.coalesce(TimeEntry.job_id, Disbursement.job_id)
    stmt = (
        select(*columns)
        .select_from(WipAllocation)
        .outerjoin(
            TimeEntry,
            and_(
                TimeEntry.organization_id == WipAllocation.organization_id,
                TimeEntry.id == WipAllocation.time_entry_id,
            ),
        )
        .outerjoin(
            Disbursement,
            and_(
                Disbursement.organization_id == WipAllocation.organization_id,
                Disbursement.id == WipAllocation.disbursement_id,
            ),
        )
        .outerjoin(
            Invoice,
            and_(
                Invoice.organization_id == WipAllocation.organization_id,
                Invoice.id == WipAllocation.invoice_id,
                Invoice.status == "issued",
                *period.contains(Invoice.issue_date),
            ),
        )
        .outerjoin(
            WipWriteOff,
            and_(
                WipWriteOff.organization_id == WipAllocation.organization_id,
                WipWriteOff.id == WipAllocation.write_off_id,
                *period.contains(WipWriteOff.written_off_on),
            ),
        )
        .outerjoin(
            Job, and_(Job.organization_id == WipAllocation.organization_id, Job.id == job_id)
        )
    )
    if with_client:
        stmt = stmt.join(
            Client,
            and_(Client.organization_id == WipAllocation.organization_id, Client.id == client_id),
        )
    return stmt.where(
        WipAllocation.organization_id == organization_id,
        WipAllocation.reversed_at.is_(None),
        or_(Invoice.id.is_not(None), WipWriteOff.id.is_not(None)),
    )


def _member_name_join(stmt: AnySelect, organization_id: uuid.UUID, user_col: Any) -> AnySelect:
    return stmt.outerjoin(
        OrganizationMember,
        and_(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id == user_col,
        ),
    ).outerjoin(User, User.id == OrganizationMember.user_id)


class ReportRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _rows(self, stmt: AnySelect) -> Sequence[RowMapping]:
        rows: Sequence[RowMapping] = (await self.session.execute(stmt)).mappings().all()
        return rows

    # --- references -----------------------------------------------------------------

    async def client_exists(self, organization_id: uuid.UUID, client_id: uuid.UUID) -> bool:
        stmt = select(Client.id).where(
            Client.organization_id == organization_id, Client.id == client_id
        )
        return (await self.session.scalar(stmt)) is not None

    async def job_exists(self, organization_id: uuid.UUID, job_id: uuid.UUID) -> bool:
        stmt = select(Job.id).where(Job.organization_id == organization_id, Job.id == job_id)
        return (await self.session.scalar(stmt)) is not None

    async def member_exists(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        stmt = select(OrganizationMember.id).where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id == user_id,
        )
        return (await self.session.scalar(stmt)) is not None

    async def service_exists(self, organization_id: uuid.UUID, service_id: uuid.UUID) -> bool:
        stmt = select(Service.id).where(
            Service.organization_id == organization_id, Service.id == service_id
        )
        return (await self.session.scalar(stmt)) is not None

    async def activity_code_exists(
        self, organization_id: uuid.UUID, activity_code_id: uuid.UUID
    ) -> bool:
        stmt = select(ActivityCode.id).where(
            ActivityCode.organization_id == organization_id, ActivityCode.id == activity_code_id
        )
        return (await self.session.scalar(stmt)) is not None

    async def member_names(
        self, organization_id: uuid.UUID, user_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        if not user_ids:
            return {}
        stmt = (
            select(OrganizationMember.user_id, User.full_name)
            .join(User, User.id == OrganizationMember.user_id)
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id.in_(user_ids),
            )
        )
        return {row.user_id: row.full_name for row in await self.session.execute(stmt)}

    # --- WIP --------------------------------------------------------------------------

    async def wip(
        self, organization_id: uuid.UUID, as_at: date, require_approval: bool, f: ItemFilter
    ) -> Sequence[RowMapping]:
        items = wip_items(organization_id, as_at, require_approval, f)
        value = items.c.time_value + items.c.disbursement_value
        stmt = (
            select(
                items.c.client_id,
                Client.code.label("client_code"),
                Client.name.label("client_name"),
                items.c.job_id,
                Job.number.label("job_number"),
                Job.name.label("job_name"),
                total(items.c.minutes).label("minutes"),
                total(items.c.time_value).label("time_value"),
                total(items.c.disbursement_value).label("disbursement_value"),
                total(value).label("total"),
                func.min(items.c.item_date).label("oldest_item_date"),
                *(
                    expr.label(name)
                    for name, expr in age_buckets(items.c.item_date, value, as_at).items()
                ),
            )
            .select_from(items)
            .join(
                Client,
                and_(Client.organization_id == organization_id, Client.id == items.c.client_id),
            )
            .outerjoin(Job, and_(Job.organization_id == organization_id, Job.id == items.c.job_id))
            .group_by(
                items.c.client_id, Client.code, Client.name, items.c.job_id, Job.number, Job.name
            )
            .order_by(func.lower(Client.name), Client.code, Job.number.nulls_first())
        )
        return await self._rows(stmt)

    # --- Aged debtors -----------------------------------------------------------------

    async def aged_debtors(
        self, organization_id: uuid.UUID, as_at: date, f: ItemFilter
    ) -> Sequence[RowMapping]:
        balances = invoice_balances(organization_id, as_at, f)
        stmt = (
            select(
                balances.c.client_id,
                Client.code.label("client_code"),
                Client.name.label("client_name"),
                func.count().label("invoice_count"),
                func.min(balances.c.due_date).label("oldest_due_date"),
                total(balances.c.balance).label("total"),
                *(
                    expr.label(name)
                    for name, expr in debt_buckets(
                        balances.c.due_date, balances.c.balance, as_at
                    ).items()
                ),
            )
            .select_from(balances)
            .join(
                Client,
                and_(Client.organization_id == organization_id, Client.id == balances.c.client_id),
            )
            .where(balances.c.balance > 0)
            .group_by(balances.c.client_id, Client.code, Client.name)
            .order_by(func.lower(Client.name), Client.code, balances.c.client_id)
        )
        return await self._rows(stmt)

    # --- Time summary -----------------------------------------------------------------

    async def time_summary(
        self,
        organization_id: uuid.UUID,
        period: DateRange,
        group_by: str,
        week_start: int,
        f: ItemFilter,
    ) -> Sequence[RowMapping]:
        """Rows ``key, code, name, client_name`` plus the time measures, per group."""
        entry_date = TimeEntry.entry_date
        key: Any
        code: Any = null()
        name: Any = null()
        client_name: Any = null()
        client_id: Any = null()
        match group_by:
            case "person":
                key, code, name = TimeEntry.user_id, OrganizationMember.initials, User.full_name
            case "client":
                key, code, name = TimeEntry.client_id, Client.code, Client.name
            case "job":
                key, code, name = TimeEntry.job_id, Job.number, Job.name
                client_name, client_id = Client.name, Client.id
            case "activity":
                key, code, name = TimeEntry.activity_code_id, ActivityCode.code, ActivityCode.name
            case "service":
                key, code, name = Job.service_id, Service.code, Service.name
            case "day":
                key = entry_date
            case "week":
                dow = cast(extract("isodow", entry_date), Integer)
                offset = func.mod(dow - _int(week_start) + _int(7), _int(7))
                key = entry_date - cast(offset, Integer)
            case "month":
                key = first_of_month(entry_date)
            case _:  # pragma: no cover - validated by the schema
                raise ValueError(group_by)
        chargeable = TimeEntry.is_billable.is_(True)
        stmt = _time_select(
            organization_id,
            key.label("key"),
            code.label("code"),
            name.label("name"),
            client_name.label("client_name"),
            client_id.label("client_id"),
            func.count(TimeEntry.id).label("entry_count"),
            total(TimeEntry.minutes).label("total_minutes"),
            total(when(chargeable, TimeEntry.minutes)).label("chargeable_minutes"),
            total(when(chargeable, TimeEntry.charge_value)).label("charge_value"),
            total(TimeEntry.cost_value).label("cost_value"),
        ).where(
            *period.contains(entry_date),
            *item_conditions(
                f,
                client_col=TimeEntry.client_id,
                job_col=TimeEntry.job_id,
                user_col=TimeEntry.user_id,
                activity_col=TimeEntry.activity_code_id,
            ),
        )
        if group_by == "person":
            stmt = _member_name_join(stmt, organization_id, TimeEntry.user_id)
        elif group_by == "activity":
            stmt = stmt.outerjoin(
                ActivityCode,
                and_(
                    ActivityCode.organization_id == TimeEntry.organization_id,
                    ActivityCode.id == TimeEntry.activity_code_id,
                ),
            )
        elif group_by == "service":
            stmt = stmt.outerjoin(
                Service,
                and_(Service.organization_id == Job.organization_id, Service.id == Job.service_id),
            )
        group_columns = [c for c in (key, code, name, client_name, client_id) if not _is_null(c)]
        stmt = stmt.group_by(*group_columns)
        if group_by in ("day", "week", "month"):
            stmt = stmt.order_by(key)
        else:
            stmt = stmt.order_by(func.lower(name).nulls_last(), code.nulls_last(), key)
        return await self._rows(stmt)

    # --- Utilisation ------------------------------------------------------------------

    async def utilisation(
        self,
        organization_id: uuid.UUID,
        period: DateRange,
        *,
        user_id: uuid.UUID | None,
        manager_user_id: uuid.UUID | None,
    ) -> Sequence[RowMapping]:
        """Every member who is not removed, or who logged time in the period."""
        chargeable = TimeEntry.is_billable.is_(True)
        minutes = (
            select(
                TimeEntry.user_id.label("user_id"),
                total(TimeEntry.minutes).label("total_minutes"),
                total(when(chargeable, TimeEntry.minutes)).label("chargeable_minutes"),
            )
            .where(
                TimeEntry.organization_id == organization_id, *period.contains(TimeEntry.entry_date)
            )
            .group_by(TimeEntry.user_id)
            .subquery("minutes")
        )
        stmt = (
            select(
                OrganizationMember.user_id,
                User.full_name,
                OrganizationMember.initials,
                OrganizationMember.status,
                OrganizationMember.weekly_capacity_minutes,
                OrganizationMember.chargeable_target_bp,
                func.coalesce(minutes.c.total_minutes, 0).label("total_minutes"),
                func.coalesce(minutes.c.chargeable_minutes, 0).label("chargeable_minutes"),
            )
            .join(User, User.id == OrganizationMember.user_id)
            .outerjoin(minutes, minutes.c.user_id == OrganizationMember.user_id)
            .where(
                OrganizationMember.organization_id == organization_id,
                or_(
                    OrganizationMember.status.not_in(GONE_STATUSES),
                    minutes.c.user_id.is_not(None),
                ),
            )
            .order_by(func.lower(User.full_name), OrganizationMember.user_id)
        )
        if user_id is not None:
            stmt = stmt.where(OrganizationMember.user_id == user_id)
        if manager_user_id is not None:
            stmt = stmt.where(OrganizationMember.manager_user_id == manager_user_id)
        return await self._rows(stmt)

    # --- Recoverability ---------------------------------------------------------------

    async def recoverability(
        self, organization_id: uuid.UUID, period: DateRange, group_by: str, f: ItemFilter
    ) -> Sequence[RowMapping]:
        client_id = func.coalesce(TimeEntry.client_id, Disbursement.client_id)
        job_id = func.coalesce(TimeEntry.job_id, Disbursement.job_id)
        key: Any
        code: Any
        name: Any
        client_name: Any = null()
        group_client_id: Any = null()
        match group_by:
            case "client":
                key, code, name = Client.id, Client.code, Client.name
            case "job":
                key, code, name = job_id, Job.number, Job.name
                client_name, group_client_id = Client.name, Client.id
            case "person":
                key, code, name = TimeEntry.user_id, OrganizationMember.initials, User.full_name
            case _:  # pragma: no cover - validated by the schema
                raise ValueError(group_by)
        on_invoice = Invoice.id.is_not(None)
        stmt = _allocation_select(
            organization_id,
            period,
            key.label("key"),
            code.label("code"),
            name.label("name"),
            client_name.label("client_name"),
            group_client_id.label("client_id"),
            total(when(on_invoice, WipAllocation.wip_value)).label("billed_wip_value"),
            total(when(on_invoice, WipAllocation.billed_value)).label("billed_value"),
            total(when(WipWriteOff.id.is_not(None), WipAllocation.wip_value)).label(
                "written_off_value"
            ),
        ).where(
            *item_conditions(
                f,
                client_col=client_id,
                job_col=job_id,
                user_col=TimeEntry.user_id,
                activity_col=None,
            )
        )
        if group_by == "person":
            stmt = _member_name_join(stmt, organization_id, TimeEntry.user_id)
        group_columns = [
            c for c in (key, code, name, client_name, group_client_id) if not _is_null(c)
        ]
        stmt = stmt.group_by(*group_columns).order_by(
            func.lower(name).nulls_last(), func.lower(client_name), code.nulls_last(), key
        )
        return await self._rows(stmt)

    # --- Billing summary --------------------------------------------------------------

    async def billing_documents(
        self, organization_id: uuid.UUID, period: DateRange, group_by: str, f: ItemFilter
    ) -> tuple[Sequence[RowMapping], Sequence[RowMapping], Sequence[RowMapping]]:
        """Invoices issued, credit notes and receipts in the period, grouped by ``key``
        (the first day of the month, or the client's partner)."""

        def key_of(day: Any) -> Any:
            if group_by == "partner":
                return Client.partner_user_id
            return first_of_month(day)

        def with_client(stmt: AnySelect, client_col: Any, org_col: Any) -> AnySelect:
            return stmt.join(
                Client, and_(Client.organization_id == org_col, Client.id == client_col)
            ).where(*client_conditions(f))

        invoice_key = key_of(Invoice.issue_date)
        invoices = with_client(
            select(
                invoice_key.label("key"),
                func.count(Invoice.id).label("count"),
                total(Invoice.net_total).label("net"),
                total(Invoice.vat_total).label("vat"),
                total(Invoice.gross_total).label("gross"),
            ).where(
                Invoice.organization_id == organization_id,
                Invoice.status == "issued",
                *period.contains(Invoice.issue_date),
            ),
            Invoice.client_id,
            Invoice.organization_id,
        ).group_by(invoice_key)
        credit_key = key_of(CreditNote.issue_date)
        credits = with_client(
            select(
                credit_key.label("key"),
                func.count(CreditNote.id).label("count"),
                total(CreditNote.net_total).label("net"),
                total(CreditNote.vat_total).label("vat"),
                total(CreditNote.gross_total).label("gross"),
            ).where(
                CreditNote.organization_id == organization_id,
                *period.contains(CreditNote.issue_date),
            ),
            CreditNote.client_id,
            CreditNote.organization_id,
        ).group_by(credit_key)
        receipt_key = key_of(Payment.received_on)
        receipts = with_client(
            select(receipt_key.label("key"), total(Payment.amount).label("amount")).where(
                Payment.organization_id == organization_id,
                live_payment(),
                *period.contains(Payment.received_on),
            ),
            Payment.client_id,
            Payment.organization_id,
        ).group_by(receipt_key)
        return await self._rows(invoices), await self._rows(credits), await self._rows(receipts)

    # --- Job profitability ------------------------------------------------------------

    async def job_profitability(
        self, organization_id: uuid.UUID, period: DateRange, f: ItemFilter
    ) -> Sequence[RowMapping]:
        """Jobs with time logged or billing/write-offs in the period."""
        billable = TimeEntry.is_billable.is_(True)
        time_q = (
            select(
                TimeEntry.job_id.label("job_id"),
                total(TimeEntry.minutes).label("logged_minutes"),
                total(when(billable, TimeEntry.charge_value)).label("time_value"),
                total(TimeEntry.cost_value).label("cost_value"),
            )
            .where(
                TimeEntry.organization_id == organization_id,
                TimeEntry.job_id.is_not(None),
                *period.contains(TimeEntry.entry_date),
            )
            .group_by(TimeEntry.job_id)
            .subquery("job_time")
        )
        job_expr = func.coalesce(TimeEntry.job_id, Disbursement.job_id)
        on_invoice = Invoice.id.is_not(None)
        billing_q = (
            _allocation_select(
                organization_id,
                period,
                job_expr.label("job_id"),
                total(
                    when(
                        and_(on_invoice, WipAllocation.time_entry_id.is_not(None)),
                        WipAllocation.billed_value,
                    )
                ).label("billed_time_value"),
                total(
                    when(
                        and_(on_invoice, WipAllocation.disbursement_id.is_not(None)),
                        WipAllocation.billed_value,
                    )
                ).label("billed_disbursement_value"),
                total(when(WipWriteOff.id.is_not(None), WipAllocation.wip_value)).label(
                    "written_off_value"
                ),
                with_client=False,
            )
            .where(job_expr.is_not(None))
            .group_by(job_expr)
            .subquery("job_billing")
        )
        stmt = (
            select(
                Job.id.label("job_id"),
                Job.number.label("job_number"),
                Job.name.label("job_name"),
                Job.status,
                Job.billing_basis,
                Job.fixed_fee,
                Job.budget_minutes,
                Job.budget_fee,
                Client.id.label("client_id"),
                Client.code.label("client_code"),
                Client.name.label("client_name"),
                func.coalesce(time_q.c.logged_minutes, 0).label("logged_minutes"),
                func.coalesce(time_q.c.time_value, 0).label("time_value"),
                func.coalesce(time_q.c.cost_value, 0).label("cost_value"),
                func.coalesce(billing_q.c.billed_time_value, 0).label("billed_time_value"),
                func.coalesce(billing_q.c.billed_disbursement_value, 0).label(
                    "billed_disbursement_value"
                ),
                func.coalesce(billing_q.c.written_off_value, 0).label("written_off_value"),
            )
            .select_from(Job)
            .join(
                Client,
                and_(Client.organization_id == Job.organization_id, Client.id == Job.client_id),
            )
            .outerjoin(time_q, time_q.c.job_id == Job.id)
            .outerjoin(billing_q, billing_q.c.job_id == Job.id)
            .where(
                Job.organization_id == organization_id,
                Job.deleted_at.is_(None),
                or_(time_q.c.job_id.is_not(None), billing_q.c.job_id.is_not(None)),
                *item_conditions(
                    f, client_col=Job.client_id, job_col=Job.id, user_col=None, activity_col=None
                ),
            )
            .order_by(func.lower(Client.name), Client.code, Job.number)
        )
        return await self._rows(stmt)

    # --- VAT summary ------------------------------------------------------------------

    async def vat_summary(
        self, organization_id: uuid.UUID, period: DateRange, client_id: uuid.UUID | None
    ) -> tuple[Sequence[RowMapping], Sequence[RowMapping]]:
        """Net and VAT per (rate, snapshotted bp) on invoices issued and credit notes."""

        def by_rate(line: Any, doc: Any, doc_fk: Any) -> AnySelect:
            stmt = (
                select(
                    line.vat_rate_id.label("vat_rate_id"),
                    line.vat_rate_bp.label("rate_bp"),
                    VatRate.name.label("name"),
                    VatRate.code.label("code"),
                    total(line.net_amount).label("net"),
                    total(line.vat_amount).label("vat"),
                )
                .select_from(line)
                .join(doc, and_(doc.organization_id == line.organization_id, doc.id == doc_fk))
                .outerjoin(
                    VatRate,
                    and_(
                        VatRate.organization_id == line.organization_id,
                        VatRate.id == line.vat_rate_id,
                    ),
                )
                .where(line.organization_id == organization_id, *period.contains(doc.issue_date))
                .group_by(line.vat_rate_id, line.vat_rate_bp, VatRate.name, VatRate.code)
            )
            if client_id is not None:
                stmt = stmt.where(doc.client_id == client_id)
            return stmt

        invoiced = by_rate(InvoiceLine, Invoice, InvoiceLine.invoice_id).where(
            Invoice.status == "issued"
        )
        credited = by_rate(CreditNoteLine, CreditNote, CreditNoteLine.credit_note_id)
        return await self._rows(invoiced), await self._rows(credited)


def _is_null(column: Any) -> bool:
    return isinstance(column, Null)
