"""Request and response models of expenses: types, claims with their lines, receipts, the
approval queue and the summary (docs/modules/expenses.md)."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from app.db.models import ExpenseType
from app.schemas.billing import BillingStatus, ClientRef, JobRef
from app.schemas.common import RequestModel
from app.schemas.settings import SettingsCode, blank_is_null, reject_nulls, upper_code

ClaimStatus = Literal["draft", "submitted", "approved", "returned"]
Payer = Literal["staff", "practice"]
ClaimEventAction = Literal["submitted", "recalled", "approved", "returned", "corrected"]
ClaimSort = Literal["created_at", "submitted_at", "total", "user"]
LineSort = Literal["incurred_on", "gross", "created_at"]
ReceiptSort = Literal["created_at", "incurred_on", "gross"]
TypeSort = Literal["name", "code"]

Money = Annotated[int, Field(ge=0, le=10**12, description="Minor units (pence)")]
Miles = Annotated[
    Decimal,
    Field(
        gt=0,
        lt=1_000_000,
        max_digits=8,
        decimal_places=2,
        description="Miles, below 1,000,000, at most two decimal places",
    ),
]


# --- Expense types ----------------------------------------------------------------------


class ExpenseTypeRead(BaseModel):
    id: uuid.UUID
    name: str
    code: str
    default_vat_rate_id: uuid.UUID | None
    is_mileage: bool
    default_billable: bool
    is_active: bool
    # True once an expense line uses the type: it can then only be deactivated.
    is_used: bool
    can_delete: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, row: ExpenseType, *, is_used: bool) -> "ExpenseTypeRead":
        return cls(
            id=row.id,
            name=row.name,
            code=row.code,
            default_vat_rate_id=row.default_vat_rate_id,
            is_mileage=row.is_mileage,
            default_billable=row.default_billable,
            is_active=row.is_active,
            is_used=is_used,
            can_delete=not is_used,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class ExpenseTypeCreate(RequestModel):
    name: str = Field(min_length=1, max_length=80)
    code: SettingsCode
    default_vat_rate_id: uuid.UUID | None = Field(
        default=None, description="The VAT code of a line that gives only its net amount"
    )
    is_mileage: bool = False
    default_billable: bool = False
    is_active: bool = True

    _code = field_validator("code")(upper_code)


class ExpenseTypeUpdate(RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    code: SettingsCode | None = None
    default_vat_rate_id: uuid.UUID | None = None
    is_mileage: bool | None = None
    default_billable: bool | None = None
    is_active: bool | None = None

    _code = field_validator("code")(upper_code)

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        reject_nulls(self, "name", "code", "is_mileage", "default_billable", "is_active")
        return self


class TypeRef(BaseModel):
    id: uuid.UUID
    name: str
    code: str
    is_mileage: bool

    @classmethod
    def from_model(cls, row: ExpenseType) -> "TypeRef":
        return cls(id=row.id, name=row.name, code=row.code, is_mileage=row.is_mileage)


# --- Lines ------------------------------------------------------------------------------


class ExpenseLineCreate(RequestModel):
    expense_type_id: uuid.UUID
    incurred_on: date = Field(description="The day it was spent; not in the future")
    description: str = Field(min_length=1, max_length=500)
    client_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = Field(default=None, description="One of the client's open jobs")
    supplier: str | None = Field(default=None, max_length=200)
    vat_rate_id: uuid.UUID | None = Field(
        default=None, description="VAT code; with only a net amount it prices the VAT"
    )
    net: Money | None = None
    vat: Money | None = None
    gross: Money | None = None
    miles: Miles | None = Field(default=None, description="Mileage types only")
    is_billable: bool | None = Field(
        default=None, description="Default: the type's, and false without a client"
    )
    recharge_amount: Money | None = Field(
        default=None, description="Override of the recharge (needs `expense.manage`)"
    )
    payer: Payer = "staff"

    _supplier = field_validator("supplier")(blank_is_null)


class ExpenseLineUpdate(RequestModel):
    expense_type_id: uuid.UUID | None = None
    incurred_on: date | None = None
    description: str | None = Field(default=None, min_length=1, max_length=500)
    client_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    supplier: str | None = Field(default=None, max_length=200)
    vat_rate_id: uuid.UUID | None = None
    net: Money | None = None
    vat: Money | None = None
    gross: Money | None = None
    miles: Miles | None = None
    is_billable: bool | None = None
    recharge_amount: Money | None = Field(
        default=None, description="Override of the recharge; null recalculates it"
    )
    payer: Payer | None = None
    reason: str | None = Field(
        default=None,
        max_length=500,
        description="Why an approved expense is changed (required then)",
    )

    _supplier = field_validator("supplier")(blank_is_null)
    _reason = field_validator("reason")(blank_is_null)

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        reject_nulls(
            self,
            "expense_type_id",
            "incurred_on",
            "description",
            "net",
            "vat",
            "gross",
            "miles",
            "is_billable",
            "payer",
        )
        return self


class ReceiptRead(BaseModel):
    id: uuid.UUID
    expense_id: uuid.UUID
    file_name: str
    content_type: str
    size_bytes: int
    is_image: bool
    uploaded_by: uuid.UUID
    uploaded_by_name: str | None
    created_at: datetime


class ExpenseLineRead(BaseModel):
    id: uuid.UUID
    claim_id: uuid.UUID
    expense_type_id: uuid.UUID
    expense_type: TypeRef
    incurred_on: date
    description: str
    client_id: uuid.UUID | None
    client: ClientRef | None
    job_id: uuid.UUID | None
    job: JobRef | None
    supplier: str | None
    vat_rate_id: uuid.UUID | None
    vat_rate_bp: int | None
    net: int
    vat: int
    gross: int
    miles: Decimal | None
    mileage_rate_applied: int | None = Field(description="Pence per mile the net was priced at")
    is_billable: bool
    markup_bp: int
    recharge_amount: int
    payer: Payer
    billing_status: BillingStatus
    receipt_required: bool = Field(
        description="Above the practice's receipt threshold (never for mileage)"
    )
    receipt_count: int
    receipts: list[ReceiptRead]
    created_at: datetime
    updated_at: datetime


class ExpenseLineListItem(ExpenseLineRead):
    """A line in the finance views, with the claim it belongs to."""

    claim_status: ClaimStatus
    claim_title: str | None
    user_id: uuid.UUID
    user_name: str | None


# --- Claims -----------------------------------------------------------------------------


class ClaimTotals(BaseModel):
    net: int
    vat: int
    gross: int
    recharge: int = Field(description="What the billable lines charge the clients")
    reimbursable: int = Field(description="Gross of the lines the staff member paid")


class ClaimActions(BaseModel):
    """What the caller may do with the claim right now (the UI shows or hides buttons by it)."""

    can_edit: bool
    can_delete: bool
    can_submit: bool
    can_recall: bool
    can_approve: bool
    can_return: bool
    can_correct: bool = Field(description="Change an approved claim's lines (with a reason)")


class ClaimEventRead(BaseModel):
    id: uuid.UUID
    action: ClaimEventAction
    actor_user_id: uuid.UUID
    actor_name: str | None
    note: str | None
    created_at: datetime


class ExpenseClaimRead(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    user_name: str | None
    created_by: uuid.UUID
    created_by_name: str | None
    title: str | None
    status: ClaimStatus
    submitted_at: datetime | None
    decided_at: datetime | None
    decided_by: uuid.UUID | None
    decided_by_name: str | None
    decision_note: str | None
    line_count: int
    first_date: date | None = Field(description="Earliest line date")
    last_date: date | None = Field(description="Latest line date")
    totals: ClaimTotals
    actions: ClaimActions
    created_at: datetime
    updated_at: datetime


class ExpenseClaimDetail(ExpenseClaimRead):
    lines: list[ExpenseLineRead]
    events: list[ClaimEventRead]


class ExpenseClaimCreate(RequestModel):
    title: str | None = Field(default=None, max_length=200)
    user_id: uuid.UUID | None = Field(
        default=None,
        description="The claimant; default the caller (someone else: `expense.manage`)",
    )
    lines: list[ExpenseLineCreate] = Field(default_factory=list, max_length=100)

    _title = field_validator("title")(blank_is_null)


class ExpenseClaimUpdate(RequestModel):
    title: str | None = Field(default=None, max_length=200)

    _title = field_validator("title")(blank_is_null)


class ClaimApprove(RequestModel):
    note: str | None = Field(default=None, max_length=2000)

    _note = field_validator("note")(blank_is_null)


class ClaimReturn(RequestModel):
    reason: str | None = Field(default=None, max_length=2000, description="Required")


class ExpenseBulkApprove(RequestModel):
    claim_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


class ClaimStatusCounts(BaseModel):
    draft: int
    submitted: int
    approved: int
    returned: int
    total: int


class ApprovalBadge(BaseModel):
    count: int
    gross: int


class ExpenseSummary(BaseModel):
    counts: ClaimStatusCounts
    pending_gross: int = Field(description="Gross of the visible claims waiting for approval")
    awaiting_my_approval: ApprovalBadge = Field(
        description="The claims in the caller's approval queue (zero without `expense.approve`)"
    )


# --- Receipts ---------------------------------------------------------------------------


class ReceiptListItem(ReceiptRead):
    """A receipt in the Receipts page, with the line and claim it proves."""

    claim_id: uuid.UUID
    claim_status: ClaimStatus
    claim_title: str | None
    user_id: uuid.UUID
    user_name: str | None
    description: str
    incurred_on: date
    gross: int
    client: ClientRef | None


__all__ = [
    "ApprovalBadge",
    "ClaimActions",
    "ClaimApprove",
    "ClaimEventRead",
    "ClaimReturn",
    "ClaimSort",
    "ClaimStatus",
    "ClaimStatusCounts",
    "ClaimTotals",
    "ExpenseBulkApprove",
    "ExpenseClaimCreate",
    "ExpenseClaimDetail",
    "ExpenseClaimRead",
    "ExpenseClaimUpdate",
    "ExpenseLineCreate",
    "ExpenseLineListItem",
    "ExpenseLineRead",
    "ExpenseLineUpdate",
    "ExpenseSummary",
    "ExpenseTypeCreate",
    "ExpenseTypeRead",
    "ExpenseTypeUpdate",
    "LineSort",
    "Payer",
    "ReceiptListItem",
    "ReceiptRead",
    "ReceiptSort",
    "TypeRef",
    "TypeSort",
]
