"""Response models of the rates of a client's jobs (APT54)."""

import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

JobRateSource = Literal["fixed_fee", "non_billable", "client_rule", "job", "task", "staff"]


class ClientRuleRef(BaseModel):
    """A rate the practice agreed with the client for everyone, which comes before a job's rate."""

    id: uuid.UUID
    level: Literal["client_service", "client"] = Field(
        description="`client_service`: the client's rate for the job's service; `client`: the "
        "client's rate for everyone on any service"
    )
    rate: int = Field(description="Pence per hour")
    effective_from: date
    effective_to: date | None


class TaskRate(BaseModel):
    """The rates set against the job's service (service + grade, staff + service) in force; no
    service + grade rate while the practice prices by staff rates only."""

    low: int = Field(description="The lowest, pence per hour")
    high: int = Field(description="The highest, pence per hour (= `low` when they agree)")
    rules: int = Field(description="How many rules there are")


class JobRateRead(BaseModel):
    job_id: uuid.UUID
    billing_basis: Literal["hourly", "fixed", "non_billable"]
    fixed_fee: int | None
    job_rate: int | None = Field(
        description="The job's own hourly rate (`jobs.hourly_rate`): the client's rate agreed for "
        "this job. Null: none, the job follows the task rate"
    )
    task_rate: TaskRate | None = Field(
        description="The task (service) rates the job would otherwise follow; null without a "
        "service, without such a rate or without `rate.read`"
    )
    client_rule: ClientRuleRef | None = Field(
        description="A rate agreed with the client for everyone that prices the job's time before "
        "the job's own rate (null without `rate.read`)"
    )
    person_rules: int | None = Field(
        description="Rates agreed with the client for named people (client + service + staff, "
        "client + staff) that price those people's time first (null without `rate.read`)"
    )
    source: JobRateSource = Field(
        description="What the job's time is billed at: `fixed_fee`, `non_billable`, "
        "`client_rule`, `job` (its own rate), `task` (the service's rates) or `staff` (each "
        "person's charge-out rate)"
    )


class ClientJobRates(BaseModel):
    client_id: uuid.UUID
    as_of: date = Field(description="Today in the practice's time zone: the rules in force")
    show_rates: bool = Field(description="False without `rate.read`: rule rates are null")
    jobs: list[JobRateRead]
