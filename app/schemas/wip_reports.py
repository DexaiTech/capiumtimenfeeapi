"""Query and response models of the historic WIP report (`GET /reports/wip` with `group_by`)."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.billing import ClientRef
from app.schemas.common import RequestModel
from app.schemas.wip import WipAges
from app.utils.pagination import DEFAULT_LIMIT, MAX_LIMIT

HistoricGroupBy = Literal["client", "job", "billing_type"]
HistoricFormat = Literal["json", "csv", "xlsx"]
BillingType = Literal["hourly", "fixed", "non_billable"]
DateType = date


class HistoricWipQuery(RequestModel):
    as_at: date | None = Field(
        None,
        description="WIP as at the end of this day. Default: today in the practice time zone. "
        "A date after today is 422; a closed day is frozen the first time it is reported",
    )
    group_by: HistoricGroupBy | None = Field(
        None,
        description="`client`, `job` or `billing_type`. Omitted: the original report, one row per "
        "client and job with the original columns (time value, disbursements, total, ages)",
    )
    client_id: uuid.UUID | None = Field(None, description="Only this client")
    job_id: uuid.UUID | None = Field(None, description="Only this job")
    user_id: uuid.UUID | None = Field(
        None, description="Only this person's time (the other items are left out)"
    )
    partner_user_id: uuid.UUID | None = Field(
        None,
        description="Only work whose responsible partner (job's, else client's) is this person",
    )
    manager_user_id: uuid.UUID | None = Field(
        None,
        description="Only work whose responsible manager (job's, else client's) is this person",
    )
    service_id: uuid.UUID | None = Field(None, description="Only jobs of this service")
    format: HistoricFormat = Field(
        "json", description="`json` (default), `csv` (UTF-8 with BOM) or `xlsx` (Excel workbook)"
    )


class HistoricWipItemsQuery(RequestModel):
    as_at: date | None = Field(None, description="As for the report. Default: today")
    client_id: uuid.UUID | None = Field(None, description="Only this client")
    job_id: uuid.UUID | None = Field(None, description="Only this job")
    billing_type: BillingType | None = Field(None, description="Only jobs with this billing type")
    kind: list[Literal["time", "fixed_fee", "expense", "disbursement", "adjustment"]] | None = (
        Field(None, description="Only these kinds of item (repeat the parameter)")
    )
    partner_user_id: uuid.UUID | None = Field(None, description="Responsible partner")
    manager_user_id: uuid.UUID | None = Field(None, description="Responsible manager")
    service_id: uuid.UUID | None = Field(None, description="Only jobs of this service")
    # The model forbids unknown query parameters, so the paging ones are declared here (a
    # separate page dependency would see `limit` and `cursor` as unknown).
    limit: int = Field(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Page size")
    cursor: str | None = Field(None, max_length=500, description="Cursor from meta.next_cursor")


class HistoricWipAmounts(BaseModel):
    minutes: int
    hours: Decimal
    time_wip: int = Field(description="Time WIP as at the day")
    fixed_fee_wip: int = Field(description="Fixed fee WIP as at the day")
    expense_wip: int = Field(description="Expense WIP (expense lines and disbursements)")
    write_ons: int = Field(
        description="Write-ons posted to the ledger up to the day, net of reversals (pence)"
    )
    write_offs: int = Field(
        description="Write-offs posted up to the day, net of reversals, as a positive number"
    )
    total: int = Field(description="Total WIP = time + fixed fee + expense WIP")
    ages: WipAges
    oldest_date: date | None
    item_count: int


class HistoricWipRow(HistoricWipAmounts):
    group_by: HistoricGroupBy
    key: str | None = Field(description="Client id, job id or billing type; null for 'no job'")
    name: str
    code: str | None = Field(description="Client code or job number")
    client: ClientRef | None = Field(description="The job's client (group_by=job)")
    billing_type: str | None = Field(description="Billing type of the job (group_by=job)")


class HistoricWipFilters(BaseModel):
    as_at: date
    group_by: HistoricGroupBy
    client_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    partner_user_id: uuid.UUID | None = None
    manager_user_id: uuid.UUID | None = None
    service_id: uuid.UUID | None = None


class SnapshotInfo(BaseModel):
    frozen: bool = Field(
        description="True for a closed day: its figures were stored when first reported and "
        "never change. False for today, which is computed live"
    )
    taken_at: datetime | None = Field(description="When the day was frozen")
    source: str | None = Field(description="`request` (first reported) or `scheduled`")


class HistoricWipReport(BaseModel):
    rows: list[HistoricWipRow]
    totals: HistoricWipAmounts
    filters: HistoricWipFilters
    snapshot: SnapshotInfo
    notice: str = Field(
        description="History is immutable: figures of a closed day do not change after later "
        "invoices, write-offs or rate changes"
    )


class HistoricWipItem(BaseModel):
    kind: Literal["time", "fixed_fee", "expense", "disbursement", "adjustment"]
    id: uuid.UUID
    component: Literal["time", "fixed_fee", "expense"]
    date: DateType
    age_days: int
    client: ClientRef
    job_id: uuid.UUID | None
    job_number: str | None
    job_name: str | None
    billing_type: str | None
    description: str | None
    minutes: int
    value: int
    charge_value: int | None
