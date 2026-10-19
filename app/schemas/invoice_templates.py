"""Request and response models of invoice templates."""

import uuid
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from app.db.models import InvoiceTemplate
from app.schemas.common import RequestModel
from app.schemas.settings import blank_is_null, reject_nulls

InvoiceLayout = Literal["standard", "detailed", "summary"]
TemplateStatus = Literal["active", "inactive"]


class InvoiceTemplateCreate(RequestModel):
    name: str = Field(
        min_length=1, max_length=80, description="Unique in the practice, ignoring case"
    )
    layout: InvoiceLayout = Field(
        default="standard",
        description="`standard`, `detailed` (every time entry) or `summary` (one line per job)",
    )
    show_logo: bool = Field(default=True, description="Print the practice logo")
    show_bank_details: bool = Field(default=True, description="Print the practice bank details")
    footer_text: str | None = Field(
        default=None, max_length=1000, description="Printed at the foot of the invoice"
    )
    is_default: bool = Field(
        default=False, description="Make it the practice default (the previous default is unset)"
    )
    status: TemplateStatus = Field(default="active", description="`active` or `inactive`")

    _blank = field_validator("footer_text")(blank_is_null)

    @model_validator(mode="after")
    def _default_is_active(self) -> Self:
        if self.is_default and self.status != "active":
            raise ValueError("status: The default template must be active")
        return self


class InvoiceTemplateUpdate(RequestModel):
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        description="Unique in the practice, ignoring case",
    )
    layout: InvoiceLayout | None = Field(
        default=None, description="`standard`, `detailed` or `summary`"
    )
    show_logo: bool | None = Field(default=None, description="Print the practice logo")
    show_bank_details: bool | None = Field(
        default=None, description="Print the practice bank details"
    )
    footer_text: str | None = Field(
        default=None, max_length=1000, description="Null or blank clears it"
    )
    status: TemplateStatus | None = Field(
        default=None,
        description="`inactive` hides it from pickers; the default template cannot be deactivated",
    )

    _blank = field_validator("footer_text")(blank_is_null)

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        reject_nulls(self, "name", "layout", "show_logo", "show_bank_details", "status")
        return self


class InvoiceTemplateRead(BaseModel):
    id: uuid.UUID = Field(description="The template's id")
    name: str = Field(description="Unique in the practice, ignoring case")
    layout: InvoiceLayout = Field(description="`standard`, `detailed` or `summary`")
    show_logo: bool = Field(description="Print the practice logo")
    show_bank_details: bool = Field(description="Print the practice bank details")
    footer_text: str | None = Field(description="Printed at the foot of the invoice")
    is_default: bool = Field(description="The template of clients that have not chosen one")
    status: TemplateStatus = Field(description="`active` or `inactive` (hidden from pickers)")
    client_count: int = Field(description="Clients of the practice that use the template")
    created_at: datetime = Field(description="When the template was created")
    updated_at: datetime = Field(description="When the template was last changed")

    @classmethod
    def from_model(cls, template: InvoiceTemplate, client_count: int) -> "InvoiceTemplateRead":
        return cls.model_validate(
            {
                "id": template.id,
                "name": template.name,
                "layout": template.layout,
                "show_logo": template.show_logo,
                "show_bank_details": template.show_bank_details,
                "footer_text": template.footer_text,
                "is_default": template.is_default,
                "status": template.status,
                "client_count": client_count,
                "created_at": template.created_at,
                "updated_at": template.updated_at,
            }
        )
