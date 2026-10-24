"""Clients, their contacts and engagements, the client list and the client overview.

Rules (docs/domain.md, "Clients"):

* ``code`` is unique among live clients, upper-cased, generated from the ``client``
  sequence when not given (taken codes are skipped).
* Any status transition is allowed; moving a client into or out of ``archived`` needs
  ``client.delete`` (the permission that archives). Other modules call
  ``ensure_client_accepts_work``: ``inactive``/``archived`` answer 409 ``CLIENT_INACTIVE``.
* Soft delete only when nothing references the client; otherwise 409 ``CLIENT_IN_USE``.
* One primary contact per client: making a contact primary unsets the old one in the same
  transaction, under the client row lock.
"""

import csv
import io
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.db.models import Client, ClientContact, ClientService, DocumentSequence, Service
from app.db.session import unit_of_work
from app.repositories.clients import (
    ClientRepository,
    ClientRow,
    ContactRepository,
    DeadlineCounts,
    EngagementRepository,
)
from app.repositories.engagement_jobs import EngagementJobRepository
from app.repositories.invoice_templates import InvoiceTemplateRepository
from app.repositories.rates import ClientRateRepository
from app.repositories.settings import SequenceRepository, ServiceRepository
from app.schemas.billing import ClientRef
from app.schemas.bulk import BulkChangeResult, unique_ids
from app.schemas.clients import (
    CENTRALLY_MANAGED_FIELDS,
    CLIENT_CODE_PATTERN,
    ClientBulkAction,
    ClientCreate,
    ClientDetail,
    ClientFilters,
    ClientListItem,
    ClientRead,
    ClientServiceCreate,
    ClientServiceRead,
    ClientServiceUpdate,
    ClientSummary,
    ClientUpdate,
    ContactCreate,
    ContactRead,
    ContactUpdate,
    Deadlines,
    DeadlineStatus,
    Financials,
    PrimaryContact,
    RecurringFees,
    SegmentCount,
    ServiceTag,
    TemplateRef,
    check_year_end,
)
from app.schemas.rates import ClientRateRead
from app.services import audit, policy
from app.services.common import record_audit_many
from app.services.policy import OrgContext
from app.services.rates import practice_today
from app.services.sequences import DEFAULT_SEQUENCES, format_number
from app.utils.money import vat_on

DUE_SOON_DAYS = 14
INACTIVE_STATUSES = frozenset({"inactive", "archived"})
ANNUAL_MULTIPLIER = {"monthly": 12, "quarterly": 4, "annually": 1}
EXPORT_BATCH = 1000
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


async def ensure_client_accepts_work(
    session: AsyncSession, organization_id: uuid.UUID, client_id: uuid.UUID
) -> Client:
    """The live client, if new jobs, time and disbursements may be recorded for it.

    Raises ``NotFoundError`` for a missing, deleted or other practice's client and
    ``ConflictError`` ``CLIENT_INACTIVE`` for an inactive or archived one. Takes a
    ``FOR KEY SHARE`` lock, so a concurrent archive or delete waits for the caller's
    transaction (call it inside your unit of work).
    """
    client = await ClientRepository(session).get(organization_id, client_id, lock="key_share")
    if client is None:
        raise NotFoundError("Client not found.")
    if client.status in INACTIVE_STATUSES:
        raise ConflictError(
            f"This client is {client.status}: new work cannot be recorded for it.",
            code="CLIENT_INACTIVE",
            details={"client_id": str(client.id), "status": client.status},
        )
    return client


async def generate_client_codes(
    session: AsyncSession, organization_id: uuid.UUID, count: int, avoid: set[str]
) -> list[str]:
    """Take ``count`` codes from the ``client`` sequence under its row lock, skipping codes
    used by live clients or listed in ``avoid``. Run inside the transaction that uses them."""
    if count <= 0:
        return []
    sequences = SequenceRepository(session)
    clients = ClientRepository(session)
    sequence = await sequences.lock(organization_id, "client")
    if sequence is None:
        prefix, padding = DEFAULT_SEQUENCES["client"]
        await sequences.add(
            DocumentSequence(
                organization_id=organization_id,
                kind="client",
                prefix=prefix,
                padding=padding,
                next_number=1,
            )
        )
        sequence = await sequences.lock(organization_id, "client")
        assert sequence is not None
    codes: list[str] = []
    next_free = sequence.next_number
    while len(codes) < count:
        start = next_free
        candidates = [
            (n, format_number(sequence.prefix, n, sequence.padding).upper())
            for n in range(start, start + count - len(codes) + 10)
        ]
        taken = await clients.codes_taken(organization_id, {c for _, c in candidates}) | avoid
        next_free = candidates[-1][0] + 1
        for number, code in candidates:
            if code in taken:
                continue
            # Only a code that would actually be handed out must be valid: the look-ahead
            # past the last needed number may run beyond 20 characters harmlessly.
            if not CLIENT_CODE_PATTERN.fullmatch(code):
                raise ConflictError(
                    "The client numbering settings do not produce valid client codes.",
                    code="SEQUENCE_INVALID",
                    details={"example": code},
                )
            codes.append(code)
            if len(codes) == count:
                next_free = number + 1
                break
    sequence.next_number = next_free
    await sequences.save(sequence)
    return codes


