"""Request and response models of the bookkeeping mappings."""

import uuid
from typing import Annotated

from pydantic import BaseModel, Field

from app.schemas.common import RequestModel

CODE_REGEX = r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,19}$"
BookkeepingCode = Annotated[
    str,
    Field(
        min_length=1,
        max_length=20,
        pattern=CODE_REGEX,
        description="A code of the bookkeeping system: letters, digits and . _ / - (1 to 20)",
    ),
]


class ServiceMappingRead(BaseModel):
    service_id: uuid.UUID | None = Field(
        description="The service; null is the row for invoice lines that have no service"
    )
    code: str | None = Field(description="The service's code (null for the no-service row)")
    name: str | None = Field(description="The service's name (null for the no-service row)")
    is_active: bool | None = Field(
        description="Whether the service is active (null: no-service row)"
    )
    account_code: str | None = Field(description="The income account code; null: not mapped")
    account_name: str | None = Field(description="The income account's name")


class VatMappingRead(BaseModel):
    vat_rate_id: uuid.UUID | None = Field(
        description="The VAT code; null is the row for lines that carry no VAT code"
    )
    code: str | None = Field(description="Kind of VAT code: `standard`, `reduced`, `zero`, ...")
    name: str | None = Field(description="The VAT code's name (null for the no-VAT row)")
    rate_bp: int | None = Field(description="The rate in basis points (2000 = 20%)")
    is_archived: bool | None = Field(description="Whether the VAT code is archived")
    tax_code: str | None = Field(description="The tax code; null: not mapped")
    tax_name: str | None = Field(description="The tax code's name")


class MappingsRead(BaseModel):
    services: list[ServiceMappingRead] = Field(
        description="The no-service row first, then every service of the practice"
    )
    vat_rates: list[VatMappingRead] = Field(
        description="The no-VAT row first, then every VAT code of the practice"
    )
    unmapped_services: int = Field(description="Rows of `services` without an account code")
    unmapped_vat_rates: int = Field(description="Rows of `vat_rates` without a tax code")


class ServiceMappingWrite(RequestModel):
    service_id: uuid.UUID | None = Field(
        description="The service, or null for invoice lines that have no service"
    )
    account_code: BookkeepingCode
    account_name: str | None = Field(
        default=None, min_length=1, max_length=120, description="The account's name, for people"
    )


class VatMappingWrite(RequestModel):
    vat_rate_id: uuid.UUID | None = Field(
        description="The VAT code, or null for lines that carry no VAT code"
    )
    tax_code: BookkeepingCode
    tax_name: str | None = Field(
        default=None, min_length=1, max_length=120, description="The tax code's name, for people"
    )


class MappingsWrite(RequestModel):
    """The complete set of mappings: whatever is not listed is unmapped."""

    services: list[ServiceMappingWrite] = Field(
        max_length=500, description="Income account by service (each service at most once)"
    )
    vat_rates: list[VatMappingWrite] = Field(
        max_length=100, description="Tax code by VAT code (each VAT code at most once)"
    )
