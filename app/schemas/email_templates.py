"""Request and response models of email templates."""

import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import RequestModel

_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")


class MergeFieldRead(BaseModel):
    name: str = Field(description="Written in a template as `{{ name }}`")
    label: str
    description: str
    sample: str = Field(description="What a preview shows when no value is given")


class EmailTemplateRead(BaseModel):
    key: str
    name: str
    description: str
    category: Literal["notification", "system"]
    audience: Literal["external", "internal"] = Field(
        description="`external` is sent to the practice's clients, `internal` to its own team"
    )
    notification_type: str | None = Field(
        description="The notification type that sends this template, when there is one"
    )
    merge_fields: list[MergeFieldRead]
    required_fields: list[str] = Field(description="Fields every body must contain")
    customised: bool = Field(description="True when the practice has its own version")
    # The effective template: the practice's version when customised, else the built-in one.
    subject: str
    html_body: str
    text_body: str
    updated_at: datetime | None
    updated_by: uuid.UUID | None
    updated_by_name: str | None


class EmailTemplateWrite(RequestModel):
    subject: str = Field(min_length=1, max_length=200)
    html_body: str = Field(min_length=1, max_length=100_000)
    text_body: str = Field(min_length=1, max_length=50_000)


class EmailTemplatePreviewRequest(RequestModel):
    """A draft to preview (omitted parts are the saved template's) and sample values for its
    merge fields (omitted fields show the field's sample)."""

    subject: str | None = Field(default=None, min_length=1, max_length=200)
    html_body: str | None = Field(default=None, min_length=1, max_length=100_000)
    text_body: str | None = Field(default=None, min_length=1, max_length=50_000)
    values: dict[str, str] = Field(default_factory=dict, max_length=50)

    @field_validator("values")
    @classmethod
    def _values(cls, value: dict[str, str]) -> dict[str, str]:
        for name, text in value.items():
            if not _FIELD_NAME.fullmatch(name):
                raise ValueError(f"{name!r} is not a merge field name.")
            if len(text) > 500:
                raise ValueError(f"The value of {name} is longer than 500 characters.")
        return value


class EmailTemplatePreview(BaseModel):
    subject: str
    html: str = Field(description="The complete HTML email, as it would be sent")
    text: str
    values: dict[str, str] = Field(description="The values used for each merge field")
