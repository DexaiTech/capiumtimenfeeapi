"""Pure rules of jobs: which statuses are open, the status labels, and the arithmetic of job
budgets (usage in basis points, the RAG band and the alert thresholds).

Usage is ``actual / budget`` in basis points (10000 = 100%), rounded half up like the job's own
``budget_minutes_used_bp`` and ``budget_fee_used_bp``. A job with a budget in hours and one in
fees is as used as the worse of the two.
"""

from typing import Literal

from app.utils.money import percentage_bp

Rag = Literal["green", "amber", "red"]

# Time can be logged and budget alerts are raised on these; the others are finished or on hold.
OPEN_JOB_STATUSES = ("planned", "in_progress", "on_hold")
# The words the spec uses (``planned`` is "Not started"); the stored values never change.
JOB_STATUS_LABELS = {
    "planned": "Not started",
    "in_progress": "In progress",
    "on_hold": "On hold",
    "completed": "Completed",
    "closed": "Closed",
    "cancelled": "Cancelled",
}
TASK_STATUS_LABELS = {
    "not_started": "Not started",
    "in_progress": "In progress",
    "on_hold": "On hold",
    "completed": "Completed",
}

# Per cent of the budget at which people are told.
ALERT_THRESHOLDS = (80, 100)
ALERT_KINDS = ("minutes", "fee")
AMBER_BP = 8_000
RED_BP = 10_000


def usage_bp(actual: int, budget: int | None) -> int | None:
    """``actual / budget`` in basis points; None without a budget (zero is no budget)."""
    return percentage_bp(actual, budget) if budget else None


def worst(*values: int | None) -> int | None:
    """The highest of the usages that exist."""
    present = [v for v in values if v is not None]
    return max(present) if present else None


def rag_of(used_bp: int | None) -> Rag | None:
    """Green below 80%, amber from 80% to just under 100%, red from 100%; None without budget."""
    if used_bp is None:
        return None
    if used_bp >= RED_BP:
        return "red"
    if used_bp >= AMBER_BP:
        return "amber"
    return "green"
