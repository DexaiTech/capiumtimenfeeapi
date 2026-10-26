"""The client's billing summary: hours, value by source, cost, and budget against actual."""

import uuid
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.billing import ClientRef

BillingBasis = Literal["contract", "job", "team"]


class BasisValues(BaseModel):
    """What billing the work ready to bill would come to on each billing basis."""

    contract: int = Field(
        description=(
            "Contract value: the time at the rates in force (agreed with the client where there "
            "is such a rate, the practice's own rates otherwise), plus the disbursements"
        )
    )
    job: int | None = Field(
        description=(
            "Job value: what is left of the value set for the job (the fixed fee, or the fee "
            "budget of an hourly job) after what has been billed; null without such a value"
        )
    )
    team: int | None = Field(
        description=(
            "Team value: the cost of the people who did the work, plus the disbursements; "
            "null without `rate.read`"
        )
    )


class Breakdown(BaseModel):
    """One job's figures, or the whole client's. Money is pence, durations whole minutes."""

    logged_minutes: int = Field(description="Every entry, billable or not, billed or not")
    billable_minutes: int
    non_billable_minutes: int
    billed_minutes: int = Field(description="Billable minutes already on an invoice")
    ready_minutes: int = Field(description="Billable, unbilled and, when required, approved")
    awaiting_approval_minutes: int = Field(
        description="Billable and unbilled but on a timesheet that is not approved yet"
    )
    company_time_value: int = Field(
        description="Unbilled billable time valued at the practice's own rates"
    )
    contract_time_value: int = Field(
        description=(
            "Unbilled billable time valued at rates agreed with the client (a client rate or the "
            "job's own rate)"
        )
    )
    ready_time_value: int = Field(description="The part of that time that can be billed now")
    billed_time_value: int = Field(description="Billable time already billed, at its value")
    fixed_fee: int | None = Field(description="The fixed fee of a fixed-fee job")
    fixed_fee_billed: int = Field(description="Net already put on fixed-fee lines")
    other_costs: int = Field(description="Disbursements ready to bill")
    billable_total: int = Field(
        description="Ready time value + fixed fee still to bill + other costs: what can be billed"
    )
    actual_cost: int | None = Field(
        description="The cost of the people who did the work; null without `rate.read`"
    )
    budgeted_cost: int | None = Field(
        description="The cost the work was expected to take (job `budget_cost`)"
    )
    agreed_price: int | None = Field(
        description="The price agreed with the client: the fixed fee, else the fee budget"
    )
    basis_values: BasisValues


class JobBilling(BaseModel):
    job_id: uuid.UUID | None = Field(description="Null for time on the client without a job")
    number: str | None
    name: str
    status: str | None
    billing_basis: str | None = Field(description="hourly, fixed or non_billable")
    fixed_fee: int | None
    hourly_rate: int | None = Field(description="The job's own charge-out rate, if it has one")
    budget_minutes: int | None
    terms: str = Field(description="The contract terms in words: 'Fixed fee of £2,000.00'")
    breakdown: Breakdown


class ClientBillingSummary(BaseModel):
    client: ClientRef
    jobs: list[JobBilling]
    totals: Breakdown
    show_cost: bool = Field(description="Whether the caller may see cost (`rate.read`)")
    time_costs: dict[uuid.UUID, int] | None = Field(
        description=(
            "The cost of each time entry ready to bill, by entry id, for billing on the team's "
            "cost; null without `rate.read`"
        )
    )
