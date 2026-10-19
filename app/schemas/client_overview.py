"""The Client 360 overview: the figures behind the tabs of one client."""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class JobsOverview(BaseModel):
    total: int = Field(description="Jobs of the client that are not deleted")
    by_status: dict[str, int] = Field(
        description=(
            "`not_started`, `in_progress`, `on_hold`, `completed` and `closed`, always all five "
            "(a planned job counts as not started, a cancelled one as closed)"
        )
    )


class TimeOverview(BaseModel):
    scope: Literal["all", "own"] = Field(
        description=(
            "`own`: the caller may see only their own time (`time.log` without `time.read_all`)"
        )
    )
    minutes_30d: int = Field(description="Time on the 30 days up to and including today")
    billable_minutes_30d: int = Field(description="Billable minutes of `minutes_30d`")
    minutes_ytd: int = Field(description="Time since 1 January of this year")
    billable_minutes_ytd: int = Field(description="Billable minutes of `minutes_ytd`")
    last_entry_on: date | None = Field(description="Date of the latest time entry")


class ExpensesOverview(BaseModel):
    scope: Literal["all", "own"] = Field(
        description="`own`: without `wip.read` the caller sees the disbursements they recorded"
    )
    count: int = Field(description="Disbursements of the client")
    net: int = Field(description="Pence, before VAT")
    gross: int = Field(
        description="Pence, with VAT at the disbursement's, else the client's, else the "
        "practice's rate (none when the practice is not VAT registered)"
    )
    awaiting_approval: int = Field(
        description="Disbursements have no approval step yet: always 0 until the Expenses "
        "module adds one"
    )


class WipAgeing(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    days_0_30: int = Field(alias="0_30", description="Items up to 30 days old")
    days_31_60: int = Field(alias="31_60", description="Items 31 to 60 days old")
    days_61_90: int = Field(alias="61_90", description="Items 61 to 90 days old")
    days_90_plus: int = Field(alias="90_plus", description="Older than 90 days")


class WipOverview(BaseModel):
    time: int = Field(description="Pence: unbilled billable time, except on fixed-fee jobs")
    fixed_fee: int = Field(description="Pence: unbilled billable time on fixed-fee jobs")
    expense: int = Field(description="Pence: unbilled billable disbursements")
    total: int = Field(description="time + fixed_fee + expense; the figure of the WIP screens")
    ageing: WipAgeing = Field(description="Total WIP by the age of its items (days)")
    item_count: int = Field(description="Time entries and disbursements in WIP")


class InvoicesOverview(BaseModel):
    count: int = Field(description="Every invoice of the client, drafts and void ones included")
    by_status: dict[str, int] = Field(
        description=(
            "`draft`, `issued`, `overdue`, `part_paid`, `paid`, `void`, `awaiting_approval`, "
            "`approved` and `credited`, always all nine"
        )
    )
    billed_ytd: int = Field(
        description="Pence, net of VAT, of the invoices issued since 1 January of this year"
    )
    outstanding: int = Field(description="Pence still owed on issued invoices")
    overdue: int = Field(description="Pence of `outstanding` that is past its due date")
    last_invoice_on: date | None = Field(description="Issue date of the latest issued invoice")


class PaymentsOverview(BaseModel):
    received_ytd: int = Field(description="Pence received since 1 January of this year")
    count_ytd: int = Field(description="Payments received since 1 January of this year")
    last_payment_on: date | None = Field(description="Date of the latest payment received")


class ActivityEvent(BaseModel):
    id: uuid.UUID = Field(description="The audit entry")
    action: str = Field(description="The audit action, e.g. `client.updated`")
    label: str = Field(description="The action in words")
    at: datetime = Field(description="When it happened")
    actor_user_id: uuid.UUID | None = Field(description="Null: the system (central events)")
    actor_name: str | None = Field(description="The person's name, when there is one")
    target_type: str | None = Field(description="The kind of record it was about")
    target_id: uuid.UUID | None = Field(description="The record it was about")


class TabCounts(BaseModel):
    jobs: int | None = Field(description="Jobs; null without `job.read`")
    time_entries: int | None = Field(
        description="Time entries the caller may see; null without time permissions"
    )
    expenses: int | None = Field(
        description="Disbursements the caller may see; null without access"
    )
    wip: int | None = Field(description="Items in WIP; null without `wip.read`")
    invoices: int | None = Field(description="Invoices; null without `invoice.read`")
    payments: int | None = Field(description="Payments; null without `payment.read`")
    contacts: int = Field(description="Needs only `client.read`")


class ClientOverview(BaseModel):
    """Every section the caller may not see is null."""

    client_id: uuid.UUID = Field(description="The client the figures are about")
    as_of: date = Field(description="Today in the practice's time zone")
    ytd_from: date = Field(description="1 January of this year: where the year to date starts")
    last_30d_from: date = Field(description="First day of the 30 days up to today")
    jobs: JobsOverview | None = Field(description="Needs `job.read`")
    time: TimeOverview | None = Field(description="Needs `time.read_all` or `time.log`")
    expenses: ExpensesOverview | None = Field(
        description="Needs `wip.read`, `disbursement.log` or `disbursement.manage`"
    )
    wip: WipOverview | None = Field(description="Needs `wip.read`")
    invoices: InvoicesOverview | None = Field(description="Needs `invoice.read`")
    payments: PaymentsOverview | None = Field(description="Needs `payment.read`")
    recent_activity: list[ActivityEvent] = Field(
        description="The last 10 audit events about the client (and clients merged into it) "
        "that the caller's permissions allow"
    )
    tab_counts: TabCounts = Field(description="Counts for the tab badges")
