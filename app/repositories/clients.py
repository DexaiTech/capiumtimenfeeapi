"""Client, contact and engagement queries. Every method is scoped to one organization.

The client list is one keyset-paginated query; the derived columns of a page (primary
contact, engagements, deadlines) are fetched with one batched query each, never per row.
"""

import base64
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    ColumnElement,
    DateTime,
    Select,
    and_,
    delete,
    exists,
    func,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.db.models import (
    Client,
    ClientContact,
    ClientService,
    CreditNote,
    Disbursement,
    Expense,
    Invoice,
    Job,
    OrganizationMember,
    Payment,
    Service,
    TimeEntry,
    Timesheet,
    User,
    VatRate,
)
from app.schemas.clients import ClientFilters

OPEN_JOB_STATUSES = ("planned", "in_progress", "on_hold")


@dataclass(frozen=True, slots=True)
class ClientRow:
    client: Client
    last_activity_at: datetime


@dataclass(frozen=True, slots=True)
class ClientPage:
    rows: list[ClientRow]
    next_cursor: str | None


@dataclass(slots=True)
class DeadlineCounts:
    open_jobs: int = 0
    overdue: int = 0
    oldest_overdue: date | None = None
    due_today: int = 0
    due_soon: int = 0
    next_due_date: date | None = None


@dataclass(slots=True)
class SummaryCounts:
    by_status: dict[str, int] = field(default_factory=dict)
    by_client_type: dict[str, int] = field(default_factory=dict)
    by_aml_status: dict[str, int] = field(default_factory=dict)
    by_accounting_software: dict[str, int] = field(default_factory=dict)
    by_segment: list[tuple[str | None, int]] = field(default_factory=list)
    by_central: dict[str, int] = field(default_factory=dict)
    with_overdue_deadlines: int = 0
    users: int = 0


@dataclass(frozen=True, slots=True)
class ClientUsage:
    jobs: int
    time_entries: int
    disbursements: int
    expenses: int
    invoices: int
    credit_notes: int
    payments: int

    @property
    def in_use(self) -> bool:
        return any(
            (
                self.jobs,
                self.time_entries,
                self.disbursements,
                self.expenses,
                self.invoices,
                self.credit_notes,
                self.payments,
            )
        )


# --- Keyset cursor carrying the sort ---------------------------------------------------


