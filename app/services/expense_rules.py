"""Rules of expense lines and claims that need no database: the money arithmetic, which line needs
a receipt, and the error codes of the module (docs/modules/expenses.md, "Error codes")."""

from dataclasses import dataclass
from decimal import Decimal

from app.core.exceptions import AuthorizationError, ConflictError, ValidationError
from app.db.models.expenses import MAX_RECEIPTS_PER_LINE
from app.utils.money import BASIS_POINTS, round_half_up, vat_on

MAX_LINES_PER_CLAIM = 200
# The largest gross of a line, in pence (the request schemas cap each amount at the same value).
MAX_AMOUNT = 10**12


class ExpenseClaimLockedError(ConflictError):
    code = "EXPENSE_CLAIM_LOCKED"
    message = "This claim cannot be changed in its current status."


class ExpenseBilledError(ConflictError):
    code = "EXPENSE_BILLED"
    message = "This expense has been billed or written off and cannot be changed."


class ExpenseTypeInUseError(ConflictError):
    code = "EXPENSE_TYPE_IN_USE"
    message = "Expenses use this type: deactivate it instead of deleting it."


class ExpenseJobClosedError(ConflictError):
    code = "JOB_CLOSED"
    message = "Expenses cannot be recorded on a completed or cancelled job."


class ClaimLineLimitError(ConflictError):
    code = "EXPENSE_CLAIM_LINE_LIMIT"
    message = f"A claim holds at most {MAX_LINES_PER_CLAIM} lines."


class ReceiptLimitError(ConflictError):
    code = "RECEIPT_LIMIT_REACHED"
    message = f"An expense holds at most {MAX_RECEIPTS_PER_LINE} receipts."


class ClaimEmptyError(ValidationError):
    code = "EXPENSE_CLAIM_EMPTY"
    message = "A claim needs at least one line before it can be submitted."


class ReceiptRequiredError(ValidationError):
    code = "RECEIPT_REQUIRED"
    message = "Some lines are above the receipt threshold and have no receipt."


class ReturnReasonRequiredError(ValidationError):
    code = "EXPENSE_RETURN_REASON_REQUIRED"
    message = "Say why the claim is returned."


class CorrectionReasonRequiredError(ValidationError):
    code = "EXPENSE_CORRECTION_REASON_REQUIRED"
    message = "Say why the approved expense is changed."


class SelfApprovalError(AuthorizationError):
    code = "SELF_APPROVAL_FORBIDDEN"
    message = "You cannot decide on your own expense claim."


def mileage_net(miles: Decimal, rate_per_mile: int) -> int:
    """Net of a mileage line: miles x the rate (pence per mile), rounded half up."""
    return round_half_up(miles * Decimal(rate_per_mile))


def recharge_for(net: int, markup_bp: int) -> int:
    """What a billable line is charged to the client: the net plus the markup, half up."""
    return round_half_up(Decimal(net) * (BASIS_POINTS + markup_bp) / BASIS_POINTS)


def vat_in_gross(gross: int, rate_bp: int) -> int:
    """The VAT contained in a VAT-inclusive amount at ``rate_bp``, half up."""
    return round_half_up(Decimal(gross) * rate_bp / (BASIS_POINTS + rate_bp))


def receipt_required(gross: int, threshold: int, *, is_mileage: bool) -> bool:
    """A line above the practice's threshold (gross, pence) needs a receipt; mileage never does."""
    return not is_mileage and gross > threshold


@dataclass(frozen=True, slots=True)
class Amounts:
    net: int
    vat: int
    gross: int


def resolve_amounts(
    *,
    net: int | None,
    vat: int | None,
    gross: int | None,
    rate_bp: int | None,
    kept_net: int | None = None,
) -> Amounts:
    """Any two of net, VAT and gross give the third. Only the net or only the gross takes the VAT
    from ``rate_bp`` (none: no VAT); only the VAT keeps ``kept_net`` (an edit of a stored line).
    Gross is net + VAT and nothing is negative or all zero (422 naming the field)."""
    rate = rate_bp or 0
    if net is not None and vat is not None and gross is not None:
        if gross != net + vat:
            raise ValidationError.for_field(
                "gross", "Gross must be the net plus the VAT.", "gross_mismatch"
            )
        result = Amounts(net, vat, gross)
    elif net is not None and vat is not None:
        result = Amounts(net, vat, net + vat)
    elif net is not None and gross is not None:
        if gross < net:
            raise ValidationError.for_field(
                "gross", "Gross cannot be less than the net.", "gross_below_net"
            )
        result = Amounts(net, gross - net, gross)
    elif vat is not None and gross is not None:
        if vat > gross:
            raise ValidationError.for_field(
                "vat", "VAT cannot be more than the gross.", "vat_above_gross"
            )
        result = Amounts(gross - vat, vat, gross)
    elif net is not None:
        computed = vat_on(net, rate)
        result = Amounts(net, computed, net + computed)
    elif gross is not None:
        contained = vat_in_gross(gross, rate)
        result = Amounts(gross - contained, contained, gross)
    elif vat is not None and kept_net is not None:
        result = Amounts(kept_net, vat, kept_net + vat)
    else:
        raise ValidationError.for_field("net", "Give the net or the gross amount.", "required")
    if result.gross <= 0:
        raise ValidationError.for_field("net", "The amount must be more than zero.", "not_positive")
    if result.gross > MAX_AMOUNT:
        raise ValidationError.for_field("net", "The amount is too large.", "too_large")
    return result
