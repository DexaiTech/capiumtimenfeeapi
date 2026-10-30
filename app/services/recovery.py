"""Recovery analysis (docs/modules/reports.md, "Recovery analysis").

What the billable work was invoiced at, against what it would have billed on three other bases:

* the **task rate**: the time at the rates it was priced at when it was logged (the job's own rate,
  a service rate, a rate agreed with the client, else the person's or grade's: the charge-out
  ladder of ``charge_rates``), i.e. its value in the WIP ledger;
* the **staff rate**: the time at each person's own charge-out rate on the day it was worked,
  whatever the job or client (the ladder resolved without a client or job: the person's staff
  rate, else their grade's, else the practice default; overtime priced as everywhere else);
* **cost plus**: what the time cost (each person's cost rate) plus the practice's markup
  (``organizations.cost_plus_markup_bp``, 50% by default: 50.00 of cost is worth 75.00).

The **invoice value** of a piece of time is what its invoice bills it at (an issued invoice:
``invoiced_value``; a draft: ``to_bill_value``), and for time not on an invoice yet what the job's
terms will bill it at (``to_bill_value``): the fixed fee still to bill spread over the job's
unbilled time by value, or the time's own value on an hourly job. Written-off time bills nothing.

The difference between each potential and the invoice value is the revenue left on the table
(positive) or the over-recovery (negative). Read only; cost figures are ``None`` without
``rate.read``. The same pieces feed the report, the client's Invoices & Recovery and the invoicing
screen, so the three always agree.
"""

import uuid
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Organization
from app.repositories.billing_common import BillingReferenceRepository
from app.repositories.billing_summary import BillingSummaryRepository
from app.repositories.recovery import JobTerms, RecoveryRepository
from app.repositories.reports import DateRange, ItemFilter
from app.repositories.wip import WipRepository
from app.schemas.billing import ClientRef
from app.schemas.recovery import (
    ClientRecovery,
    JobRecovery,
    RecoveryAnalysisRow,
    RecoveryFigures,
    RecoveryGroupBy,
)
from app.services.billing_common import get_client_or_404
from app.services.billing_summary import terms_of
from app.services.policy import OrgContext, has_permission
from app.services.recovery_values import (
    COST_PERMISSION,
    Piece,
    Tally,
    figures,
    staff_values,
    tally_by,
)
from app.utils.money import apportion

_NO_KEY = {"job": "No job", "client": "No client (internal time)", "person": "Unknown person"}


