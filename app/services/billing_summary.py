"""The client's billing summary (Billing > Prepare invoice): for each of the client's jobs the
contract terms, the hours spent and what the work is worth, split by where the value comes from
(the practice's own rates, rates agreed with the client, a fixed fee, other costs), what billing on
each basis would come to, and the cost against the budget and the agreed price.

Read only. The figures are those of the WIP ledger (``app/repositories/wip.py``) and the budget
tracking (``app/repositories/job_budgets.py``) taken together, so nothing here can disagree with
them.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Job
from app.repositories.billing_common import BillingReferenceRepository
from app.repositories.billing_summary import BillingSummaryRepository, TimeRollup
from app.repositories.wip import WipRepository
from app.schemas.billing import ClientRef
from app.schemas.billing_summary import (
    BasisValues,
    Breakdown,
    ClientBillingSummary,
    JobBilling,
)
from app.services.billing_common import get_client_or_404
from app.services.policy import OrgContext, has_permission


def _money(pence: int, currency: str) -> str:
    symbol = {"GBP": "£", "EUR": "€", "USD": "$"}.get(currency)
    amount = f"{pence / 100:,.2f}"
    return f"{symbol}{amount}" if symbol else f"{amount} {currency}"


def terms_of(job: Job | None, currency: str) -> str:
    """The contract terms of a job in words."""
    if job is None:
        return "No job"
    if job.billing_basis == "fixed":
        return f"Fixed fee of {_money(job.fixed_fee or 0, currency)}"
    if job.billing_basis == "non_billable":
        return "Non-billable"
    if job.hourly_rate:
        return f"Hourly at {_money(job.hourly_rate, currency)} an hour (the rate set on the job)"
    return "Hourly at the rates agreed with the client or the practice's own rates"


def _breakdown(
    job: Job | None,
    time: TimeRollup,
    other_costs: int,
    fixed_fee_billed: int,
    *,
    show_cost: bool,
) -> Breakdown:
    fixed = job.fixed_fee if job is not None and job.billing_basis == "fixed" else None
    agreed = fixed if fixed is not None else (job.budget_fee if job is not None else None)
    fee_left = max((fixed or 0) - fixed_fee_billed, 0) if fixed is not None else None
    job_left = (
        fee_left
        if fee_left is not None
        else (max(agreed - time.billed_value, 0) if agreed is not None else None)
    )
    contract_time = fee_left if fee_left is not None else time.ready_value
    return Breakdown(
        logged_minutes=time.logged_minutes,
        billable_minutes=time.billable_minutes,
        non_billable_minutes=time.non_billable_minutes,
        billed_minutes=time.billed_minutes,
        ready_minutes=time.ready_minutes,
        awaiting_approval_minutes=time.awaiting_approval_minutes,
        company_time_value=time.company_value,
        contract_time_value=time.contract_value,
        ready_time_value=time.ready_value,
        billed_time_value=time.billed_value,
        fixed_fee=fixed,
        fixed_fee_billed=fixed_fee_billed,
        other_costs=other_costs,
        billable_total=contract_time + other_costs,
        actual_cost=time.cost_value if show_cost else None,
        budgeted_cost=job.budget_cost if job is not None else None,
        agreed_price=agreed,
        basis_values=BasisValues(
            contract=contract_time + other_costs,
            job=job_left + other_costs if job_left is not None else None,
            team=time.ready_cost_value + other_costs if show_cost else None,
        ),
    )


def _optional_sum(values: list[int | None]) -> int | None:
    found = [v for v in values if v is not None]
    return sum(found) if found else None


def _totals(parts: list[Breakdown], *, show_cost: bool) -> Breakdown:
    def total(name: str) -> int:
        return sum(getattr(p, name) for p in parts)

    return Breakdown(
        logged_minutes=total("logged_minutes"),
        billable_minutes=total("billable_minutes"),
        non_billable_minutes=total("non_billable_minutes"),
        billed_minutes=total("billed_minutes"),
        ready_minutes=total("ready_minutes"),
        awaiting_approval_minutes=total("awaiting_approval_minutes"),
        company_time_value=total("company_time_value"),
        contract_time_value=total("contract_time_value"),
        ready_time_value=total("ready_time_value"),
        billed_time_value=total("billed_time_value"),
        fixed_fee=_optional_sum([p.fixed_fee for p in parts]),
        fixed_fee_billed=total("fixed_fee_billed"),
        other_costs=total("other_costs"),
        billable_total=total("billable_total"),
        actual_cost=_optional_sum([p.actual_cost for p in parts]) if show_cost else None,
        budgeted_cost=_optional_sum([p.budgeted_cost for p in parts]),
        agreed_price=_optional_sum([p.agreed_price for p in parts]),
        basis_values=BasisValues(
            contract=sum(p.basis_values.contract for p in parts),
            job=_optional_sum([p.basis_values.job for p in parts]),
            team=_optional_sum([p.basis_values.team for p in parts]) if show_cost else None,
        ),
    )


class BillingSummaryService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = BillingSummaryRepository(session)
        self.wip = WipRepository(session)
        self.refs = BillingReferenceRepository(session)

    async def client_summary(self, ctx: OrgContext, client_id: uuid.UUID) -> ClientBillingSummary:
        org = ctx.organization
        client = await get_client_or_404(self.refs, org.id, client_id)
        show_cost = has_permission(ctx, "rate.read")
        time = await self.repo.time_by_job(
            org.id, client.id, require_approval=org.require_timesheet_approval
        )
        disbursements = await self.repo.disbursements_by_job(org.id, client.id)
        jobs = await self.repo.jobs(org.id, client.id)
        fixed_ids = [j.id for j in jobs if j.billing_basis == "fixed"]
        billed = await self.wip.fixed_fee_billed(org.id, fixed_ids)

        rows: list[JobBilling] = []
        parts: list[Breakdown] = []
        known = {j.id for j in jobs}
        for job in jobs:
            rollup = time.get(job.id, TimeRollup())
            figures = _breakdown(
                job,
                rollup,
                disbursements.get(job.id, 0),
                billed.get(job.id, 0),
                show_cost=show_cost,
            )
            # A job with nothing on it is left out: the page is about work to bill.
            if rollup.logged_minutes == 0 and figures.other_costs == 0 and job.fixed_fee is None:
                continue
            parts.append(figures)
            rows.append(
                JobBilling(
                    job_id=job.id,
                    number=job.number,
                    name=job.name,
                    status=job.status,
                    billing_basis=job.billing_basis,
                    fixed_fee=job.fixed_fee,
                    hourly_rate=job.hourly_rate,
                    budget_minutes=job.budget_minutes,
                    terms=terms_of(job, org.currency),
                    breakdown=figures,
                )
            )
        # Time and disbursements that belong to no (known) job.
        loose_time = TimeRollup()
        for job_id, rollup in time.items():
            if job_id is None or job_id not in known:
                for name in TimeRollup.__slots__:
                    setattr(loose_time, name, getattr(loose_time, name) + getattr(rollup, name))
        loose_costs = sum(v for k, v in disbursements.items() if k is None or k not in known)
        if loose_time.logged_minutes or loose_costs:
            figures = _breakdown(None, loose_time, loose_costs, 0, show_cost=show_cost)
            parts.append(figures)
            rows.append(
                JobBilling(
                    job_id=None,
                    number=None,
                    name="Work without a job",
                    status=None,
                    billing_basis=None,
                    fixed_fee=None,
                    hourly_rate=None,
                    budget_minutes=None,
                    terms=terms_of(None, org.currency),
                    breakdown=figures,
                )
            )
        time_costs = (
            await self.repo.ready_time_costs(
                org.id, client.id, require_approval=org.require_timesheet_approval
            )
            if show_cost
            else None
        )
        return ClientBillingSummary(
            client=ClientRef.from_model(client),
            jobs=rows,
            totals=_totals(parts, show_cost=show_cost),
            show_cost=show_cost,
            time_costs=time_costs,
        )
