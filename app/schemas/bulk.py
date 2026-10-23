"""What the bulk endpoints of jobs, clients and invoices share (tickets have their own).

A bulk body is ``{<noun>_ids: [1-200 ids], action, value?}``; the answer says how many rows
were selected (``affected``) and how many of them actually changed (``changed``).
"""

import uuid
from datetime import date

from pydantic import BaseModel, Field

MAX_BULK_IDS = 200


class BulkChangeResult(BaseModel):
    action: str
    affected: int = Field(description="Distinct rows the action was applied to (the selection)")
    changed: int = Field(
        description="Of those, the rows that actually changed (one already in the wanted state "
        "does not count)"
    )


def need_value(value: str | None) -> str:
    """The ``value`` an action needs (ValueError, shown as a 422 on ``value``, when missing)."""
    if value is None:
        raise ValueError("value: This field is required for this action.")
    return value


def parse_user_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise ValueError("value: Use the id of a staff member, or null to clear it.") from exc


def parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("value: Use a date as YYYY-MM-DD, or null to clear it.") from exc


def unique_ids(ids: list[uuid.UUID]) -> list[uuid.UUID]:
    """The ids once each, in the order sent."""
    return list(dict.fromkeys(ids))
