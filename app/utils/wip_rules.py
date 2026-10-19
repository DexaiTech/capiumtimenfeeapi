"""Pure rules of work in progress: the age buckets and how much of a fixed fee counts as WIP.

Total WIP = Time WIP + Fixed fee WIP + Expense WIP (docs/modules/wip.md). A fixed fee counts once it
is *recognised*; until then the time on that job counts at its charge value, as a fixed fee job's
WIP (``docs/domain.md``, "Work in progress").
"""

from datetime import date
from decimal import Decimal
from typing import Literal

from app.utils.money import round_half_up

Component = Literal["time", "fixed_fee", "expense"]
COMPONENTS: tuple[Component, ...] = ("time", "fixed_fee", "expense")
AGE_BUCKETS = ("0_30", "31_60", "61_90", "90_plus")
COMPLETED_STATUSES = ("completed", "closed")


def age_days(item_date: date, as_at: date) -> int:
    """Whole days between the item date and the day of the report (never negative)."""
    return max(0, (as_at - item_date).days)


def age_bucket(days: int) -> str:
    """0-30, 31-60, 61-90 and 90+ days (an item 30 days old is still in the first bucket)."""
    if days <= 30:
        return "0_30"
    if days <= 60:
        return "31_60"
    if days <= 90:
        return "61_90"
    return "90_plus"


def recognised_fixed_fee(
    mode: str,
    *,
    fee: int,
    status: str,
    budget_minutes: int | None,
    logged_minutes: int,
) -> int | None:
    """The part of a fixed fee recognised as WIP, in pence, or ``None`` while it is not recognised.

    * ``on_completion``: the whole fee once the job is completed or closed.
    * ``by_budget_progress``: ``fee x min(1, logged minutes / budget minutes)``, half up. A job
      without a budget in hours has no measurable progress and is not recognised.

    The caller subtracts what is already billed.
    """
    if mode == "by_budget_progress":
        if not budget_minutes:
            return None
        used = min(max(logged_minutes, 0), budget_minutes)
        return round_half_up(Decimal(fee) * used / budget_minutes)
    return fee if status in COMPLETED_STATUSES else None


def empty_buckets() -> dict[str, int]:
    return dict.fromkeys(AGE_BUCKETS, 0)
