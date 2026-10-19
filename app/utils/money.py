"""Money arithmetic. Amounts are integer minor units; rounding is always half up.

Never use floats for money. Every function here takes and returns ``int`` minor units,
working through ``Decimal`` in between.
"""

from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal

MINUTES_PER_HOUR = 60
BASIS_POINTS = 10_000


def round_half_up(value: Decimal) -> int:
    """Round to the nearest whole minor unit, halves away from zero (2.5 -> 3, -2.5 -> -3)."""
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def time_value(minutes: int, hourly_rate: int) -> int:
    """Value of ``minutes`` at ``hourly_rate`` (minor units per hour)."""
    return round_half_up(Decimal(minutes) * Decimal(hourly_rate) / MINUTES_PER_HOUR)


def vat_on(net: int, rate_bp: int) -> int:
    """VAT on a net amount at a rate in basis points (2000 = 20%), rounded per line."""
    return round_half_up(Decimal(net) * Decimal(rate_bp) / BASIS_POINTS)


def line_net(quantity: Decimal, unit_amount: int) -> int:
    return round_half_up(quantity * Decimal(unit_amount))


def percentage_bp(part: int, whole: int) -> int | None:
    """``part / whole`` in basis points (7500 = 75%), or None when ``whole`` is zero."""
    if whole == 0:
        return None
    return round_half_up(Decimal(part) * BASIS_POINTS / Decimal(whole))


def apportion(total: int, weights: Sequence[int]) -> list[int]:
    """Split ``total`` across items in proportion to ``weights`` so the parts sum exactly.

    Largest-remainder method; ties go to the earlier item. With all weights zero the total
    is split as evenly as possible. Used to spread a billed amount over the WIP items it
    clears, so each item's write-up or write-off is known.
    """
    count = len(weights)
    if count == 0:
        if total != 0:
            raise ValueError("cannot apportion a non-zero total over no items")
        return []
    if any(w < 0 for w in weights):
        raise ValueError("weights must not be negative")
    weight_sum = sum(weights)
    effective = list(weights) if weight_sum else [1] * count
    weight_sum = weight_sum or count
    sign = -1 if total < 0 else 1
    magnitude = abs(total)
    shares = [magnitude * w // weight_sum for w in effective]
    remainders = [(magnitude * w % weight_sum, -i) for i, w in enumerate(effective)]
    for _, neg_index in sorted(remainders, reverse=True)[: magnitude - sum(shares)]:
        shares[-neg_index] += 1
    return [sign * s for s in shares]
