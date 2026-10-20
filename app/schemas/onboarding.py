"""Request and response models of the onboarding checklist."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import RequestModel

StepState = Literal["done", "todo", "skipped"]
StepSource = Literal["auto", "manual"]


class OnboardingStepRead(BaseModel):
    key: str = Field(description="Stable identifier of the step")
    title: str = Field(description="Short name of the step")
    description: str = Field(description="What to do, in one or two sentences")
    state: StepState = Field(description="`done`, `todo`, or `skipped`")
    source: StepSource = Field(
        description=(
            "`auto`: the state comes from the practice's data. `manual`: a person marked the "
            "step done or skipped it (or the step is one only a person can complete)"
        )
    )
    detected: bool | None = Field(
        description="What the data says (true: finished); null for a step only a person completes"
    )
    detail: str | None = Field(description="What was found, for example '3 active staff members'")
    link: str = Field(
        description="Where to do it: a path below the practice's URL, e.g. /settings/practice"
    )
    updated_by_name: str | None = Field(description="Who marked the step; null when nobody did")
    updated_at: datetime | None = Field(description="When it was marked")


class OnboardingSummary(BaseModel):
    total: int
    done: int
    skipped: int
    todo: int
    percent: int = Field(ge=0, le=100, description="Steps finished (done or skipped), in percent")
    complete: bool = Field(description="No step is left to do")


class OnboardingRead(BaseModel):
    steps: list[OnboardingStepRead] = Field(description="In the order the practice should work")
    summary: OnboardingSummary


class OnboardingStepUpdate(RequestModel):
    state: StepState = Field(
        description="`done` or `skipped` is a decision; `todo` takes it back, so the data decides"
    )
