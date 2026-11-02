"""The arithmetic of the recovery analysis (``app/services/recovery.py``), shared with the billing
summary of the invoicing screen and the Cost to Business report: pieces of time valued on every
basis, their sums, the cost-plus mark-up, and the figures against the invoice value.

**The recovery rate** of a piece of work is the first rate that exists, in the practice's order
(``organizations.recovery_rate_order``, default client job → task → staff), of three tiers of the
charge-out ladder (``RateBook.tiers``) on the day it was worked: the **client job-specific rate**
(a rate agreed with the client, else the job's own rate: levels 1-4), the **task rate** (the rate
set against the job's service: level 5) and the **staff charge-out rate** (the person's rate, else
their grade's, else the practice default; it always exists). The **staff rate** potential values
the same work at the third tier alone, whatever the order.
"""

import uuid
from collections import defaultdict
from collections.abc import Callable, Hashable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Organization
from app.services.audit_catalogue import COST_PERMISSION
from app.services.charge_rates import RateBook, RateQuery
from app.utils.money import BASIS_POINTS, percentage_bp, round_half_up, time_value

__all__ = [
    "COST_PERMISSION",
    "RATE_TIERS",
    "SETTLED",
    "Piece",
    "Tally",
    "Work",
    "cost_figures",
    "figures",
    "potentials",
    "tally_by",
    "value_work",
    "with_markup",
]

RATE_TIERS = ("client_job", "task", "staff")
# Billing statuses whose time is on an invoice (issued or draft) or written off: the work the
# invoice value is compared with. Unbilled time is WIP: it has no invoice value yet.
SETTLED = ("billed", "in_draft", "written_off")


@dataclass(frozen=True, slots=True)
class Work:
    """Time to value: who, for whom, which day, and how long."""

    user_id: uuid.UUID
    client_id: uuid.UUID | None
    job_id: uuid.UUID | None
    day: date
    overtime: bool
    minutes: int


@dataclass(frozen=True, slots=True)
class Valued:
    recovery_value: int
    # The tier the recovery rate came from: one of ``RATE_TIERS``.
    tier: str
    staff_value: int


async def value_work(
    session: AsyncSession, organization: Organization, work: Sequence[Work]
) -> list[Valued]:
    """Each piece of work at the recovery rate (the practice's order of the three tiers) and at
    the person's own charge-out rate, in the order given."""
    if not work:
        return []
    queries = [
        RateQuery(w.user_id, w.client_id, w.job_id, w.day, overtime=w.overtime) for w in work
    ]
    book = await RateBook.load(session, organization, queries)
    order = list(organization.recovery_rate_order) or list(RATE_TIERS)
    found: list[Valued] = []
    for w, query in zip(work, queries, strict=True):
        rates = book.tiers(query)
        tier = next(t for t in [*order, "staff"] if rates.get(t) is not None)
        rate, staff = rates[tier], rates["staff"]
        assert rate is not None and staff is not None
        found.append(Valued(time_value(w.minutes, rate), tier, time_value(w.minutes, staff)))
    return found


@dataclass(frozen=True, slots=True)
class Piece:
    """Time of one job, client, person, day, billable flag and billing status, on every basis."""

    job_id: uuid.UUID | None
    client_id: uuid.UUID | None
    user_id: uuid.UUID
    billable: bool
    status: str
    minutes: int
    # At the rates the time was recorded at (its WIP value).
    charge_value: int
    cost_value: int
    # The cost-plus mark-up of the work: the job's, else the client's, else the practice's.
    markup_bp: int
    # For settled billable time only (0 otherwise): the potentials and what its invoices bill.
    recovery_value: int = 0
    tier: str = "staff"
    staff_value: int = 0
    invoiced_value: int = 0
    draft_value: int = 0
    credited_value: int = 0


