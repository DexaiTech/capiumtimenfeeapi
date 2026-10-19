"""The Client 360 overview: the figures behind the tabs of one client, in one call.

Every section is computed by the same rules as the screen it summarises, and a section the caller
may not see is ``null`` (never a 403 for the whole overview):

* ``jobs``: ``job.read``. The stored statuses ``planned`` and ``cancelled`` are reported as
  ``not_started`` and ``closed``.
* ``time``: ``time.read_all`` (every person's time) or ``time.log`` (the caller's own).
* ``expenses``: ``wip.read`` (every disbursement) or ``disbursement.log`` / ``.manage`` (the
  caller's own), like ``GET /disbursements``.
* ``wip``: ``wip.read``; the figures come from the WIP module's own queries, so they equal the WIP
  screens (time WIP + fixed-fee WIP + expense WIP).
* ``invoices``: ``invoice.read``; ``payments``: ``payment.read``.
* ``recent_activity``: audit entries about the client (and clients merged into it) whose kind the
  caller may see (``invoice.*`` needs ``invoice.read`` and so on).

Cost values and margins are never part of the overview.
"""

import uuid
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.db.models import AuditLog, User
from app.repositories.audit import AuditRepository
from app.repositories.client_overview import ClientOverviewRepository
from app.repositories.clients import ClientRepository
from app.repositories.wip import WipRepository, WipSummaryFilters
from app.schemas.client_overview import (
    ActivityEvent,
    ClientOverview,
    ExpensesOverview,
    InvoicesOverview,
    JobsOverview,
    PaymentsOverview,
    TabCounts,
    TimeOverview,
    WipAgeing,
    WipOverview,
)
from app.services.billing_common import today_for
from app.services.policy import OrgContext, has_permission
from app.utils.pagination import PageParams

RECENT_ACTIVITY = 10
JOB_STATUS_KEYS = ("not_started", "in_progress", "on_hold", "completed", "closed")
# The Jobs module renames planned -> not_started and cancelled -> closed; both spellings count.
_JOB_STATUS = {
    "planned": "not_started",
    "not_started": "not_started",
    "in_progress": "in_progress",
    "on_hold": "on_hold",
    "completed": "completed",
    "cancelled": "closed",
    "closed": "closed",
}
# Audit actions by prefix and the permission that lets a person see them (None: anyone who can
# read the client).
ACTIVITY_PERMISSIONS: tuple[tuple[str, str | None], ...] = (
    ("client.", None),
    ("client_contact.", None),
    ("client_service.", None),
    ("client_rate.", "rate.read"),
    ("rate.", "rate.read"),
    ("job.", "job.read"),
    ("time_entry.", "time.read_all"),
    ("disbursement.", "wip.read"),
    ("wip.", "wip.read"),
    ("invoice.", "invoice.read"),
    ("credit_note.", "invoice.read"),
    ("payment.", "payment.read"),
)
ACTION_LABELS = {
    "client.created": "Client created",
    "client.updated": "Client details updated",
    "client.archived": "Client archived",
    "client.deleted": "Client deleted",
    "client.central_enabled": "Enabled from the central client service",
    "client.central_linked": "Linked to the central client service",
    "client.central_unlinked": "Unlinked from the central client service",
    "client.central_synced": "Synchronised with the central client service",
    "client.central_renamed": "Renamed by the central client service",
    "client.central_archived": "Archived by the central client service",
    "client.central_restored": "Restored by the central client service",
    "client.central_merged": "Merged with another client",
    "client_contact.created": "Contact added",
    "client_contact.updated": "Contact updated",
    "client_contact.deleted": "Contact removed",
    "client_service.created": "Service added",
    "client_service.updated": "Service updated",
    "client_service.deleted": "Service removed",
    "client_rate.created": "Client rate added",
    "client_rate.updated": "Client rate changed",
    "client_rate.deleted": "Client rate removed",
    "job.created": "Job created",
    "job.updated": "Job updated",
    "job.status_changed": "Job status changed",
    "job.assignees_changed": "Job assignees changed",
    "job.deleted": "Job deleted",
    "time_entry.created": "Time recorded",
    "time_entry.updated": "Time entry changed",
    "time_entry.deleted": "Time entry deleted",
    "disbursement.created": "Disbursement recorded",
    "disbursement.updated": "Disbursement changed",
    "disbursement.deleted": "Disbursement deleted",
    "wip.written_off": "WIP written off",
    "wip.write_off_reversed": "WIP write-off reversed",
    "invoice.draft_created": "Draft invoice created",
    "invoice.wip_added": "WIP added to an invoice",
    "invoice.updated": "Invoice updated",
    "invoice.line_added": "Invoice line added",
    "invoice.line_updated": "Invoice line changed",
    "invoice.line_removed": "Invoice line removed",
    "invoice.draft_deleted": "Draft invoice deleted",
    "invoice.issued": "Invoice issued",
    "invoice.voided": "Invoice voided",
    "invoice.send_queued": "Invoice emailed",
    "credit_note.issued": "Credit note issued",
    "payment.recorded": "Payment recorded",
    "payment.allocated": "Payment allocated",
    "payment.unallocated": "Payment allocation removed",
    "payment.voided": "Payment voided",
}


