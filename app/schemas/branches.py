"""Request and response models of the practice's branches (ticket APT74)."""

import uuid
from datetime import datetime
from typing import Annotated, Self

from pydantic import BaseModel, Field, model_validator

from app.db.models import Branch
from app.schemas.common import RequestModel
from app.schemas.settings import reject_nulls

BranchName = Annotated[str, Field(min_length=1, max_length=40)]


class BranchRead(BaseModel):
    id: uuid.UUID
    name: str
    is_active: bool
    # Live clients in the branch; null for a member without ``client.read``.
    client_count: int | None
    # False once clients have the branch: DELETE then deactivates it instead.
    can_delete: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(
        cls, branch: Branch, *, client_count: int, show_count: bool = True
    ) -> "BranchRead":
        return cls(
            id=branch.id,
            name=branch.name,
            is_active=branch.is_active,
            client_count=client_count if show_count else None,
            can_delete=client_count == 0,
            created_at=branch.created_at,
            updated_at=branch.updated_at,
        )


class BranchRef(BaseModel):
    """The branch a client belongs to, as the client list and overview show it."""

    id: uuid.UUID
    name: str


class BranchCreate(RequestModel):
    name: BranchName
    is_active: bool = True


class BranchUpdate(RequestModel):
    """Every field optional."""

    name: BranchName | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        reject_nulls(self, "name", "is_active")
        return self
