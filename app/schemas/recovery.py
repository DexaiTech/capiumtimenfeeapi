"""The recovery analysis: what the work was invoiced at against what it could have been, and what
it cost the business.

For the billable time of a scope (a client, a job, a person, a report's filters) on an invoice or
written off, it compares the invoice value with three potentials: the time at the **recovery
rate** (the first of the client job-specific rate, the task rate and the staff charge-out rate
that exists, in the practice's order), at each person's own charge-out rate, and at cost plus the
mark-up (the job's, else the client's, else the practice's). A positive shortfall is revenue left
on the table, a negative one more than that basis would have billed. Time not invoiced yet is WIP:
it is shown apart and has no invoice value (a fixed fee counts once it is on an invoice). Money is
pence, durations whole minutes, percentages basis points (``null`` when the denominator is zero).
Cost figures are ``null`` without ``rate.read``.
"""

import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.billing import ClientRef
from app.schemas.organization import RecoveryRate
from app.schemas.reports import PagedQuery, PagedReport, ReportFilters

RecoveryGroupBy = Literal["job", "client", "person"]
_PARTNER = "Only work whose responsible partner (job's, else client's) is this person"
_MANAGER = "Only work whose responsible manager (job's, else client's) is this person"

_SHORTFALL = (
    "{basis} - invoice_value: revenue left on the table when positive, more than that basis "
    "would have billed when negative"
)


class RecoveryPotentials(BaseModel):
    """What the work would bill at on each basis."""

    minutes: int = Field(
        description="Billable minutes compared: on an invoice (issued or draft) or written off"
    )
    recovery_value: int = Field(
        description=(
            "The time at the recovery rate: for each piece the first rate that exists, in the "
            "practice's `recovery_rate_order`, of the client job-specific rate (agreed with the "
            "client, else the job's own rate), the task rate (set against the job's service) and "
            "the staff charge-out rate, on the day it was worked"
        )
    )
    client_job_minutes: int = Field(description="Minutes valued at a client job-specific rate")
    task_minutes: int = Field(description="Minutes valued at a task rate")
    staff_rate_minutes: int = Field(description="Minutes valued at the staff charge-out rate")
    staff_value: int = Field(
        description=(
            "The time at each person's own charge-out rate on the day it was worked (their staff "
            "rate, else their grade's, else the practice default), whatever the job or client"
        )
    )
    cost_value: int | None = Field(
        description="What the time cost (each person's cost rate); null without `rate.read`"
    )
    cost_plus_value: int | None = Field(
        description=(
            "cost_value plus the mark-up (the job's, else the client's, else the practice's), "
            "rounded once per mark-up; null without `rate.read`"
        )
    )
    markup_bp: int | None = Field(
        description="The cost-plus mark-up of the work when it has one only, else null"
    )


class RecoveryFigures(RecoveryPotentials):
    """The potentials against the invoice value."""

    invoiced_value: int = Field(
        description="What issued invoices billed for the time (fixed fees spread over the time)"
    )
    credited_value: int = Field(
        description=(
            "The time's share of the credit notes allocated to those invoices (net, by value): "
            "it reduces the invoice value"
        )
    )
    draft_value: int = Field(description="What draft invoices bill for the time")
    invoice_value: int = Field(description="invoiced_value - credited_value + draft_value")
    recovery_shortfall: int = Field(description=_SHORTFALL.format(basis="recovery_value"))
    recovery_bp: int | None = Field(description="invoice_value / recovery_value")
    staff_shortfall: int = Field(description=_SHORTFALL.format(basis="staff_value"))
    staff_recovery_bp: int | None = Field(description="invoice_value / staff_value")
    cost_plus_shortfall: int | None = Field(
        description=_SHORTFALL.format(basis="cost_plus_value") + "; null without `rate.read`"
    )
    cost_plus_recovery_bp: int | None = Field(
        description="invoice_value / cost_plus_value; null without `rate.read`"
    )
    margin_value: int | None = Field(
        description="invoice_value - cost_value; null without `rate.read`"
    )
    margin_bp: int | None = Field(description="margin_value / invoice_value")
    wip_minutes: int = Field(
        description="Billable minutes not on an invoice yet (WIP): not compared, no invoice value"
    )
    wip_value: int = Field(description="Their value at the rates they were recorded at")


# --- the report ----------------------------------------------------------------------------


class _ScopeQuery(PagedQuery):
    date_from: date | None = Field(
        None, description="Time worked on or after this day. Default: no lower bound"
    )
    date_to: date | None = Field(
        None, description="Time worked on or before this day. Default: no upper bound"
    )
    group_by: RecoveryGroupBy = Field("job", description="How rows are grouped")
    client_id: uuid.UUID | None = Field(None, description="Only this client's time")
    job_id: uuid.UUID | None = Field(None, description="Only this job's time")
    user_id: uuid.UUID | None = Field(None, description="Only this person's time")
    partner_user_id: uuid.UUID | None = Field(None, description=_PARTNER)
    manager_user_id: uuid.UUID | None = Field(None, description=_MANAGER)
    service_id: uuid.UUID | None = Field(None, description="Only jobs of this service")