class RecoveryService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = RecoveryRepository(session)

    # --- the pieces ------------------------------------------------------------------------

    async def pieces(
        self, organization: Organization, period: DateRange, f: ItemFilter
    ) -> list[Piece]:
        """The billable time of the scope, valued on every basis."""
        org_id = organization.id
        rows = await self.repo.time_pieces(org_id, period, f)
        if not rows:
            return []
        staff = await staff_values(
            self.session,
            organization,
            [(r["user_id"], r["entry_date"], r["is_overtime"], int(r["minutes"])) for r in rows],
        )
        jobs = await self.repo.jobs(org_id, {r["job_id"] for r in rows if r["job_id"]})
        unbilled_fixed = {
            r["job_id"]
            for r in rows
            if r["billing_status"] == "unbilled"
            and r["job_id"] in jobs
            and jobs[r["job_id"]].billing_basis == "fixed"
        }
        shares = await self._fixed_fee_shares(org_id, jobs, unbilled_fixed)
        pieces: list[Piece] = []
        for r, staff_value in zip(rows, staff, strict=True):
            charge, billed = int(r["charge_value"]), int(r["billed_value"])
            invoiced = to_bill = 0
            match r["billing_status"]:
                case "billed":
                    invoiced = billed
                case "in_draft":
                    to_bill = billed
                case "unbilled":
                    job = jobs.get(r["job_id"]) if r["job_id"] else None
                    if job is None or job.billing_basis == "hourly":
                        to_bill = charge
                    elif job.billing_basis == "fixed":
                        key = (r["job_id"], r["user_id"], r["entry_date"], r["is_overtime"])
                        to_bill = shares.get(key, 0)
                case _:  # written off: nothing is billed for it
                    pass
            pieces.append(
                Piece(
                    job_id=r["job_id"],
                    client_id=r["client_id"],
                    user_id=r["user_id"],
                    minutes=int(r["minutes"]),
                    task_value=charge,
                    staff_value=staff_value,
                    cost_value=int(r["cost_value"]),
                    invoiced_value=invoiced,
                    to_bill_value=to_bill,
                )
            )
        return pieces

    async def _fixed_fee_shares(
        self, org_id: uuid.UUID, jobs: dict[uuid.UUID, JobTerms], job_ids: set[uuid.UUID]
    ) -> dict[tuple[Any, ...], int]:
        """For each fixed-fee job, the fee not yet put on an invoice spread over all of the job's
        unbilled time (whatever the filters) by value, keyed as the pieces are."""
        if not job_ids:
            return {}
        already = await WipRepository(self.session).fixed_fee_billed(org_id, job_ids)
        by_job: dict[uuid.UUID, list[Any]] = defaultdict(list)
        for row in await self.repo.unbilled_pieces(org_id, job_ids):
            by_job[row["job_id"]].append(row)
        shares: dict[tuple[Any, ...], int] = {}
        for job_id, rows in by_job.items():
            left = max((jobs[job_id].fixed_fee or 0) - already.get(job_id, 0), 0)
            parts = apportion(left, [int(row["charge_value"]) for row in rows])
            for row, part in zip(rows, parts, strict=True):
                key = (job_id, row["user_id"], row["entry_date"], row["is_overtime"])
                shares[key] = part
        return shares

    # --- the report's rows -----------------------------------------------------------------

    async def report_rows(
        self,
        org_id: uuid.UUID,
        pieces: Sequence[Piece],
        group_by: RecoveryGroupBy,
        markup_bp: int,
        show_cost: bool,
    ) -> list[RecoveryAnalysisRow]:
        rows: list[RecoveryAnalysisRow] = []
        if group_by == "job":
            by_job = tally_by(pieces, lambda p: (p.job_id, p.client_id))
            jobs = await self.repo.jobs(org_id, {job for job, _ in by_job if job})
            clients = await self.repo.clients(org_id, {client for _, client in by_job if client})
            for (job_id, client_id), tally in by_job.items():
                job = jobs.get(job_id) if job_id else None
                client = clients.get(client_id) if client_id else None
                rows.append(
                    RecoveryAnalysisRow(
                        key=str(job_id) if job_id else None,
                        code=job.number if job else None,
                        name=job.name if job else _NO_KEY["job"],
                        client_name=client.name if client else None,
                        job_id=job_id,
                        client_id=client_id,
                        billing_basis=job.billing_basis if job else None,
                        fixed_fee=job.fixed_fee if job else None,
                        **figures(tally, markup_bp, show_cost),
                    )
                )
        elif group_by == "client":
            by_client = tally_by(pieces, lambda p: p.client_id)
            clients = await self.repo.clients(org_id, {c for c in by_client if c})
            for client_id, tally in by_client.items():
                client = clients.get(client_id) if client_id else None
                rows.append(
                    RecoveryAnalysisRow(
                        key=str(client_id) if client_id else None,
                        code=client.code if client else None,
                        name=client.name if client else _NO_KEY["client"],
                        client_name=None,
                        client_id=client_id,
                        **figures(tally, markup_bp, show_cost),
                    )
                )
        else:
            by_person = tally_by(pieces, lambda p: p.user_id)
            people = await self.repo.people(org_id, set(by_person))
            for user_id, tally in by_person.items():
                person = people.get(user_id)
                rows.append(
                    RecoveryAnalysisRow(
                        key=str(user_id),
                        code=person.initials if person else None,
                        name=person.full_name if person else _NO_KEY["person"],
                        client_name=None,
                        user_id=user_id,
                        **figures(tally, markup_bp, show_cost),
                    )
                )
        rows.sort(
            key=lambda row: (
                row.key is None,
                row.name.lower(),
                (row.client_name or "").lower(),
                row.code or "",
                row.key or "",
            )
        )
        return rows

    # --- one client ------------------------------------------------------------------------

    async def client_recovery(self, ctx: OrgContext, client_id: uuid.UUID) -> ClientRecovery:
        """Every job of the client with billable time, and the client's totals (all dates)."""
        org = ctx.organization
        refs = BillingReferenceRepository(self.session)
        client = await get_client_or_404(refs, org.id, client_id)
        show_cost = has_permission(ctx, COST_PERMISSION)
        markup = org.cost_plus_markup_bp
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
                    terms=terms_of(job, org.currency),
                    **figures(tally, markup, show_cost),
                )
            )
        # Time without a job, or on a job that is not (or no longer) one of the client's.
        loose = Tally.of(by_job.values())
        if loose.minutes:
            rows.append(
                JobRecovery(
                    job_id=None,
                    number=None,
                    name="Work without a job",
                    status=None,
                    billing_basis=None,
                    fixed_fee=None,
                    hourly_rate=None,
                    terms=terms_of(None, org.currency),
                    **figures(loose, markup, show_cost),
                )
            )
        return ClientRecovery(
            client=ClientRef.from_model(client),
            jobs=rows,
            totals=RecoveryFigures(**figures(Tally.of(pieces), markup, show_cost)),
            markup_bp=markup,
            show_cost=show_cost,
        )
