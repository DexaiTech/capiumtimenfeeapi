"""Response model of the Application Testing dashboard."""

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.tickets import TagColor, TicketListItem, TicketPriority, TicketStatus


class Change(BaseModel):
    kind: Literal["new", "percent", "none"] = Field(
        description=(
            "`new`: nothing in the previous period and something now (show `New`); `percent`: "
            "show `percent` signed (`+12%`, `-5%`, `0%`); `none`: nothing in either period"
        )
    )
    percent: int | None = Field(
        description="Whole percent change against the previous period, half away from zero"
    )


class Kpi(BaseModel):
    value: int
    previous: int = Field(description="The same count for the period just before the range")
    change: Change


class Kpis(BaseModel):
    total: Kpi
    open: Kpi
    hold: Kpi
    in_progress: Kpi
    reopen: Kpi
    closed: Kpi


class StatusCount(BaseModel):
    status: TicketStatus
    count: int
    percent: float | None = Field(
        description="Share of all tickets in the range to one decimal; null when there are none"
    )


class PriorityCount(BaseModel):
    priority: TicketPriority
    count: int
    percent: float | None


class TagCount(BaseModel):
    tag_id: uuid.UUID | None = Field(description="Null for the `Others` bucket")
    name: str
    color: TagColor | None = Field(description="Null for the `Others` bucket")
    count: int = Field(
        description=(
            "Tickets carrying the tag (a ticket counts under each of its tags); for `Others`, the "
            "tickets that carry at least one tag outside the seven, each counted once"
        )
    )
    is_other: bool


class TrendPoint(BaseModel):
    date: dt.date
    open: int
    hold: int
    in_progress: int
    reopen: int
    closed: int
    total: int = Field(description="Tickets of the range that existed at the end of the day")


class AssigneeCount(BaseModel):
    user_id: uuid.UUID | None = Field(description="Null for the `Unassigned` row")
    full_name: str
    count: int


class TicketDashboard(BaseModel):
    date_from: dt.date
    date_to: dt.date
    previous_from: dt.date = Field(description="The period of equal length just before the range")
    previous_to: dt.date
    days: int = Field(description="Days in the range, both ends included")
    kpis: Kpis
    by_status: list[StatusCount]
    by_priority: list[PriorityCount]
    by_tag: list[TagCount] = Field(
        description="The seven most used tags, then an `Others` bucket when there are more"
    )
    trend: list[TrendPoint] = Field(description="The 14 days ending on `date_to`, oldest first")
    top_assignees: list[AssigneeCount]
    recent: list[TicketListItem] = Field(description="The seven newest tickets of the range")
