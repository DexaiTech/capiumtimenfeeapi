"""The bookkeeping system behind one interface (Phase 2B).

Issued invoices and credit notes are sent to bookkeeping with the income account of each line's
service and the tax code of its VAT code, and bookkeeping answers with its own reference.
Time & Fees reaches it only through :class:`BookkeepingGateway`; the real system is one more
implementation next to :class:`MockBookkeepingGateway`. A send carries an idempotency key
(``invoice:<id>``), so sending the same document twice books it once.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal, Protocol

from app.services.integrations.common import IntegrationRejectedError, IntegrationUnavailableError

DocumentKind = Literal["invoice", "credit_note"]


@dataclass(frozen=True, slots=True)
class BookkeepingLine:
    """One line, in pence: ``net_amount`` and ``vat_amount`` are those of the line."""

    description: str
    quantity: Decimal
    unit_amount: int
    net_amount: int
    vat_amount: int
    vat_rate_bp: int
    account_code: str
    tax_code: str


@dataclass(frozen=True, slots=True)
class BookkeepingDocument:
    kind: DocumentKind
    number: str
    client_code: str
    client_name: str
    issue_date: date
    due_date: date | None
    currency: str
    net_total: int
    vat_total: int
    gross_total: int
    lines: tuple[BookkeepingLine, ...]
    # A credit note names the invoice it credits and why.
    invoice_number: str | None = None
    reason: str | None = None
    reference: str | None = None


@dataclass(frozen=True, slots=True)
class BookkeepingReceipt:
    """Bookkeeping's reference for the document; ``duplicate`` when it had already booked this
    idempotency key and so booked nothing new."""

    external_ref: str
    duplicate: bool = False


class BookkeepingGateway(Protocol):
    """A bookkeeping system. Every call raises :class:`IntegrationUnavailableError` when the
    system cannot be used (the send is retried later) and :class:`IntegrationRejectedError` when
    it refuses the document for good (it does not add up)."""

    name: str

    async def send_invoice(
        self, document: BookkeepingDocument, *, idempotency_key: str
    ) -> BookkeepingReceipt: ...

    async def send_credit_note(
        self, document: BookkeepingDocument, *, idempotency_key: str
    ) -> BookkeepingReceipt: ...


class MockBookkeepingGateway:
    """In-memory ledger for development and tests: it keeps what it was sent, books each
    idempotency key once, refuses documents whose lines do not add up and can be switched off or
    made to fail. State lives in the process."""

    name = "mock"

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Back to an empty ledger, switched on."""
        self._booked: dict[str, tuple[str, BookkeepingDocument]] = {}
        self._available = True
        self._fail_next: list[bool] = []
        self.calls = 0

    @property
    def available(self) -> bool:
        return self._available

    def set_available(self, available: bool) -> None:
        self._available = available

    def fail_next(self, count: int = 1, *, permanent: bool = False) -> None:
        """Make the next ``count`` sends fail (``permanent``: bookkeeping refuses them)."""
        self._fail_next.extend([permanent] * count)

    @property
    def documents(self) -> list[BookkeepingDocument]:
        """What was booked, in order (a repeated key is booked once)."""
        return [document for _, document in self._booked.values()]

    def booked(self, idempotency_key: str) -> BookkeepingDocument | None:
        entry = self._booked.get(idempotency_key)
        return entry[1] if entry else None

    async def send_invoice(
        self, document: BookkeepingDocument, *, idempotency_key: str
    ) -> BookkeepingReceipt:
        return self._book("invoice", document, idempotency_key)

    async def send_credit_note(
        self, document: BookkeepingDocument, *, idempotency_key: str
    ) -> BookkeepingReceipt:
        return self._book("credit_note", document, idempotency_key)

    def _book(
        self, kind: DocumentKind, document: BookkeepingDocument, idempotency_key: str
    ) -> BookkeepingReceipt:
        self.calls += 1
        if not self._available:
            raise IntegrationUnavailableError("The mock bookkeeping system is switched off.")
        if self._fail_next:
            if self._fail_next.pop(0):
                raise IntegrationRejectedError("Bookkeeping refused the document.")
            raise IntegrationUnavailableError("Bookkeeping did not answer.")
        if document.kind != kind:
            raise IntegrationRejectedError(f"A {document.kind} cannot be sent as a {kind}.")
        if idempotency_key in self._booked:
            return BookkeepingReceipt(self._booked[idempotency_key][0], duplicate=True)
        if (
            sum(line.net_amount for line in document.lines) != document.net_total
            or sum(line.vat_amount for line in document.lines) != document.vat_total
            or document.net_total + document.vat_total != document.gross_total
        ):
            raise IntegrationRejectedError(
                f"{document.number}: the lines do not add up to the totals."
            )
        reference = f"BK-{len(self._booked) + 1:06d}"
        self._booked[idempotency_key] = (reference, document)
        return BookkeepingReceipt(reference)