class RecoveryAnalysisQuery(_ScopeQuery):
    markup_bp: int | None = Field(
        None,
        ge=0,
        le=100_000,
        description=(
            "A what-if cost-plus mark-up in basis points (5000 = 50%) for all the work. Default: "
            "each job's, else its client's, else the practice's `cost_plus_markup_bp`"
        ),
    )


class _RowKey(BaseModel):
    key: str | None = Field(description="Job, client or person id (null: no job / no client)")
    code: str | None = Field(description="Job number, client code or initials")
    name: str
    client_name: str | None = Field(description="The job's client (group_by=job only)")
    user_id: uuid.UUID | None = Field(None, description="group_by=person: the person")
    client_id: uuid.UUID | None = Field(None, description="group_by=client or job: the client")
    job_id: uuid.UUID | None = Field(None, description="group_by=job: the job")
    billing_basis: str | None = Field(
        None, description="group_by=job: hourly, fixed or non_billable"
    )
    fixed_fee: int | None = Field(None, description="group_by=job: the fixed fee of the job")


class RecoveryAnalysisRow(RecoveryFigures, _RowKey):
    pass


class RecoveryAnalysisTotals(RecoveryFigures):
    pass


class RecoveryAnalysisFilters(ReportFilters):
    markup_bp: int | None = Field(
        description="The what-if mark-up the report was worked out with; null: each job's own"
    )
    practice_markup_bp: int = Field(description="The practice's `cost_plus_markup_bp`")
    rate_order: list[RecoveryRate] = Field(description="The practice's `recovery_rate_order`")


class RecoveryAnalysisReport(PagedReport[RecoveryAnalysisRow, RecoveryAnalysisTotals]):
    filters: RecoveryAnalysisFilters


# --- Cost to Business ----------------------------------------------------------------------


class CostToBusinessQuery(_ScopeQuery):
    pass


class CostToBusinessFigures(BaseModel):
    """What the work cost the practice, at what it pays each person (their cost rate)."""

    minutes: int = Field(description="All the time, billable or not")
    billable_minutes: int
    non_billable_minutes: int
    cost_value: int = Field(description="What all the time cost (`time_entries.cost_value`)")
    invoiced_cost: int = Field(
        description="Cost of the billable time on an invoice (issued or draft) or written off"
    )
    wip_cost: int = Field(description="Cost of the billable time not invoiced yet")
    non_billable_cost: int = Field(description="Cost of the non-billable time")
    cost_per_hour: int | None = Field(description="cost_value per hour of all the time")
    invoice_value: int = Field(
        description="What the invoices bill for the time, less credit notes allocated to them"
    )
    margin_value: int = Field(description="invoice_value - invoiced_cost")
    margin_bp: int | None = Field(description="margin_value / invoice_value")


class CostToBusinessRow(CostToBusinessFigures, _RowKey):
    pass


class CostToBusinessTotals(CostToBusinessFigures):
    pass


class CostToBusinessReport(PagedReport[CostToBusinessRow, CostToBusinessTotals]):
    filters: ReportFilters


# --- one client ----------------------------------------------------------------------------


class JobRecovery(RecoveryFigures):
    job_id: uuid.UUID | None = Field(description="Null for time on the client without a job")
    number: str | None
    name: str
    status: str | None
    billing_basis: str | None = Field(description="hourly, fixed or non_billable")
    fixed_fee: int | None
    hourly_rate: int | None = Field(
        description="The job's own hourly rate: the client job-specific rate agreed for it"
    )
    job_markup_bp: int | None = Field(description="The job's own cost-plus mark-up, if set")
    terms: str = Field(description="The contract terms in words: 'Fixed fee of £1,500.00'")


class ClientRecovery(BaseModel):
    client: ClientRef
    jobs: list[JobRecovery] = Field(description="Jobs with billable time, newest first")
    totals: RecoveryFigures
    practice_markup_bp: int = Field(description="The practice's `cost_plus_markup_bp`")
    client_markup_bp: int | None = Field(description="The client's own mark-up, if set")
    rate_order: list[RecoveryRate] = Field(description="The practice's `recovery_rate_order`")
    credit_on_account: int = Field(
        description=(
            "Money the client paid that is not allocated to an invoice: a credit balance for a "
            "future invoice. It reduces no invoice value until it is allocated"
        )
    )
    show_cost: bool = Field(description="Whether the caller may see cost (`rate.read`)")
