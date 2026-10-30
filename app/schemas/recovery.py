"""The recovery analysis: what the work was invoiced at against what it could have been.

For the billable time of a scope (a client, a job, a person, a report's filters) it compares the
invoice value with three potentials: the time at the task rate (the rates it was priced at: the
job's or service's rate, a rate agreed with the client, else the person's), at each person's own
charge-out rate, and at cost plus the practice's markup. A positive shortfall is revenue left on
the table, a negative one more than that basis would have billed. Money is pence, durations whole
minutes, percentages basis points (``null`` when the denominator is zero). Cost figures are
``null`` without ``rate.read``.
"""

import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.billing import ClientRef
from app.schemas.reports import PagedQuery, PagedReport, ReportFilters

RecoveryGroupBy = Literal["job", "client", "person"]
_PARTNER = "Only work whose responsible partner (job's, else client's) is this person"
_MANAGER = "Only work whose responsible manager (job's, else client's) is this person"

_SHORTFALL = (
    "{basis} - invoice_value: revenue left on the table when positive, more than that basis "
    "would have billed when negative"
)


class RecoveryPotentials(BaseModel):
    """What the time would bill at on each basis (no invoice value to compare with yet)."""

    minutes: int = Field(description="Billable minutes")
    task_value: int = Field(
        description=(
            "The time at the task rate: the rates it was priced at when logged (the job's own "
            "rate, a service rate, a rate agreed with the client, else the person's or grade's)"
        )
    )
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
        description="cost_value plus the markup (`markup_bp`); null without `rate.read`"
    )


class RecoveryFigures(RecoveryPotentials):
    """The potentials against the invoice value."""

    invoiced_value: int = Field(
        description="What issued invoices billed for the time (fixed fees spread over the time)"
    )
    to_bill_value: int = Field(
        description=(
            "What the time not invoiced yet will bill: its share of a draft invoice, else its "
            "value at the job's terms (the fixed fee still to bill, spread over the unbilled time "
            "of the job; the time's value on an hourly job). Written-off time bills nothing"
        )
    )
    invoice_value: int = Field(description="invoiced_value + to_bill_value")
    task_shortfall: int = Field(description=_SHORTFALL.format(basis="task_value"))
    task_recovery_bp: int | None = Field(description="invoice_value / task_value")
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


# --- the report ----------------------------------------------------------------------------


class RecoveryAnalysisQuery(PagedQuery):
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
    markup_bp: int | None = Field(
        None,
        ge=0,
        le=100_000,
        description=(
            "The cost-plus markup in basis points (5000 = 50%). Default: the practice's "
            "`cost_plus_markup_bp`"
        ),
    )


class RecoveryAnalysisRow(RecoveryFigures):
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


class RecoveryAnalysisTotals(RecoveryFigures):
    pass


class RecoveryAnalysisFilters(ReportFilters):
    markup_bp: int = Field(description="The cost-plus markup the report was worked out with")


class RecoveryAnalysisReport(PagedReport[RecoveryAnalysisRow, RecoveryAnalysisTotals]):
    filters: RecoveryAnalysisFilters


# --- one client ----------------------------------------------------------------------------


class JobRecovery(RecoveryFigures):
    job_id: uuid.UUID | None = Field(description="Null for time on the client without a job")
    number: str | None
    name: str
    status: str | None
    billing_basis: str | None = Field(description="hourly, fixed or non_billable")
    fixed_fee: int | None
    hourly_rate: int | None = Field(description="The job's own charge-out rate, if it has one")
    terms: str = Field(description="The contract terms in words: 'Fixed fee of £1,500.00'")


class ClientRecovery(BaseModel):
    client: ClientRef
    jobs: list[JobRecovery] = Field(description="Jobs with billable time, newest first")
    totals: RecoveryFigures
    markup_bp: int = Field(description="The cost-plus markup the figures were worked out with")
    show_cost: bool = Field(description="Whether the caller may see cost (`rate.read`)")
