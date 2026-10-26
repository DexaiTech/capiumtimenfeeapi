"""Request and response models of a person's own permissions (on top of their role)."""

import uuid

from pydantic import BaseModel, Field

from app.schemas.common import RequestModel
from app.schemas.organization import RoleSummary


class MemberPermissionsRead(BaseModel):
    user_id: uuid.UUID
    role: RoleSummary
    role_permissions: list[str] = Field(description="What the person's role gives them")
    grants: list[str] = Field(
        description="Permissions given to this person that their role does not give"
    )
    denies: list[str] = Field(
        description="Permissions taken from this person that their role would give"
    )
    effective: list[str] = Field(
        description="What the person can do: the role's, plus the grants, less the denials"
    )


class MemberPermissionsUpdate(RequestModel):
    grants: list[str] = Field(
        default_factory=list,
        max_length=200,
        description=(
            "Permission codes to give this person on top of their role. A code the role already "
            "gives is left out. You can grant only what you hold yourself"
        ),
    )
    denies: list[str] = Field(
        default_factory=list,
        max_length=200,
        description=(
            "Permission codes to take from this person although their role gives them. A code the "
            "role does not give is left out. A code cannot be both granted and denied"
        ),
    )