def label_of(action: str) -> str:
    """The action in words: a known label, else the action itself made readable."""
    known = ACTION_LABELS.get(action)
    if known:
        return known
    text = action.replace(".", " ").replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def allowed_prefixes(ctx: OrgContext) -> list[str]:
    return [
        prefix
        for prefix, permission in ACTIVITY_PERMISSIONS
        if permission is None or has_permission(ctx, permission)
    ]


class ClientOverviewService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.clients = ClientRepository(session)
        self.overview = ClientOverviewRepository(session)
        self.wip = WipRepository(session)
        self.audit = AuditRepository(session)

    async def get(self, ctx: OrgContext, client_id: uuid.UUID) -> ClientOverview:
        org = ctx.organization
        client = await self.clients.get(org.id, client_id)
        if client is None:
            raise NotFoundError("Client not found.")
        today = today_for(org)
        ytd_from = date(today.year, 1, 1)
        last_30d_from = today - timedelta(days=29)

        jobs: JobsOverview | None = None
        if has_permission(ctx, "job.read"):
            jobs = await self._jobs(ctx, client_id)

        time: TimeOverview | None = None
        time_entries: int | None = None
        if has_permission(ctx, "time.read_all") or has_permission(ctx, "time.log"):
            time, time_entries = await self._time(ctx, client_id, today, ytd_from, last_30d_from)

        expenses: ExpensesOverview | None = None
        if (
            has_permission(ctx, "wip.read")
            or has_permission(ctx, "disbursement.log")
            or has_permission(ctx, "disbursement.manage")
        ):
            expenses = await self._expenses(ctx, client_id, client.default_vat_rate_id)

        wip: WipOverview | None = None
        if has_permission(ctx, "wip.read"):
            wip = await self._wip(ctx, client_id, today)

        invoices: InvoicesOverview | None = None
        if has_permission(ctx, "invoice.read"):
            invoices = await self._invoices(ctx, client_id, today, ytd_from)

        payments: PaymentsOverview | None = None
        payment_count: int | None = None
        if has_permission(ctx, "payment.read"):
            payments, payment_count = await self._payments(ctx, client_id, today, ytd_from)

        return ClientOverview(
            client_id=client.id,
            as_of=today,
            ytd_from=ytd_from,
            last_30d_from=last_30d_from,
            jobs=jobs,
            time=time,
            expenses=expenses,
            wip=wip,
            invoices=invoices,
            payments=payments,
            recent_activity=await self._activity(ctx, client_id),
            tab_counts=TabCounts(
                jobs=jobs.total if jobs else None,
                time_entries=time_entries,
                expenses=expenses.count if expenses else None,
                wip=wip.item_count if wip else None,
                invoices=invoices.count if invoices else None,
                payments=payment_count,
                contacts=await self.overview.contact_count(org.id, client_id),
            ),
        )

    # --- Sections -------------------------------------------------------------------------

    async def _jobs(self, ctx: OrgContext, client_id: uuid.UUID) -> JobsOverview:
        counts = await self.overview.job_counts(ctx.organization_id, client_id)
        by_status = dict.fromkeys(JOB_STATUS_KEYS, 0)
        for status, count in counts.items():
            key = _JOB_STATUS.get(status)
            if key is not None:
                by_status[key] += count
        return JobsOverview(total=sum(counts.values()), by_status=by_status)

    @staticmethod
    def _own_user(ctx: OrgContext, *, all_permission: str) -> uuid.UUID | None:
        """The person whose rows the caller may see: nobody in particular with the permission
        to see everyone's, else the caller."""
        return None if has_permission(ctx, all_permission) else ctx.user.id

    async def _time(
        self,
        ctx: OrgContext,
        client_id: uuid.UUID,
        today: date,
        ytd_from: date,
        last_30d_from: date,
    ) -> tuple[TimeOverview, int]:
        user_id = self._own_user(ctx, all_permission="time.read_all")
        figures = await self.overview.time_figures(
            ctx.organization_id,
            client_id,
            last_30d_from=last_30d_from,
            ytd_from=ytd_from,
            today=today,
            user_id=user_id,
        )
        return (
            TimeOverview(
                scope="own" if user_id else "all",
                minutes_30d=figures.minutes_30d,
                billable_minutes_30d=figures.billable_minutes_30d,
                minutes_ytd=figures.minutes_ytd,
                billable_minutes_ytd=figures.billable_minutes_ytd,
                last_entry_on=figures.last_entry_on,
            ),
            figures.entries,
        )

    async def _expenses(
        self, ctx: OrgContext, client_id: uuid.UUID, client_vat_rate_id: uuid.UUID | None
    ) -> ExpensesOverview:
        org_id = ctx.organization_id
        created_by = self._own_user(ctx, all_permission="wip.read")
        fallback = 0
        if ctx.organization.vat_registered:
            rates = await self.clients.vat_rates_bp(
                org_id, {client_vat_rate_id} if client_vat_rate_id else set()
            )
            default = await self.clients.default_vat_rate_bp(org_id) or 0
            fallback = rates.get(client_vat_rate_id, default) if client_vat_rate_id else default
        figures = await self.overview.disbursement_figures(
            org_id, client_id, fallback_rate_bp=fallback, created_by=created_by
        )
        return ExpensesOverview(
            scope="own" if created_by else "all",
            count=figures.count,
            net=figures.net,
            # A practice that is not VAT registered charges no VAT on anything.
            gross=figures.gross if ctx.organization.vat_registered else figures.net,
            awaiting_approval=0,
        )

    async def _wip(self, ctx: OrgContext, client_id: uuid.UUID, today: date) -> WipOverview:
        org_id = ctx.organization_id
        approval = ctx.organization.require_timesheet_approval
        page = await self.wip.summary_page(
            org_id,
            require_approval=approval,
            filters=WipSummaryFilters(client_id=client_id),
            today=today,
            params=PageParams(limit=1),
        )
        if not page.items:
            return WipOverview(
                time=0,
                fixed_fee=0,
                expense=0,
                total=0,
                ageing=WipAgeing(days_0_30=0, days_31_60=0, days_61_90=0, days_90_plus=0),
                item_count=0,
            )
        row = page.items[0]
        fixed_fee = await self.wip.fixed_fee_time_value(
            org_id, client_id, require_approval=approval
        )
        return WipOverview(
            time=row.time_value - fixed_fee,
            fixed_fee=fixed_fee,
            expense=row.disbursement_value,
            total=row.time_value + row.disbursement_value,
            ageing=WipAgeing(
                days_0_30=row.age_0_30,
                days_31_60=row.age_31_60,
                days_61_90=row.age_61_90,
                days_90_plus=row.age_91_plus,
            ),
            item_count=row.item_count,
        )

    async def _invoices(
        self, ctx: OrgContext, client_id: uuid.UUID, today: date, ytd_from: date
    ) -> InvoicesOverview:
        org_id = ctx.organization_id
        figures = await self.overview.invoice_figures(
            org_id, client_id, today=today, ytd_from=ytd_from
        )
        # The same balances as the client detail's financial snapshot.
        outstanding, overdue, _ = await self.clients.balances(org_id, client_id, today=today)
        return InvoicesOverview(
            count=figures.count,
            by_status=figures.by_status,
            billed_ytd=figures.billed_ytd,
            outstanding=outstanding,
            overdue=overdue,
            last_invoice_on=figures.last_invoice_on,
        )

    async def _payments(
        self, ctx: OrgContext, client_id: uuid.UUID, today: date, ytd_from: date
    ) -> tuple[PaymentsOverview, int]:
        figures = await self.overview.payment_figures(
            ctx.organization_id, client_id, today=today, ytd_from=ytd_from
        )
        return (
            PaymentsOverview(
                received_ytd=figures.received_ytd,
                count_ytd=figures.count_ytd,
                last_payment_on=figures.last_payment_on,
            ),
            figures.count,
        )

    async def _activity(self, ctx: OrgContext, client_id: uuid.UUID) -> list[ActivityEvent]:
        org_id = ctx.organization_id
        client_ids = [client_id, *await self.overview.merged_into(org_id, client_id)]
        entries = await self.overview.recent_activity(
            org_id, client_ids, allowed_prefixes(ctx), RECENT_ACTIVITY
        )
        actors = await self.audit.get_actors({e.actor_user_id for e in entries if e.actor_user_id})
        return [self._event(e, actors) for e in entries]

    @staticmethod
    def _event(entry: AuditLog, actors: dict[uuid.UUID, User]) -> ActivityEvent:
        actor = actors.get(entry.actor_user_id) if entry.actor_user_id else None
        return ActivityEvent(
            id=entry.id,
            action=entry.action,
            label=label_of(entry.action),
            at=entry.created_at,
            actor_user_id=entry.actor_user_id,
            actor_name=actor.full_name if actor else None,
            target_type=entry.target_type,
            target_id=entry.target_id,
        )
