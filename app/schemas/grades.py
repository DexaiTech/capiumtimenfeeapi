"""Request and response models of staff grades."""

import uuid
from datetime import datetime
from typing import Annotated, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from app.db.models import Grade
from app.schemas.common import RequestModel
from app.schemas.settings import (
    Money,
    RateBp,
    SettingsCode,
    blank_is_null,
    reject_nulls,
    upper_code,
)

GradeName = Annotated[str, Field(min_length=1, max_length=80)]
GradeDescription = Annotated[str, Field(max_length=500)]


class GradeRead(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    description: str | None
    # Pence per hour. Null when the grade sets none, and for a caller without `rate.read`.
    default_cost_rate: int | None
    default_charge_rate: int | None
    # Basis points: 7500 = 75%. The starting chargeable target of staff given the grade.
    default_chargeable_target_bp: int | None
    is_active: bool
    sort_order: int
    # Staff who have not left holding the grade.
    staff_count: int
    # False once staff (whatever their status) or rates refer to the grade: DELETE then
    # deactivates it instead.
    can_delete: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(
        cls, grade: Grade, *, staff_count: int, can_delete: bool, show_cost: bool
    ) -> "GradeRead":
        return cls(
            id=grade.id,
            code=grade.code,
            name=grade.name,
            description=grade.description,
            default_cost_rate=grade.default_cost_rate if show_cost else None,
            default_charge_rate=grade.default_charge_rate,
            default_chargeable_target_bp=grade.default_chargeable_target_bp,
            is_active=grade.is_active,
            sort_order=grade.sort_order,
            staff_count=staff_count,
            can_delete=can_delete,
            created_at=grade.created_at,
            updated_at=grade.updated_at,
        )


class GradeCreate(RequestModel):
    code: SettingsCode
    name: GradeName
    description: GradeDescription | None = None
    default_cost_rate: Money | None = Field(default=None, description="Pence per hour")
    default_charge_rate: Money | None = Field(default=None, description="Pence per hour")
    default_chargeable_target_bp: RateBp | None = Field(
        default=None, description="Basis points: 7500 = 75%"
    )
    sort_order: int = Field(default=0, ge=0, le=10_000)
    is_active: bool = True

    _code = field_validator("code")(upper_code)
    _description = field_validator("description")(blank_is_null)


class GradeUpdate(RequestModel):
    """Every field optional. `description` and the three defaults accept null to clear them."""

    code: SettingsCode | None = None
    name: GradeName | None = None
    description: GradeDescription | None = None
    default_cost_rate: Money | None = None
    default_charge_rate: Money | None = None
    default_chargeable_target_bp: RateBp | None = None
    sort_order: int | None = Field(default=None, ge=0, le=10_000)
    is_active: bool | None = None

    _code = field_validator("code")(upper_code)
    _description = field_validator("description")(blank_is_null)

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        reject_nulls(self, "code", "name", "sort_order", "is_active")
        return self
