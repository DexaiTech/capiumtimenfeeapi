"""Recovery analysis and Cost to Business (docs/modules/reports.md, "Recovery analysis").

What the billable work was invoiced at, against what it would have billed on three other bases:

* the **recovery rate**: for each piece of work the first rate that exists, in the practice's
  order (``organizations.recovery_rate_order``, default client job → task → staff), of the
  **client job-specific rate** (agreed with the client, else the job's own rate), the **task
  rate** (set against the job's service) and the **staff charge-out rate**, on the day it was
  worked (``recovery_values.value_work``);
* the **staff rate**: each person's own charge-out rate that day, whatever the job or client;
* **cost plus**: what the time cost (each person's cost rate) plus the mark-up of the job, else of
  its client, else of the practice (``cost_plus_markup_bp``; 50% by default: 50.00 is 75.00).

The **invoice value** of a piece of time is what the invoice it is on bills it at: an issued
invoice's ``billed_value`` less the time's share of the credit notes allocated to that invoice,
or a draft's ``billed_value`` (a draft counts: it is the invoice being created; it is shown apart
as ``draft_value``). Only that work, and written-off work (invoice value 0), is compared. Time
not on an invoice yet is WIP: it has no invoice value (so a fixed fee shows only in the WIP report
until it is invoiced) and is reported apart (``wip_minutes``, ``wip_value``). Money the client
paid that is not allocated to an invoice is a credit on the account: shown on the client, it
reduces no invoice value.

The difference between each potential and the invoice value is the revenue left on the table
(positive) or the over-recovery (negative). **Cost to Business** sets what the work cost the
practice (``time_entries.cost_value``: what it pays each person) against what it was invoiced
at, for all the time, billable or not. Read only; cost figures need ``rate.read``. The same
pieces feed the reports, the client's Invoices & Recovery and the invoicing screen, so they agree.
"""

import uuid
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Organization
from app.repositories.billing_common import BillingReferenceRepository
from app.repositories.billing_summary import BillingSummaryRepository
from app.repositories.recovery import RecoveryRepository
from app.repositories.reports import DateRange, ItemFilter
from app.schemas.billing import ClientRef
from app.schemas.recovery import (
    ClientRecovery,
    JobRecovery,
    RecoveryFigures,
    RecoveryGroupBy,
)
from app.services.billing_common import get_client_or_404
from app.services.billing_summary import terms_of
from app.services.policy import OrgContext, has_permission
from app.services.recovery_values import (
    COST_PERMISSION,
    SETTLED,
    Piece,
    Tally,
    Work,
    figures,
    tally_by,
    value_work,
)
from app.utils.money import round_half_up

_NO_KEY = {"job": "No job", "client": "No client (internal time)", "person": "Unknown person"}