@dataclass(slots=True)
class Tally:
    """The sum of some pieces, split into the work the invoices are compared with (``minutes``
    ...: billable time on an invoice or written off), billable time not invoiced yet (``wip_*``)
    and non-billable time (``non_billable_*``)."""

    minutes: int = 0
    recovery_value: int = 0
    staff_value: int = 0
    cost_value: int = 0
    invoiced_value: int = 0
    draft_value: int = 0
    credited_value: int = 0
    wip_minutes: int = 0
    wip_value: int = 0
    wip_cost: int = 0
    non_billable_minutes: int = 0
    non_billable_cost: int = 0
    # Cost of the compared work by mark-up (basis points): cost plus is rounded once per mark-up.
    cost_by_markup: dict[int, int] = field(default_factory=dict)
    # Minutes of the compared work by the tier its recovery rate came from.
    tier_minutes: dict[str, int] = field(default_factory=dict)

    def add_valued(
        self,
        *,
        minutes: int,
        recovery_value: int,
        tier: str,
        staff_value: int,
        cost_value: int,
        markup_bp: int,
    ) -> None:
        """Work valued on every basis (settled work, or the work ready to bill of an invoice)."""
        self.minutes += minutes
        self.recovery_value += recovery_value
        self.staff_value += staff_value
        self.cost_value += cost_value
        self.cost_by_markup[markup_bp] = self.cost_by_markup.get(markup_bp, 0) + cost_value
        self.tier_minutes[tier] = self.tier_minutes.get(tier, 0) + minutes

    def add(self, piece: Piece) -> None:
        if not piece.billable:
            self.non_billable_minutes += piece.minutes
            self.non_billable_cost += piece.cost_value
        elif piece.status not in SETTLED:
            self.wip_minutes += piece.minutes
            self.wip_value += piece.charge_value
            self.wip_cost += piece.cost_value
        else:
            self.add_valued(
                minutes=piece.minutes,
                recovery_value=piece.recovery_value,
                tier=piece.tier,
                staff_value=piece.staff_value,
                cost_value=piece.cost_value,
                markup_bp=piece.markup_bp,
            )
            self.invoiced_value += piece.invoiced_value
            self.draft_value += piece.draft_value
            self.credited_value += piece.credited_value

    def merge(self, other: "Tally") -> None:
        for name in (
            "minutes",
            "recovery_value",
            "staff_value",
            "cost_value",
            "invoiced_value",
            "draft_value",
            "credited_value",
            "wip_minutes",
            "wip_value",
            "wip_cost",
            "non_billable_minutes",
            "non_billable_cost",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        for bp, cost in other.cost_by_markup.items():
            self.cost_by_markup[bp] = self.cost_by_markup.get(bp, 0) + cost
        for tier, minutes in other.tier_minutes.items():
            self.tier_minutes[tier] = self.tier_minutes.get(tier, 0) + minutes

    @classmethod
    def of(cls, items: Iterable["Piece | Tally"]) -> "Tally":
        tally = cls()
        for item in items:
            if isinstance(item, Tally):
                tally.merge(item)
            else:
                tally.add(item)
        return tally

    @property
    def cost_plus_value(self) -> int:
        return sum(with_markup(cost, bp) for bp, cost in self.cost_by_markup.items())

    @property
    def markup_bp(self) -> int | None:
        """The mark-up of the compared work when it has one only."""
        return next(iter(self.cost_by_markup)) if len(self.cost_by_markup) == 1 else None

    @property
    def invoice_value(self) -> int:
        """Issued less the credit notes allocated to them, plus drafts."""
        return self.invoiced_value - self.credited_value + self.draft_value


def with_markup(cost: int, markup_bp: int) -> int:
    """``cost`` plus ``markup_bp`` of it, rounded half up: 5000 (50%) on 50.00 is 75.00."""
    return round_half_up(Decimal(cost) * (BASIS_POINTS + markup_bp) / BASIS_POINTS)


def potentials(tally: Tally, show_cost: bool) -> dict[str, Any]:
    """The fields of ``RecoveryPotentials``."""
    return {
        "minutes": tally.minutes,
        "recovery_value": tally.recovery_value,
        "client_job_minutes": tally.tier_minutes.get("client_job", 0),
        "task_minutes": tally.tier_minutes.get("task", 0),
        "staff_rate_minutes": tally.tier_minutes.get("staff", 0),
        "staff_value": tally.staff_value,
        "cost_value": tally.cost_value if show_cost else None,
        "cost_plus_value": tally.cost_plus_value if show_cost else None,
        "markup_bp": tally.markup_bp,
    }


def figures(tally: Tally, show_cost: bool) -> dict[str, Any]:
    """The fields of ``RecoveryFigures``: the potentials against the invoice value."""
    invoice = tally.invoice_value
    cost_plus = tally.cost_plus_value
    margin = invoice - tally.cost_value
    return {
        **potentials(tally, show_cost),
        "invoiced_value": tally.invoiced_value,
        "credited_value": tally.credited_value,
        "draft_value": tally.draft_value,
        "invoice_value": invoice,
        "recovery_shortfall": tally.recovery_value - invoice,
        "recovery_bp": percentage_bp(invoice, tally.recovery_value),
        "staff_shortfall": tally.staff_value - invoice,
        "staff_recovery_bp": percentage_bp(invoice, tally.staff_value),
        "cost_plus_shortfall": cost_plus - invoice if show_cost else None,
        "cost_plus_recovery_bp": percentage_bp(invoice, cost_plus) if show_cost else None,
        "margin_value": margin if show_cost else None,
        "margin_bp": percentage_bp(margin, invoice) if show_cost else None,
        "wip_minutes": tally.wip_minutes,
        "wip_value": tally.wip_value,
    }


def cost_figures(tally: Tally) -> dict[str, Any]:
    """The fields of ``CostToBusinessFigures``: what the work cost the practice (each person's
    cost rate), against what it was invoiced at."""
    minutes = tally.minutes + tally.wip_minutes + tally.non_billable_minutes
    cost = tally.cost_value + tally.wip_cost + tally.non_billable_cost
    invoice = tally.invoice_value
    margin = invoice - tally.cost_value
    return {
        "minutes": minutes,
        "billable_minutes": tally.minutes + tally.wip_minutes,
        "non_billable_minutes": tally.non_billable_minutes,
        "cost_value": cost,
        "invoiced_cost": tally.cost_value,
        "wip_cost": tally.wip_cost,
        "non_billable_cost": tally.non_billable_cost,
        "cost_per_hour": round_half_up(Decimal(cost) * 60 / minutes) if minutes else None,
        "invoice_value": invoice,
        "margin_value": margin,
        "margin_bp": percentage_bp(margin, invoice),
    }


def tally_by[K: Hashable](pieces: Iterable[Piece], key: Callable[[Piece], K]) -> dict[K, Tally]:
    found: dict[K, Tally] = defaultdict(Tally)
    for piece in pieces:
        found[key(piece)].add(piece)
    return dict(found)
