"""The arithmetic of the recovery analysis (``app/services/recovery.py``), shared with the billing
summary of the invoicing screen: pieces of time valued on every basis, their sums, the cost-plus
markup, the potentials and the figures against the invoice value, and the staff-rate value.
"""

import uuid
from collections import defaultdict
from collections.abc import Callable, Hashable, Iterable, Sequence
from dataclasses import dataclass, fields
from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Organization
from app.services.audit_catalogue import COST_PERMISSION
from app.services.charge_rates import RateQuery, resolve_many
from app.utils.money import BASIS_POINTS, percentage_bp, round_half_up, time_value

__all__ = [
    "COST_PERMISSION",
    "Piece",
    "Tally",
    "figures",
    "potentials",
    "staff_values",
    "tally_by",
    "with_markup",
]


@dataclass(frozen=True, slots=True)
class Piece:
    """Billable time of one job, client, person, day and billing status, on every basis."""

    job_id: uuid.UUID | None
    client_id: uuid.UUID | None
    user_id: uuid.UUID
    minutes: int
    task_value: int
    staff_value: int
    cost_value: int
    invoiced_value: int
    to_bill_value: int


@dataclass(slots=True)
class Tally:
    """The sum of some pieces."""

    minutes: int = 0
    task_value: int = 0
    staff_value: int = 0
    cost_value: int = 0
    invoiced_value: int = 0
    to_bill_value: int = 0

    def add(self, piece: "Piece | Tally") -> None:
        for field in fields(Tally):
            name = field.name
            setattr(self, name, getattr(self, name) + getattr(piece, name))

    @classmethod
    def of(cls, pieces: Iterable["Piece | Tally"]) -> "Tally":
        tally = cls()
        for piece in pieces:
            tally.add(piece)
        return tally


def with_markup(cost: int, markup_bp: int) -> int:
    """``cost`` plus ``markup_bp`` of it, rounded half up: 5000 (50%) on 50.00 is 75.00."""
    return round_half_up(Decimal(cost) * (BASIS_POINTS + markup_bp) / BASIS_POINTS)


def potentials(tally: Tally, markup_bp: int, show_cost: bool) -> dict[str, int | None]:
    """The fields of ``RecoveryPotentials``."""
    return {
        "minutes": tally.minutes,
        "task_value": tally.task_value,
        "staff_value": tally.staff_value,
        "cost_value": tally.cost_value if show_cost else None,
        "cost_plus_value": with_markup(tally.cost_value, markup_bp) if show_cost else None,
    }


def figures(tally: Tally, markup_bp: int, show_cost: bool) -> dict[str, int | None]:
    """The fields of ``RecoveryFigures``: the potentials against the invoice value."""
    invoice = tally.invoiced_value + tally.to_bill_value
    cost_plus = with_markup(tally.cost_value, markup_bp)
    margin = invoice - tally.cost_value
    return {
        **potentials(tally, markup_bp, show_cost),
        "invoiced_value": tally.invoiced_value,
        "to_bill_value": tally.to_bill_value,
        "invoice_value": invoice,
        "task_shortfall": tally.task_value - invoice,
        "task_recovery_bp": percentage_bp(invoice, tally.task_value),
        "staff_shortfall": tally.staff_value - invoice,
        "staff_recovery_bp": percentage_bp(invoice, tally.staff_value),
        "cost_plus_shortfall": cost_plus - invoice if show_cost else None,
        "cost_plus_recovery_bp": percentage_bp(invoice, cost_plus) if show_cost else None,
        "margin_value": margin if show_cost else None,
        "margin_bp": percentage_bp(margin, invoice) if show_cost else None,
    }


def tally_by[K: Hashable](pieces: Iterable[Piece], key: Callable[[Piece], K]) -> dict[K, Tally]:
    found: dict[K, Tally] = defaultdict(Tally)
    for piece in pieces:
        found[key(piece)].add(piece)
    return dict(found)


async def staff_values(
    session: AsyncSession,
    organization: Organization,
    work: Sequence[tuple[uuid.UUID, date, bool, int]],
) -> list[int]:
    """``(user_id, day, overtime, minutes)`` valued at the person's own charge-out rate that day:
    the charge-out ladder without a client, service or job, so the staff rate (or, in the
    ``grade_only`` rate mode, the grade's rate), else the grade's, else the practice default."""
    queries = [
        RateQuery(user_id, None, None, day, overtime=overtime) for user_id, day, overtime, _ in work
    ]
    rates = await resolve_many(session, organization, queries)
    return [
        time_value(minutes, rate.charge_rate)
        for (_, _, _, minutes), rate in zip(work, rates, strict=True)
    ]
