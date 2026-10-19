"""Emailing client statements and the statements list."""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

from app.schemas.billing import ClientRef
from app.schemas.common import RequestModel

StatementSort = Literal["client", "balance", "overdue", "last_sent"]
SortOrder = Literal["asc", "desc"]


class StatementSendRequest(RequestModel):
    date_from: date | None = Field(
        default=None, description="Start of the statement (inclusive); default: the whole account"
    )
    date_to: date | None = Field(
        default=None, description="End of the statement (inclusive); default today"
    )
    to: list[EmailStr] | None = Field(
        default=None,
        min_length=1,
        max_length=10,
        description=(
            "Default: the client's invoice email, else its billing contacts, else its primary "
            "contact, else the client's own email"
        ),
    )


class StatementSendRead(BaseModel):
    id: uuid.UUID
    client_id: uuid.UUID
    to: list[str]
    date_from: date | None
    date_to: date
    closing_balance: int = Field(description="What the client owed when it was queued (pence)")
    queued: bool = Field(
        description="False when the client already had a statement queued in the last ten "
        "minutes: `id` is that send"
    )


class StatementRow(BaseModel):
    client: ClientRef
    partner_user_id: uuid.UUID | None
    manager_user_id: uuid.UUID | None
    outstanding: int = Field(description="Owed on issued invoices")
    on_account: int = Field(description="Cash received and not allocated")
    balance: int = Field(description="`outstanding - on_account`: the statement's closing balance")
    overdue: int = Field(description="The part of `outstanding` past its due date")
    oldest_overdue_due_date: date | None
    oldest_overdue_days: int | None
    last_statement_sent_at: datetime | None
    last_statement_to: list[str] | None
    has_recipient: bool = Field(description="The client has an address a statement can go to")


class StatementTotals(BaseModel):
    client_count: int
    outstanding: int
    overdue: int
    on_account: int
    balance: int


class StatementsRead(BaseModel):
    totals: StatementTotals = Field(description="Of every matching client, not only this page")
    rows: list[StatementRow]