def encode_client_cursor(sort: str, descending: bool, value: Any, row_id: uuid.UUID) -> str:
    kind = "dt" if isinstance(value, datetime) else "raw"
    payload = {
        "s": sort,
        "d": descending,
        "k": kind,
        "v": value.isoformat() if isinstance(value, datetime) else value,
        "id": str(row_id),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


_DATETIME_SORTS = frozenset({"created_at", "last_activity_at"})
_INT_SORTS = frozenset({"health_score"})


def _cursor_value(sort: str, kind: Any, value: Any) -> Any:
    """The sort key of a cursor, checked against the type the sort's SQL key has: a
    tampered cursor must answer 422, never reach the database as a mistyped parameter."""
    if sort in _DATETIME_SORTS:
        if kind != "dt" or not isinstance(value, str):
            raise ValueError("expected a timestamp")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("expected an aware timestamp")
        return parsed
    if kind != "raw":
        raise ValueError("bad kind")
    if sort in _INT_SORTS:
        # Health scores are 0-100; a null score sorts as -1.
        if type(value) is not int or not -1 <= value <= 100:
            raise ValueError("expected a health score")
        return value
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("expected text")
    return value


def decode_client_cursor(cursor: str, sort: str, descending: bool) -> tuple[Any, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if data["s"] != sort or bool(data["d"]) != descending:
            raise ValueError("cursor of another sort")
        value = _cursor_value(sort, data["k"], data["v"])
        if not isinstance(data["id"], str):
            raise ValueError("bad id")
        return value, uuid.UUID(data["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ValidationError.for_field(
            "cursor", "Invalid cursor (the sort or filters may have changed)."
        ) from exc


def last_activity_expr() -> ColumnElement[datetime]:
    """Greatest of the client's own ``updated_at`` and its latest job, time and invoice
    change. ``GREATEST`` ignores the nulls of a client with none of those."""

    def latest(column: Any, model: Any) -> Any:
        return (
            select(func.max(column))
            .where(model.organization_id == Client.organization_id, model.client_id == Client.id)
            .correlate(Client)
            .scalar_subquery()
        )

    return func.greatest(
        Client.updated_at,
        latest(TimeEntry.updated_at, TimeEntry),
        latest(Job.updated_at, Job),
        latest(Invoice.updated_at, Invoice),
        type_=DateTime(timezone=True),
    )


def _sort_expr(sort: str, activity: ColumnElement[datetime]) -> ColumnElement[Any]:
    match sort:
        case "code":
            return Client.code.expression
        case "segment":
            return func.coalesce(func.lower(Client.segment), "")
        case "health_score":
            return func.coalesce(Client.health_score, -1)
        case "aml_status":
            return Client.aml_status.expression
        case "created_at":
            return Client.created_at.expression
        case "last_activity_at":
            return activity
        case _:
            return func.lower(Client.name)


class ClientRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, client: Client) -> Client:
        self.session.add(client)
        await self.session.flush()
        await self.session.refresh(client)
        return client

    async def add_all(self, clients: Sequence[Client]) -> None:
        self.session.add_all(clients)
        await self.session.flush()

    async def save(self, client: Client) -> Client:
        await self.session.flush()
        await self.session.refresh(client)
        return client

    async def lock_many(
        self, organization_id: uuid.UUID, client_ids: Sequence[uuid.UUID]
    ) -> list[Client]:
        """Live clients among ``client_ids`` locked ``FOR UPDATE`` in id order (no deadlocks)."""
        stmt = (
            select(Client)
            .where(
                Client.organization_id == organization_id,
                Client.id.in_(list(client_ids)),
                Client.deleted_at.is_(None),
            )
            .order_by(Client.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def get(
        self, organization_id: uuid.UUID, client_id: uuid.UUID, *, lock: str | None = None
    ) -> Client | None:
        """A live (not deleted) client.

        ``lock``: ``"update"`` (FOR UPDATE: status changes and deletion), ``"no_key_update"``
        (FOR NO KEY UPDATE: serialises edits such as contact changes without blocking work
        being added) or ``"key_share"`` (FOR KEY SHARE: work being added to the client;
        blocks a concurrent status change or delete until the caller commits).
        """
        stmt = select(Client).where(
            Client.organization_id == organization_id,
            Client.id == client_id,
            Client.deleted_at.is_(None),
        )
        if lock == "update":
            stmt = stmt.with_for_update()
        elif lock == "no_key_update":
            stmt = stmt.with_for_update(key_share=True)
        elif lock == "key_share":
            stmt = stmt.with_for_update(read=True, key_share=True)
        stmt = stmt.execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def code_holder(
        self, organization_id: uuid.UUID, code: str, exclude_id: uuid.UUID | None = None
    ) -> Client | None:
        """The live (not deleted) client using ``code``, if any."""
        stmt = select(Client).where(
            Client.organization_id == organization_id,
            Client.code == code,
            Client.deleted_at.is_(None),
        )
        if exclude_id is not None:
            stmt = stmt.where(Client.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).scalar_one_or_none()

    async def codes_taken(self, organization_id: uuid.UUID, codes: set[str]) -> set[str]:
        if not codes:
            return set()
        stmt = select(Client.code).where(
            Client.organization_id == organization_id,
            Client.code.in_(codes),
            Client.deleted_at.is_(None),
        )
        return set((await self.session.execute(stmt)).scalars())

    # --- The list ---------------------------------------------------------------------

    def _filtered(
        self,
        organization_id: uuid.UUID,
        filters: ClientFilters,
        activity: ColumnElement[datetime],
    ) -> Select[Client, datetime]:
        stmt = select(Client, activity.label("last_activity_at")).where(
            Client.organization_id == organization_id, Client.deleted_at.is_(None)
        )
        if filters.statuses:
            stmt = stmt.where(Client.status.in_(filters.statuses))
        else:
            stmt = stmt.where(Client.status != "archived")
        if filters.segments:
            stmt = stmt.where(func.lower(Client.segment).in_([s.lower() for s in filters.segments]))
        if filters.client_types:
            stmt = stmt.where(Client.client_type.in_(filters.client_types))
        if filters.aml_statuses:
            stmt = stmt.where(Client.aml_status.in_(filters.aml_statuses))
        if filters.accounting_software:
            stmt = stmt.where(Client.accounting_software.in_(filters.accounting_software))
        if filters.manager_user_id is not None:
            stmt = stmt.where(Client.manager_user_id == filters.manager_user_id)
        if filters.partner_user_id is not None:
            stmt = stmt.where(Client.partner_user_id == filters.partner_user_id)
        if filters.central == "linked":
            stmt = stmt.where(Client.central_client_id.is_not(None))
        elif filters.central == "local":
            stmt = stmt.where(Client.central_client_id.is_(None))
        if filters.service_id is not None:
            stmt = stmt.where(
                exists().where(
                    ClientService.organization_id == organization_id,
                    ClientService.client_id == Client.id,
                    ClientService.service_id == filters.service_id,
                    ClientService.is_active.is_(True),
                )
            )
        if filters.q:
            q = filters.q
            stmt = stmt.where(
                or_(
                    Client.name.icontains(q, autoescape=True),
                    Client.code.icontains(q, autoescape=True),
                    Client.company_number.icontains(q, autoescape=True),
                    Client.email.icontains(q, autoescape=True),
                    exists().where(
                        ClientContact.organization_id == organization_id,
                        ClientContact.client_id == Client.id,
                        ClientContact.email.icontains(q, autoescape=True),
                    ),
                )
            )
        return stmt

    async def list_page(
        self,
        organization_id: uuid.UUID,
        filters: ClientFilters,
        *,
        sort: str,
        descending: bool,
        limit: int,
        cursor: str | None,
    ) -> ClientPage:
        """One page ordered by ``(sort key, id)``; the cursor carries the sort key."""
        activity = last_activity_expr()
        key = _sort_expr(sort, activity)
        stmt = self._filtered(organization_id, filters, activity).add_columns(key.label("sort_key"))
        if cursor:
            value, row_id = decode_client_cursor(cursor, sort, descending)
            pair = tuple_(key, Client.id)
            stmt = stmt.where(pair < (value, row_id) if descending else pair > (value, row_id))
        order = (key.desc(), Client.id.desc()) if descending else (key.asc(), Client.id.asc())
        rows = (await self.session.execute(stmt.order_by(*order).limit(limit + 1))).all()
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = encode_client_cursor(sort, descending, last.sort_key, last[0].id)
        return ClientPage(
            rows=[ClientRow(client=row[0], last_activity_at=row[1]) for row in rows],
            next_cursor=next_cursor,
        )

    async def last_activity(self, organization_id: uuid.UUID, client_id: uuid.UUID) -> datetime:
        stmt = select(last_activity_expr()).where(
            Client.organization_id == organization_id, Client.id == client_id
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def primary_contacts(
        self, organization_id: uuid.UUID, client_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, ClientContact]:
        if not client_ids:
            return {}
        stmt = select(ClientContact).where(
            ClientContact.organization_id == organization_id,
            ClientContact.client_id.in_(client_ids),
            ClientContact.is_primary.is_(True),
        )
        return {c.client_id: c for c in (await self.session.execute(stmt)).scalars()}

    async def active_services(
        self, organization_id: uuid.UUID, client_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[Service]]:
        if not client_ids:
            return {}
        stmt = (
            select(ClientService.client_id, Service)
            .join(
                Service,
                and_(
                    Service.organization_id == ClientService.organization_id,
                    Service.id == ClientService.service_id,
                ),
            )
            .where(
                ClientService.organization_id == organization_id,
                ClientService.client_id.in_(client_ids),
                ClientService.is_active.is_(True),
            )
            .order_by(ClientService.client_id, Service.sort_order, Service.name)
        )
        result: dict[uuid.UUID, list[Service]] = {}
        for client_id, service in (await self.session.execute(stmt)).all():
            result.setdefault(client_id, []).append(service)
        return result

    async def deadlines(
        self,
        organization_id: uuid.UUID,
        client_ids: Sequence[uuid.UUID],
        *,
        today: date,
        soon_until: date,
    ) -> dict[uuid.UUID, DeadlineCounts]:
        """Counts of each client's open jobs by due date relative to ``today``."""
        if not client_ids:
            return {}
        due = Job.due_date
        stmt = (
            select(
                Job.client_id,
                func.count(),
                func.count().filter(due < today),
                func.min(due).filter(due < today),
                func.count().filter(due == today),
                func.count().filter(due > today, due <= soon_until),
                func.min(due).filter(due >= today),
            )
            .where(
                Job.organization_id == organization_id,
                Job.client_id.in_(client_ids),
                Job.deleted_at.is_(None),
                Job.status.in_(OPEN_JOB_STATUSES),
            )
            .group_by(Job.client_id)
        )
        return {
            row[0]: DeadlineCounts(
                open_jobs=row[1],
                overdue=row[2],
                oldest_overdue=row[3],
                due_today=row[4],
                due_soon=row[5],
                next_due_date=row[6],
            )
            for row in (await self.session.execute(stmt)).all()
        }

    async def iter_all(
        self,
        organization_id: uuid.UUID,
        filters: ClientFilters,
        *,
        sort: str,
        descending: bool,
        batch: int,
    ) -> list[ClientPage]:
        """Every matching client, as consecutive pages of ``batch`` (for the export)."""
        pages: list[ClientPage] = []
        cursor: str | None = None
        while True:
            page = await self.list_page(
                organization_id,
                filters,
                sort=sort,
                descending=descending,
                limit=batch,
                cursor=cursor,
            )
            pages.append(page)
            if page.next_cursor is None:
                return pages
            cursor = page.next_cursor

    # --- Summary ----------------------------------------------------------------------

    async def summary(self, organization_id: uuid.UUID, *, today: date) -> SummaryCounts:
        live = and_(Client.organization_id == organization_id, Client.deleted_at.is_(None))
        visible = and_(live, Client.status != "archived")
        counts = SummaryCounts()
        by_status = select(Client.status, func.count()).where(live).group_by(Client.status)
        counts.by_status = dict((await self.session.execute(by_status)).all())
        for column, target in (
            (Client.client_type, counts.by_client_type),
            (Client.aml_status, counts.by_aml_status),
            (Client.accounting_software, counts.by_accounting_software),
        ):
            stmt = select(column, func.count()).where(visible, column.is_not(None)).group_by(column)
            target.update({str(k): v for k, v in (await self.session.execute(stmt)).all()})
        segment = func.nullif(func.trim(Client.segment), "")
        seg_stmt = (
            select(func.min(segment), func.count())
            .where(visible)
            .group_by(func.lower(segment))
            .order_by(func.lower(segment).nulls_last())
        )
        counts.by_segment = [(k, v) for k, v in (await self.session.execute(seg_stmt)).all()]
        linked = (
            await self.session.execute(
                select(
                    func.count().filter(Client.central_client_id.is_not(None)),
                    func.count().filter(Client.central_client_id.is_(None)),
                ).where(visible)
            )
        ).one()
        counts.by_central = {"linked": int(linked[0]), "local": int(linked[1])}
        overdue = select(func.count()).where(
            visible,
            exists().where(
                Job.organization_id == organization_id,
                Job.client_id == Client.id,
                Job.deleted_at.is_(None),
                Job.status.in_(OPEN_JOB_STATUSES),
                Job.due_date < today,
            ),
        )
        counts.with_overdue_deadlines = int((await self.session.execute(overdue)).scalar_one())
        users = select(func.count()).where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.status == "active",
        )
        counts.users = int((await self.session.execute(users)).scalar_one())
        return counts

    # --- Usage and money --------------------------------------------------------------

    async def usage(self, organization_id: uuid.UUID, client_id: uuid.UUID) -> ClientUsage:
        def count(model: Any, *extra: ColumnElement[bool]) -> Any:
            return (
                select(func.count())
                .where(
                    model.organization_id == organization_id, model.client_id == client_id, *extra
                )
                .scalar_subquery()
            )

        stmt = select(
            count(Job, Job.deleted_at.is_(None)),
            count(TimeEntry),
            count(Disbursement),
            count(Expense),
            count(Invoice),
            count(CreditNote),
            count(Payment),
        )
        row = (await self.session.execute(stmt)).one()
        return ClientUsage(*(int(v) for v in row))

    async def wip_time(
        self, organization_id: uuid.UUID, client_id: uuid.UUID, *, approved_only: bool
    ) -> tuple[int, int]:
        """(minutes, charge value) of the client's billable unbilled time that is WIP."""
        stmt = select(
            func.coalesce(func.sum(TimeEntry.minutes), 0),
            func.coalesce(func.sum(TimeEntry.charge_value), 0),
        ).where(
            TimeEntry.organization_id == organization_id,
            TimeEntry.client_id == client_id,
            TimeEntry.is_billable.is_(True),
            TimeEntry.billing_status == "unbilled",
        )
        if approved_only:
            stmt = stmt.join(
                Timesheet,
                and_(
                    Timesheet.organization_id == TimeEntry.organization_id,
                    Timesheet.id == TimeEntry.timesheet_id,
                ),
            ).where(or_(Timesheet.status == "approved", Timesheet.approval_exempt.is_(True)))
        row = (await self.session.execute(stmt)).one()
        return int(row[0]), int(row[1])

    async def wip_disbursements(self, organization_id: uuid.UUID, client_id: uuid.UUID) -> int:
        stmt = select(func.coalesce(func.sum(Disbursement.amount), 0)).where(
            Disbursement.organization_id == organization_id,
            Disbursement.client_id == client_id,
            Disbursement.is_billable.is_(True),
            Disbursement.billing_status == "unbilled",
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def balances(
        self, organization_id: uuid.UUID, client_id: uuid.UUID, *, today: date
    ) -> tuple[int, int, int]:
        """(outstanding, overdue, on account) for the client."""
        balance = Invoice.gross_total - Invoice.paid_total - Invoice.credited_total
        invoices = select(
            func.coalesce(func.sum(balance), 0),
            func.coalesce(func.sum(balance).filter(Invoice.due_date < today), 0),
        ).where(
            Invoice.organization_id == organization_id,
            Invoice.client_id == client_id,
            Invoice.status == "issued",
        )
        outstanding, overdue = (await self.session.execute(invoices)).one()
        on_account = select(
            func.coalesce(func.sum(Payment.amount - Payment.allocated_total), 0)
        ).where(
            Payment.organization_id == organization_id,
            Payment.client_id == client_id,
            Payment.voided_at.is_(None),
        )
        return (
            int(outstanding),
            int(overdue),
            int((await self.session.execute(on_account)).scalar_one()),
        )

    async def vat_rates_bp(
        self, organization_id: uuid.UUID, rate_ids: set[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        if not rate_ids:
            return {}
        stmt = select(VatRate.id, VatRate.rate_bp).where(
            VatRate.organization_id == organization_id, VatRate.id.in_(rate_ids)
        )
        return dict((await self.session.execute(stmt)).all())

    async def default_vat_rate_bp(self, organization_id: uuid.UUID) -> int | None:
        stmt = select(VatRate.rate_bp).where(
            VatRate.organization_id == organization_id,
            VatRate.is_default.is_(True),
            VatRate.archived_at.is_(None),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def live_vat_rate(self, organization_id: uuid.UUID, rate_id: uuid.UUID) -> bool:
        stmt = select(VatRate.id).where(
            VatRate.organization_id == organization_id,
            VatRate.id == rate_id,
            VatRate.archived_at.is_(None),
        )
        return (await self.session.execute(stmt)).first() is not None

    # --- Members ----------------------------------------------------------------------

    async def active_member_ids(
        self, organization_id: uuid.UUID, user_ids: set[uuid.UUID]
    ) -> set[uuid.UUID]:
        if not user_ids:
            return set()
        stmt = select(OrganizationMember.user_id).where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id.in_(user_ids),
            OrganizationMember.status == "active",
        )
        return set((await self.session.execute(stmt)).scalars())

    async def active_members_by_email(
        self, organization_id: uuid.UUID, emails: set[str]
    ) -> dict[str, uuid.UUID]:
        if not emails:
            return {}
        stmt = (
            select(func.lower(User.email), OrganizationMember.user_id)
            .join(User, User.id == OrganizationMember.user_id)
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.status == "active",
                func.lower(User.email).in_([e.lower() for e in emails]),
            )
        )
        return dict((await self.session.execute(stmt)).all())


class ContactRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(self, organization_id: uuid.UUID, client_id: uuid.UUID) -> list[ClientContact]:
        stmt = (
            select(ClientContact)
            .where(
                ClientContact.organization_id == organization_id,
                ClientContact.client_id == client_id,
            )
            .order_by(
                ClientContact.is_primary.desc(), func.lower(ClientContact.name), ClientContact.id
            )
        )
        return list((await self.session.execute(stmt)).scalars())

    async def get(
        self, organization_id: uuid.UUID, client_id: uuid.UUID, contact_id: uuid.UUID
    ) -> ClientContact | None:
        stmt = (
            select(ClientContact)
            .where(
                ClientContact.organization_id == organization_id,
                ClientContact.client_id == client_id,
                ClientContact.id == contact_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def has_primary(self, organization_id: uuid.UUID, client_id: uuid.UUID) -> bool:
        stmt = select(ClientContact.id).where(
            ClientContact.organization_id == organization_id,
            ClientContact.client_id == client_id,
            ClientContact.is_primary.is_(True),
        )
        return (await self.session.execute(stmt)).first() is not None

    async def clear_primary(
        self, organization_id: uuid.UUID, client_id: uuid.UUID, keep_id: uuid.UUID | None
    ) -> None:
        stmt = update(ClientContact).where(
            ClientContact.organization_id == organization_id,
            ClientContact.client_id == client_id,
            ClientContact.is_primary.is_(True),
        )
        if keep_id is not None:
            stmt = stmt.where(ClientContact.id != keep_id)
        await self.session.execute(stmt.values(is_primary=False))

    async def add(self, contact: ClientContact) -> ClientContact:
        self.session.add(contact)
        await self.session.flush()
        await self.session.refresh(contact)
        return contact

    async def add_all(self, contacts: Sequence[ClientContact]) -> None:
        self.session.add_all(contacts)
        await self.session.flush()

    async def save(self, contact: ClientContact) -> ClientContact:
        await self.session.flush()
        await self.session.refresh(contact)
        return contact

    async def delete(self, organization_id: uuid.UUID, contact_id: uuid.UUID) -> None:
        await self.session.execute(
            delete(ClientContact).where(
                ClientContact.organization_id == organization_id, ClientContact.id == contact_id
            )
        )


class EngagementRepository:
    """``client_services``: the services a client takes."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(
        self, organization_id: uuid.UUID, client_id: uuid.UUID
    ) -> list[tuple[ClientService, Service]]:
        stmt = (
            select(ClientService, Service)
            .join(
                Service,
                and_(
                    Service.organization_id == ClientService.organization_id,
                    Service.id == ClientService.service_id,
                ),
            )
            .where(
                ClientService.organization_id == organization_id,
                ClientService.client_id == client_id,
            )
            .order_by(ClientService.is_active.desc(), Service.sort_order, Service.name)
        )
        return [(row[0], row[1]) for row in (await self.session.execute(stmt)).all()]

    async def get(
        self, organization_id: uuid.UUID, client_id: uuid.UUID, engagement_id: uuid.UUID
    ) -> tuple[ClientService, Service] | None:
        stmt = (
            select(ClientService, Service)
            .join(
                Service,
                and_(
                    Service.organization_id == ClientService.organization_id,
                    Service.id == ClientService.service_id,
                ),
            )
            .where(
                ClientService.organization_id == organization_id,
                ClientService.client_id == client_id,
                ClientService.id == engagement_id,
            )
            .with_for_update(of=ClientService)
            .execution_options(populate_existing=True)
        )
        row = (await self.session.execute(stmt)).one_or_none()
        return (row[0], row[1]) if row else None

    async def exists_for(
        self, organization_id: uuid.UUID, client_id: uuid.UUID, service_id: uuid.UUID
    ) -> bool:
        stmt = select(ClientService.id).where(
            ClientService.organization_id == organization_id,
            ClientService.client_id == client_id,
            ClientService.service_id == service_id,
        )
        return (await self.session.execute(stmt)).first() is not None

    async def add(self, engagement: ClientService) -> ClientService:
        self.session.add(engagement)
        await self.session.flush()
        await self.session.refresh(engagement)
        return engagement

    async def add_all(self, engagements: Sequence[ClientService]) -> None:
        self.session.add_all(engagements)
        await self.session.flush()

    async def save(self, engagement: ClientService) -> ClientService:
        await self.session.flush()
        await self.session.refresh(engagement)
        return engagement

    async def delete(self, organization_id: uuid.UUID, engagement_id: uuid.UUID) -> None:
        await self.session.execute(
            delete(ClientService).where(
                ClientService.organization_id == organization_id,
                ClientService.id == engagement_id,
            )
        )