def _deadlines(counts: DeadlineCounts | None, today: date) -> Deadlines:
    if counts is None:
        return Deadlines(due_soon_days=DUE_SOON_DAYS)
    status: DeadlineStatus
    if counts.overdue:
        status = "overdue"
    elif counts.due_today:
        status = "due_today"
    elif counts.due_soon:
        status = "due_soon"
    else:
        status = "on_track" if counts.open_jobs else "none"
    return Deadlines(
        open_jobs=counts.open_jobs,
        overdue=counts.overdue,
        overdue_days=(today - counts.oldest_overdue).days if counts.oldest_overdue else 0,
        due_today=counts.due_today,
        due_soon=counts.due_soon,
        due_soon_days=DUE_SOON_DAYS,
        next_due_date=counts.next_due_date,
        status=status,
    )


def _csv_cell(value: Any) -> Any:
    """Neutralise spreadsheet formulas in text cells (CSV injection)."""
    if value is None:
        return ""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, str) and value.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, date | datetime):
        return value.isoformat()
    return value


class ClientsService:
    """(Named in the plural: ``ClientService`` is the engagement model.)"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.clients = ClientRepository(session)
        self.contacts = ContactRepository(session)
        self.engagements = EngagementRepository(session)
        self.services = ServiceRepository(session)
        self.templates = InvoiceTemplateRepository(session)
        self.client_rates = ClientRateRepository(session)

    async def _audit(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        action: str,
        target_type: str,
        target_id: uuid.UUID,
        meta: dict[str, Any],
    ) -> None:
        await audit.record(
            self.session,
            action,
            ctx=rctx,
            actor_user_id=ctx.user.id,
            organization_id=ctx.organization_id,
            target_type=target_type,
            target_id=target_id,
            meta=meta,
        )

    async def _client(
        self, ctx: OrgContext, client_id: uuid.UUID, lock: str | None = None
    ) -> Client:
        client = await self.clients.get(ctx.organization_id, client_id, lock=lock)
        if client is None:
            raise NotFoundError("Client not found.")
        return client

    # --- List, summary, export ----------------------------------------------------------

    async def _items(
        self, ctx: OrgContext, rows: Sequence[ClientRow], today: date
    ) -> list[ClientListItem]:
        """List rows with their derived columns: three batched queries for any page size."""
        org_id = ctx.organization_id
        ids = [row.client.id for row in rows]
        primaries = await self.clients.primary_contacts(org_id, ids)
        services = await self.clients.active_services(org_id, ids)
        deadlines = await self.clients.deadlines(
            org_id, ids, today=today, soon_until=today + timedelta(days=DUE_SOON_DAYS)
        )
        items = []
        for row in rows:
            client = row.client
            primary = primaries.get(client.id)
            items.append(
                ClientListItem.model_validate(
                    {
                        **ClientRead.fields_of(client),
                        "primary_contact": PrimaryContact(
                            id=primary.id,
                            name=primary.name,
                            email=primary.email,
                            phone=primary.phone,
                        )
                        if primary
                        else None,
                        "contact_name": primary.name if primary else None,
                        "contact_email": (primary.email if primary else None) or client.email,
                        "deadlines": _deadlines(deadlines.get(client.id), today),
                        "last_activity_at": row.last_activity_at,
                        "services": [
                            ServiceTag(service_id=s.id, code=s.code, name=s.name)
                            for s in services.get(client.id, [])
                        ],
                    }
                )
            )
        return items

    async def list_clients(
        self,
        ctx: OrgContext,
        filters: ClientFilters,
        *,
        sort: str,
        descending: bool,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[ClientListItem], str | None]:
        page = await self.clients.list_page(
            ctx.organization_id,
            filters,
            sort=sort,
            descending=descending,
            limit=limit,
            cursor=cursor,
        )
        return await self._items(ctx, page.rows, practice_today(ctx)), page.next_cursor

    async def summary(self, ctx: OrgContext) -> ClientSummary:
        counts = await self.clients.summary(ctx.organization_id, today=practice_today(ctx))
        by_status = {
            s: counts.by_status.get(s, 0) for s in ("prospect", "active", "inactive", "archived")
        }
        return ClientSummary(
            total=sum(v for k, v in by_status.items() if k != "archived"),
            by_status=by_status,
            by_client_type=counts.by_client_type,
            by_aml_status={
                s: counts.by_aml_status.get(s, 0)
                for s in ("not_started", "pending", "clear", "review", "failed")
            },
            by_accounting_software=counts.by_accounting_software,
            by_segment=[SegmentCount(segment=s, count=c) for s, c in counts.by_segment],
            with_overdue_deadlines=counts.with_overdue_deadlines,
            by_central={
                "linked": counts.by_central.get("linked", 0),
                "local": counts.by_central.get("local", 0),
            },
            users=counts.users,
        )

    async def export_csv(
        self, ctx: OrgContext, filters: ClientFilters, *, sort: str, descending: bool
    ) -> bytes:
        """Every matching client as CSV (UTF-8 with BOM for Excel), same columns as the list."""
        today = practice_today(ctx)
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\r\n")
        writer.writerow(
            [
                "code",
                "name",
                "client_type",
                "status",
                "company_number",
                "segment",
                "health_score",
                "contact_name",
                "contact_email",
                "email",
                "phone",
                "deadline_status",
                "overdue_jobs",
                "overdue_days",
                "next_due_date",
                "aml_status",
                "accounting_software",
                "services",
                "last_activity_at",
                "created_at",
            ]
        )
        pages = await self.clients.iter_all(
            ctx.organization_id, filters, sort=sort, descending=descending, batch=EXPORT_BATCH
        )
        for page in pages:
            for item in await self._items(ctx, page.rows, today):
                writer.writerow(
                    _csv_cell(v)
                    for v in (
                        item.code,
                        item.name,
                        item.client_type,
                        item.status,
                        item.company_number,
                        item.segment,
                        item.health_score,
                        item.contact_name,
                        item.contact_email,
                        item.email,
                        item.phone,
                        item.deadlines.status,
                        item.deadlines.overdue,
                        item.deadlines.overdue_days,
                        item.deadlines.next_due_date,
                        item.aml_status,
                        item.accounting_software,
                        "; ".join(s.name for s in item.services),
                        item.last_activity_at,
                        item.created_at,
                    )
                )
        return out.getvalue().encode("utf-8-sig")

    # --- Detail -------------------------------------------------------------------------

    async def _financials(self, ctx: OrgContext, client: Client, today: date) -> Financials:
        org_id = ctx.organization_id
        wip_time = wip_minutes = wip_disb = None
        outstanding = overdue = on_account = None
        if policy.has_permission(ctx, "wip.read"):
            wip_minutes, wip_time = await self.clients.wip_time(
                org_id, client.id, approved_only=ctx.organization.require_timesheet_approval
            )
            wip_disb = await self.clients.wip_disbursements(org_id, client.id)
        if policy.has_permission(ctx, "invoice.read") or policy.has_permission(ctx, "payment.read"):
            outstanding, overdue, on_account = await self.clients.balances(
                org_id, client.id, today=today
            )
            if not policy.has_permission(ctx, "invoice.read"):
                outstanding = overdue = None
            if not policy.has_permission(ctx, "payment.read"):
                on_account = None
        return Financials(
            wip_time_value=wip_time,
            wip_disbursement_value=wip_disb,
            wip_total=None if wip_time is None or wip_disb is None else wip_time + wip_disb,
            unbilled_minutes=wip_minutes,
            outstanding_balance=outstanding,
            overdue_balance=overdue,
            on_account=on_account,
        )

    async def _recurring_fees(
        self,
        ctx: OrgContext,
        client: Client,
        engagements: Sequence[tuple[ClientService, Service]],
        today: date,
    ) -> RecurringFees:
        live = [
            (e, s)
            for e, s in engagements
            if e.is_active and (e.end_date is None or e.end_date >= today)
        ]
        org_id = ctx.organization_id
        rate_ids = {s.vat_rate_id for _, s in live if s.vat_rate_id}
        if client.default_vat_rate_id:
            rate_ids.add(client.default_vat_rate_id)
        rates = await self.clients.vat_rates_bp(org_id, rate_ids)
        default_bp = await self.clients.default_vat_rate_bp(org_id) or 0
        net = vat = one_off = 0
        for engagement, service in live:
            fee = engagement.fee if engagement.fee is not None else (service.default_fee or 0)
            if engagement.frequency == "one_off":
                one_off += fee
                continue
            line_net = fee * ANNUAL_MULTIPLIER[engagement.frequency]
            if ctx.organization.vat_registered:
                rate_id = service.vat_rate_id or client.default_vat_rate_id
                rate_bp = rates.get(rate_id, default_bp) if rate_id else default_bp
            else:
                rate_bp = 0
            net += line_net
            vat += vat_on(line_net, rate_bp)
        return RecurringFees(
            annual_net=net, annual_vat=vat, annual_gross=net + vat, one_off_net=one_off
        )

    async def _detail(self, ctx: OrgContext, client: Client) -> ClientDetail:
        org_id = ctx.organization_id
        today = practice_today(ctx)
        activity = await self.clients.last_activity(org_id, client.id)
        item = (
            await self._items(ctx, [ClientRow(client=client, last_activity_at=activity)], today)
        )[0]
        contacts = await self.contacts.list(org_id, client.id)
        engagements = await self.engagements.list(org_id, client.id)
        rates = None
        if policy.has_permission(ctx, "rate.read"):
            rates = [
                ClientRateRead.from_model(rate, name)
                for rate, name in await self.client_rates.list_for_client(org_id, client.id)
            ]
        default_service = None
        if client.default_service_id is not None:
            service = await self.services.get(org_id, client.default_service_id)
            if service is not None:
                default_service = ServiceTag(
                    service_id=service.id, code=service.code, name=service.name
                )
        invoice_template = None
        if client.invoice_template_id is not None:
            template = await self.templates.get(org_id, client.invoice_template_id)
            if template is not None:
                invoice_template = TemplateRef(id=template.id, name=template.name)
        merged_into = None
        if client.merged_into_client_id is not None:
            survivor = await self.clients.get(org_id, client.merged_into_client_id)
            if survivor is not None:
                merged_into = ClientRef.from_model(survivor)
        return ClientDetail.model_validate(
            {
                **item.model_dump(),
                "contacts": [ContactRead.from_model(c) for c in contacts],
                "engagements": await self.engagement_reads(ctx, engagements),
                "rates": rates,
                "financials": await self._financials(ctx, client, today),
                "recurring_fees": await self._recurring_fees(ctx, client, engagements, today),
                "default_service": default_service,
                "invoice_template": invoice_template,
                "merged_into": merged_into,
            }
        )

    async def get_detail(self, ctx: OrgContext, client_id: uuid.UUID) -> ClientDetail:
        return await self._detail(ctx, await self._client(ctx, client_id))

    # --- Create, update, archive, delete ------------------------------------------------

    async def _check_people(
        self, ctx: OrgContext, values: dict[str, Any], current: Client | None = None
    ) -> None:
        wanted = {
            field: values[field]
            for field in ("partner_user_id", "manager_user_id")
            if values.get(field) is not None
            and (current is None or values[field] != getattr(current, field))
        }
        if not wanted:
            return
        active = await self.clients.active_member_ids(ctx.organization_id, set(wanted.values()))
        for field, user_id in wanted.items():
            if user_id not in active:
                raise ValidationError.for_field(field, "Choose an active member of this practice.")

    async def _check_vat_rate(
        self, ctx: OrgContext, rate_id: uuid.UUID | None, current: uuid.UUID | None = None
    ) -> None:
        if rate_id is None or rate_id == current:
            return
        if not await self.clients.live_vat_rate(ctx.organization_id, rate_id):
            raise ValidationError.for_field(
                "default_vat_rate_id", "Choose a live VAT rate of the practice."
            )

    async def _check_billing_settings(
        self, ctx: OrgContext, values: dict[str, Any], current: Client | None = None
    ) -> None:
        """The default service and the invoice template must be the practice's own, and live.
        An id that is not the practice's answers 404; one that is inactive answers 422."""
        org_id = ctx.organization_id
        service_id = values.get("default_service_id")
        if service_id is not None and (current is None or service_id != current.default_service_id):
            service = await self.services.get(org_id, service_id)
            if service is None:
                raise NotFoundError("Service not found.", details={"field": "default_service_id"})
            if not service.is_active:
                raise ValidationError.for_field("default_service_id", "This service is inactive.")
        template_id = values.get("invoice_template_id")
        if template_id is not None and (
            current is None or template_id != current.invoice_template_id
        ):
            template = await self.templates.get(org_id, template_id)
            if template is None:
                raise NotFoundError(
                    "Invoice template not found.", details={"field": "invoice_template_id"}
                )
            if template.status != "active":
                raise ValidationError.for_field(
                    "invoice_template_id", "This invoice template is inactive."
                )

    def _check_archive_permission(self, ctx: OrgContext, old: str | None, new: str) -> None:
        """Archiving (and un-archiving) is ``client.delete``'s job, whatever the route."""
        if (old == "archived") != (new == "archived"):
            policy.ensure(ctx, "client.delete")

    @staticmethod
    def _check_centrally_managed(client: Client, values: dict[str, Any]) -> None:
        """A linked client's name and address belong to the central service."""
        if client.central_client_id is None:
            return
        managed = [
            field
            for field in CENTRALLY_MANAGED_FIELDS
            if field in values and values[field] != getattr(client, field)
        ]
        if managed:
            message = "This client is linked to the central client service, which manages it."
            raise ConflictError(
                f"{message} Unlink it to edit these fields.",
                code="CLIENT_FIELD_MANAGED_CENTRALLY",
                details=[
                    {"field": field, "code": "managed_centrally", "message": message}
                    for field in managed
                ],
            )

    async def _ensure_code_free(
        self, ctx: OrgContext, code: str, exclude_id: uuid.UUID | None = None
    ) -> None:
        if await self.clients.code_taken(ctx.organization_id, code, exclude_id=exclude_id):
            message = "Another client already uses this code."
            raise ConflictError(
                message,
                code="CLIENT_CODE_TAKEN",
                details=[{"field": "code", "code": "duplicate", "message": message}],
            )

    async def create(
        self, ctx: OrgContext, data: ClientCreate, rctx: RequestContext
    ) -> ClientDetail:
        org_id = ctx.organization_id
        values = data.model_dump(exclude={"contacts"})
        self._check_archive_permission(ctx, None, data.status)
        async with unit_of_work(self.session):
            await self._check_people(ctx, values)
            await self._check_vat_rate(ctx, data.default_vat_rate_id)
            await self._check_billing_settings(ctx, values)
            if data.code:
                await self._ensure_code_free(ctx, data.code)
            else:
                values["code"] = (await generate_client_codes(self.session, org_id, 1, set()))[0]
            client = await self.clients.add(Client(organization_id=org_id, **values))
            if data.contacts:
                has_primary = any(c.is_primary for c in data.contacts)
                await self.contacts.add_all(
                    [
                        ClientContact(
                            organization_id=org_id,
                            client_id=client.id,
                            **contact.model_dump(exclude={"is_primary"}),
                            is_primary=bool(contact.is_primary)
                            or (not has_primary and index == 0 and contact.is_primary is None),
                        )
                        for index, contact in enumerate(data.contacts)
                    ]
                )
            await self._audit(
                ctx,
                rctx,
                "client.created",
                "client",
                client.id,
                {"code": client.code, "name": client.name, "status": client.status},
            )
            detail = await self._detail(ctx, client)
        return detail

    async def update(
        self, ctx: OrgContext, client_id: uuid.UUID, data: ClientUpdate, rctx: RequestContext
    ) -> ClientDetail:
        values = data.model_dump(exclude_unset=True)
        async with unit_of_work(self.session):
            lock = "update" if "status" in values else "no_key_update"
            client = await self._client(ctx, client_id, lock=lock)
            if "status" in values:
                self._check_archive_permission(ctx, client.status, values["status"])
            if "code" in values and values["code"] != client.code:
                await self._ensure_code_free(ctx, values["code"], exclude_id=client.id)
            self._check_centrally_managed(client, values)
            await self._check_people(ctx, values, client)
            await self._check_billing_settings(ctx, values, client)
            if "default_vat_rate_id" in values:
                await self._check_vat_rate(
                    ctx, values["default_vat_rate_id"], client.default_vat_rate_id
                )
            try:
                check_year_end(
                    values.get("year_end_month", client.year_end_month),
                    values.get("year_end_day", client.year_end_day),
                )
            except ValueError as exc:
                field, _, message = str(exc).partition(": ")
                raise ValidationError.for_field(field, message) from exc
            changes: dict[str, list[Any]] = {}
            for field, value in values.items():
                old = getattr(client, field)
                if old != value:
                    changes[field] = [_jsonable(old), _jsonable(value)]
                    setattr(client, field, value)
            client = await self.clients.save(client)
            if changes:
                action = (
                    "client.archived"
                    if changes.get("status", [None, None])[1] == "archived"
                    else "client.updated"
                )
                await self._audit(
                    ctx,
                    rctx,
                    action,
                    "client",
                    client.id,
                    {"code": client.code, "name": client.name, "changes": changes},
                )
            detail = await self._detail(ctx, client)
        return detail

    async def archive(
        self, ctx: OrgContext, client_id: uuid.UUID, rctx: RequestContext
    ) -> ClientDetail:
        return await self.update(ctx, client_id, ClientUpdate(status="archived"), rctx)

    async def delete(self, ctx: OrgContext, client_id: uuid.UUID, rctx: RequestContext) -> None:
        async with unit_of_work(self.session):
            client = await self._client(ctx, client_id, lock="update")
            usage = await self.clients.usage(ctx.organization_id, client.id)
            if usage.in_use:
                raise ConflictError(
                    "This client has jobs, time, disbursements, expenses, invoices or payments. "
                    "Archive it instead.",
                    code="CLIENT_IN_USE",
                    details={
                        "jobs": usage.jobs,
                        "time_entries": usage.time_entries,
                        "disbursements": usage.disbursements,
                        "expenses": usage.expenses,
                        "invoices": usage.invoices,
                        "credit_notes": usage.credit_notes,
                        "payments": usage.payments,
                    },
                )
            client.deleted_at = datetime.now(UTC)
            await self.clients.save(client)
            await self._audit(
                ctx,
                rctx,
                "client.deleted",
                "client",
                client.id,
                {"code": client.code, "name": client.name},
            )

    # --- Bulk action --------------------------------------------------------------------

    async def bulk(
        self, ctx: OrgContext, data: ClientBulkAction, rctx: RequestContext
    ) -> BulkChangeResult:
        """One action on up to 200 clients, all or nothing: a client that breaks the rule of
        the single operation (409) stops the whole action."""
        policy.ensure(ctx, "client.delete" if data.action == "delete" else "client.update")
        org_id = ctx.organization_id
        ids = unique_ids(data.client_ids)
        async with unit_of_work(self.session):
            clients = await self.clients.lock_many(org_id, ids)
            if len(clients) != len(ids):
                found = {c.id for c in clients}
                raise NotFoundError(
                    "Some of the clients were not found.",
                    details={"client_ids": [str(i) for i in ids if i not in found]},
                )
            rows: list[audit.AuditRow]
            match data.action:
                case "delete":
                    rows = await self._bulk_delete(ctx, clients)
                case "set_status":
                    assert data.value is not None
                    for client in clients:
                        if client.status != data.value:
                            self._check_archive_permission(ctx, client.status, data.value)
                    rows = self._bulk_set(clients, "status", data.value)
                case "set_manager" | "set_partner":
                    field = "manager_user_id" if data.action == "set_manager" else "partner_user_id"
                    person = data.user_id
                    if person is not None:
                        await self._check_people(ctx, {field: person})
                    rows = self._bulk_set(clients, field, person)
                case "set_segment":
                    rows = self._bulk_set(clients, "segment", data.segment)
            await self.session.flush()
            await record_audit_many(self.session, ctx, rctx, rows)
        return BulkChangeResult(action=data.action, affected=len(ids), changed=len(rows))

    @staticmethod
    def _bulk_set(clients: Sequence[Client], field: str, value: Any) -> list[audit.AuditRow]:
        rows: list[audit.AuditRow] = []
        for client in clients:
            old = getattr(client, field)
            if old == value:
                continue
            setattr(client, field, value)
            action = "client.archived" if field == "status" and value == "archived" else None
            rows.append(
                (
                    action or "client.updated",
                    "client",
                    client.id,
                    {
                        "code": client.code,
                        "name": client.name,
                        "changes": {field: [_jsonable(old), _jsonable(value)]},
                        "bulk": True,
                    },
                )
            )
        return rows

    async def _bulk_delete(
        self, ctx: OrgContext, clients: Sequence[Client]
    ) -> list[audit.AuditRow]:
        refused = []
        for client in clients:
            usage = await self.clients.usage(ctx.organization_id, client.id)
            if usage.in_use:
                refused.append(
                    {
                        "client_id": str(client.id),
                        "code": client.code,
                        "name": client.name,
                        "reason": "Has jobs, time, disbursements, expenses, invoices, credit "
                        "notes or payments. Archive it instead.",
                        "usage": {
                            "jobs": usage.jobs,
                            "time_entries": usage.time_entries,
                            "disbursements": usage.disbursements,
                            "expenses": usage.expenses,
                            "invoices": usage.invoices,
                            "credit_notes": usage.credit_notes,
                            "payments": usage.payments,
                        },
                    }
                )
        if refused:
            raise ConflictError(
                f"{len(refused)} client{'s are' if len(refused) != 1 else ' is'} in use and "
                "cannot be deleted. Nothing was deleted.",
                code="CLIENT_IN_USE",
                details=refused,
            )
        now = datetime.now(UTC)
        rows: list[audit.AuditRow] = []
        for client in clients:
            client.deleted_at = now
            rows.append(
                (
                    "client.deleted",
                    "client",
                    client.id,
                    {"code": client.code, "name": client.name, "bulk": True},
                )
            )
        return rows

    # --- Contacts -----------------------------------------------------------------------

    async def list_contacts(self, ctx: OrgContext, client_id: uuid.UUID) -> list[ClientContact]:
        await self._client(ctx, client_id)
        return await self.contacts.list(ctx.organization_id, client_id)

    async def create_contact(
        self, ctx: OrgContext, client_id: uuid.UUID, data: ContactCreate, rctx: RequestContext
    ) -> ClientContact:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            # The client row lock serialises primary-contact changes of this client.
            client = await self._client(ctx, client_id, lock="no_key_update")
            primary = data.is_primary
            if primary is None:
                primary = not await self.contacts.has_primary(org_id, client.id)
            if primary:
                await self.contacts.clear_primary(org_id, client.id, keep_id=None)
            contact = await self.contacts.add(
                ClientContact(
                    organization_id=org_id,
                    client_id=client.id,
                    **data.model_dump(exclude={"is_primary"}),
                    is_primary=primary,
                )
            )
            await self._audit(
                ctx,
                rctx,
                "client_contact.created",
                "client_contact",
                contact.id,
                {"client_id": str(client.id), "client_name": client.name, "name": contact.name},
            )
        return contact

    async def update_contact(
        self,
        ctx: OrgContext,
        client_id: uuid.UUID,
        contact_id: uuid.UUID,
        data: ContactUpdate,
        rctx: RequestContext,
    ) -> ClientContact:
        org_id = ctx.organization_id
        values = data.model_dump(exclude_unset=True)
        async with unit_of_work(self.session):
            client = await self._client(ctx, client_id, lock="no_key_update")
            contact = await self.contacts.get(org_id, client.id, contact_id)
            if contact is None:
                raise NotFoundError("Contact not found.")
            if values.get("is_primary") and not contact.is_primary:
                await self.contacts.clear_primary(org_id, client.id, keep_id=contact.id)
            changes: dict[str, list[Any]] = {}
            for field, value in values.items():
                old = getattr(contact, field)
                if old != value:
                    changes[field] = [old, value]
                    setattr(contact, field, value)
            contact = await self.contacts.save(contact)
            if changes:
                await self._audit(
                    ctx,
                    rctx,
                    "client_contact.updated",
                    "client_contact",
                    contact.id,
                    {"client_id": str(client.id), "client_name": client.name, "changes": changes},
                )
        return contact

    async def delete_contact(
        self, ctx: OrgContext, client_id: uuid.UUID, contact_id: uuid.UUID, rctx: RequestContext
    ) -> None:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            client = await self._client(ctx, client_id, lock="no_key_update")
            contact = await self.contacts.get(org_id, client.id, contact_id)
            if contact is None:
                raise NotFoundError("Contact not found.")
            await self.contacts.delete(org_id, contact.id)
            await self._audit(
                ctx,
                rctx,
                "client_contact.deleted",
                "client_contact",
                contact.id,
                {"client_id": str(client.id), "client_name": client.name, "name": contact.name},
            )

    # --- Engagements (client services) --------------------------------------------------

    async def engagement_reads(
        self, ctx: OrgContext, rows: Sequence[tuple[ClientService, Service]]
    ) -> list[ClientServiceRead]:
        """The engagements as returned by the API, each with its next job period."""
        # Imported here: the job service imports this module.
        from app.services.engagement_jobs import next_job_period

        done = await EngagementJobRepository(self.session).created_periods(
            ctx.organization_id, [e.id for e, _ in rows]
        )
        today = practice_today(ctx)
        return [
            ClientServiceRead.from_model(e, s, next_job_period(e, today, done.get(e.id, set())))
            for e, s in rows
        ]

    async def list_engagements(
        self, ctx: OrgContext, client_id: uuid.UUID
    ) -> list[tuple[ClientService, Service]]:
        await self._client(ctx, client_id)
        return await self.engagements.list(ctx.organization_id, client_id)

    async def create_engagement(
        self,
        ctx: OrgContext,
        client_id: uuid.UUID,
        data: ClientServiceCreate,
        rctx: RequestContext,
    ) -> tuple[ClientService, Service]:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            client = await self._client(ctx, client_id, lock="no_key_update")
            service = await self.services.get(org_id, data.service_id)
            if service is None or not service.is_active:
                raise ValidationError.for_field(
                    "service_id", "Choose an active service of the practice."
                )
            if await self.engagements.exists_for(org_id, client.id, service.id):
                message = "This client already takes this service."
                raise ConflictError(
                    message,
                    code="CLIENT_SERVICE_EXISTS",
                    details=[{"field": "service_id", "code": "duplicate", "message": message}],
                )
            engagement = await self.engagements.add(
                ClientService(organization_id=org_id, client_id=client.id, **data.model_dump())
            )
            await self._audit(
                ctx,
                rctx,
                "client_service.created",
                "client_service",
                engagement.id,
                {
                    "client_id": str(client.id),
                    "client_name": client.name,
                    "service_code": service.code,
                    "fee": engagement.fee,
                    "frequency": engagement.frequency,
                    "auto_create_jobs": engagement.auto_create_jobs,
                },
            )
        return engagement, service

    async def update_engagement(
        self,
        ctx: OrgContext,
        client_id: uuid.UUID,
        engagement_id: uuid.UUID,
        data: ClientServiceUpdate,
        rctx: RequestContext,
    ) -> tuple[ClientService, Service]:
        org_id = ctx.organization_id
        values = data.model_dump(exclude_unset=True)
        async with unit_of_work(self.session):
            client = await self._client(ctx, client_id)
            found = await self.engagements.get(org_id, client.id, engagement_id)
            if found is None:
                raise NotFoundError("Client service not found.")
            engagement, service = found
            start = values.get("start_date", engagement.start_date)
            end = values.get("end_date", engagement.end_date)
            if start and end and end < start:
                raise ValidationError.for_field("end_date", "The end date is before the start date")
            if values.get("auto_create_jobs", engagement.auto_create_jobs) and start is None:
                raise ValidationError.for_field(
                    "start_date", "Automatic jobs need a start date.", "required"
                )
            changes: dict[str, list[Any]] = {}
            for field, value in values.items():
                old = getattr(engagement, field)
                if old != value:
                    changes[field] = [_jsonable(old), _jsonable(value)]
                    setattr(engagement, field, value)
            engagement = await self.engagements.save(engagement)
            if changes:
                await self._audit(
                    ctx,
                    rctx,
                    "client_service.updated",
                    "client_service",
                    engagement.id,
                    {
                        "client_id": str(client.id),
                        "client_name": client.name,
                        "service_code": service.code,
                        "changes": changes,
                    },
                )
        return engagement, service

    async def delete_engagement(
        self,
        ctx: OrgContext,
        client_id: uuid.UUID,
        engagement_id: uuid.UUID,
        rctx: RequestContext,
    ) -> None:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            client = await self._client(ctx, client_id)
            found = await self.engagements.get(org_id, client.id, engagement_id)
            if found is None:
                raise NotFoundError("Client service not found.")
            engagement, service = found
            await self.engagements.delete(org_id, engagement.id)
            await self._audit(
                ctx,
                rctx,
                "client_service.deleted",
                "client_service",
                engagement.id,
                {
                    "client_id": str(client.id),
                    "client_name": client.name,
                    "service_code": service.code,
                    "fee": engagement.fee,
                },
            )