class RecoveryService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = RecoveryRepository(session)

    # --- the pieces ------------------------------------------------------------------------

    async def pieces(
        self,
        organization: Organization,
        period: DateRange,
        f: ItemFilter,
        *,
        markup_bp: int | None = None,
        billable_only: bool = True,
        valued: bool = True,
    ) -> list[Piece]:
        """The time of the scope. With ``valued`` the billable time on an invoice or written off
        is valued on every basis. ``markup_bp`` overrides every mark-up (a what-if)."""
        org_id = organization.id
        rows = await self.repo.time_pieces(org_id, period, f, billable_only=billable_only)
        if not rows:
            return []
        jobs = await self.repo.jobs(org_id, {r["job_id"] for r in rows if r["job_id"]})
        clients = await self.repo.clients(org_id, {r["client_id"] for r in rows if r["client_id"]})

        def markup_of(row: Any) -> int:
            if markup_bp is not None:
                return markup_bp
            job = jobs.get(row["job_id"]) if row["job_id"] else None
            if job is not None and job.cost_plus_markup_bp is not None:
                return job.cost_plus_markup_bp
            client = clients.get(row["client_id"]) if row["client_id"] else None
            if client is not None and client.cost_plus_markup_bp is not None:
                return client.cost_plus_markup_bp
            return organization.cost_plus_markup_bp

        compared = [
            i
            for i, r in enumerate(rows)
            if valued and r["is_billable"] and r["billing_status"] in SETTLED
        ]
        values = await value_work(
            self.session,
            organization,
            [
                Work(
                    rows[i]["user_id"],
                    rows[i]["client_id"],
                    rows[i]["job_id"],
                    rows[i]["entry_date"],
                    rows[i]["is_overtime"],
                    int(rows[i]["minutes"]),
                )
                for i in compared
            ],
        )
        value_of = dict(zip(compared, values, strict=True))
        pieces: list[Piece] = []
        for i, r in enumerate(rows):
            billed = int(r["billed_value"])
            status = r["billing_status"]
            found = value_of.get(i)
            pieces.append(
                Piece(
                    job_id=r["job_id"],
                    client_id=r["client_id"],
                    user_id=r["user_id"],
                    billable=r["is_billable"],
                    status=status,
                    minutes=int(r["minutes"]),
                    charge_value=int(r["charge_value"]),
                    cost_value=int(r["cost_value"]),
                    markup_bp=markup_of(r),
                    recovery_value=found.recovery_value if found else 0,
                    tier=found.tier if found else "staff",
                    staff_value=found.staff_value if found else 0,
                    invoiced_value=billed if status == "billed" else 0,
                    draft_value=billed if status == "in_draft" else 0,
                    credited_value=round_half_up(Decimal(r["credited"]))
                    if status == "billed"
                    else 0,
                )
            )
        return pieces

    # --- the report's rows -----------------------------------------------------------------

    async def report_rows[RowT: BaseModel](
        self,
        org_id: uuid.UUID,
        pieces: Sequence[Piece],
        group_by: RecoveryGroupBy,
        row: Callable[..., RowT],
        values: Callable[[Tally], dict[str, Any]],
    ) -> list[RowT]:
        """One row per job, client or person: ``row(**key fields, **values(tally))``."""
        found: list[tuple[dict[str, Any], Tally]] = []
        if group_by == "job":
            by_job = tally_by(pieces, lambda p: (p.job_id, p.client_id))
            jobs = await self.repo.jobs(org_id, {job for job, _ in by_job if job})
            clients = await self.repo.clients(org_id, {client for _, client in by_job if client})
            for (job_id, client_id), tally in by_job.items():
                job = jobs.get(job_id) if job_id else None
                client = clients.get(client_id) if client_id else None
                key = {
                    "key": str(job_id) if job_id else None,
                    "code": job.number if job else None,
                    "name": job.name if job else _NO_KEY["job"],
                    "client_name": client.name if client else None,
                    "job_id": job_id,
                    "client_id": client_id,
                    "billing_basis": job.billing_basis if job else None,
                    "fixed_fee": job.fixed_fee if job else None,
                }
                found.append((key, tally))
        elif group_by == "client":
            by_client = tally_by(pieces, lambda p: p.client_id)
            clients = await self.repo.clients(org_id, {c for c in by_client if c})
            for client_id, tally in by_client.items():
                client = clients.get(client_id) if client_id else None
                key = {
                    "key": str(client_id) if client_id else None,
                    "code": client.code if client else None,
                    "name": client.name if client else _NO_KEY["client"],
                    "client_name": None,
                    "client_id": client_id,
                }
                found.append((key, tally))
        else:
            by_person = tally_by(pieces, lambda p: p.user_id)
            people = await self.repo.people(org_id, set(by_person))
            for user_id, tally in by_person.items():
                person = people.get(user_id)
                key = {
                    "key": str(user_id),
                    "code": person.initials if person else None,
                    "name": person.full_name if person else _NO_KEY["person"],
                    "client_name": None,
                    "user_id": user_id,
                }
                found.append((key, tally))
        found.sort(
            key=lambda item: (
                item[0]["key"] is None,
                str(item[0]["name"]).lower(),
                str(item[0]["client_name"] or "").lower(),
                item[0]["code"] or "",
                item[0]["key"] or "",
            )
        )
        return [row(**key, **values(tally)) for key, tally in found]

    # --- one client ------------------------------------------------------------------------

    async def client_recovery(self, ctx: OrgContext, client_id: uuid.UUID) -> ClientRecovery:
        """Every job of the client with billable time, and the client's totals (all dates)."""
        org = ctx.organization
        refs = BillingReferenceRepository(self.session)
        client = await get_client_or_404(refs, org.id, client_id)
        show_cost = has_permission(ctx, COST_PERMISSION)
        pieces = await self.pieces(org, DateRange(None, None), ItemFilter(client_id=client.id))
        by_job = tally_by(pieces, lambda p: p.job_id)
        jobs = await BillingSummaryRepository(self.session).jobs(org.id, client.id)
        rows: list[JobRecovery] = []
        for job in jobs:
            tally = by_job.pop(job.id, None)
            if tally is None:
                continue
            rows.append(
                JobRecovery(
                    job_id=job.id,
                    number=job.number,
                    name=job.name,
                    status=job.status,
                    billing_basis=job.billing_basis,
                    fixed_fee=job.fixed_fee,
                    hourly_rate=job.hourly_rate,
                    job_markup_bp=job.cost_plus_markup_bp,
                    terms=terms_of(job, org.currency),
                    **figures(tally, show_cost),
                )
            )
        # Time without a job, or on a job that is not (or no longer) one of the client's.
        loose = Tally.of(by_job.values())
        if loose.minutes or loose.wip_minutes:
            rows.append(
                JobRecovery(
                    job_id=None,
                    number=None,
                    name="Work without a job",
                    status=None,
                    billing_basis=None,
                    fixed_fee=None,
                    hourly_rate=None,
                    job_markup_bp=None,
                    terms=terms_of(None, org.currency),
                    **figures(loose, show_cost),
                )
            )
        return ClientRecovery(
            client=ClientRef.from_model(client),
            jobs=rows,
            totals=RecoveryFigures(**figures(Tally.of(pieces), show_cost)),
            practice_markup_bp=org.cost_plus_markup_bp,
            client_markup_bp=client.cost_plus_markup_bp,
            rate_order=list(org.recovery_rate_order),
            credit_on_account=await self.repo.credit_on_account(org.id, client.id),
            show_cost=show_cost,
        )
