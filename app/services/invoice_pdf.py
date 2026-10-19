"""PDF documents: invoices, credit notes and client statements (fpdf2).

The core PDF fonts are Latin-1, which includes the pound sign; text is normalised to
Latin-1 (typographic dashes and quotes become their ASCII forms, anything else a ``?``) so
no client-entered description can break rendering. Page content is left uncompressed so
the documents stay greppable and diff-friendly; they are small.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from fpdf import FPDF
from fpdf.enums import XPos, YPos
from fpdf.fonts import FontFace

from app.db.models import Client, CreditNote, CreditNoteLine, Invoice, InvoiceLine, Organization

_SYMBOLS = {"GBP": chr(0xA3), "USD": "$"}
# Typographic characters outside Latin-1, by code point, and what replaces them.
_REPLACEMENTS = {
    chr(0x2013): "-",  # en dash
    chr(0x2014): "-",  # em dash
    chr(0x2212): "-",  # minus sign
    chr(0x2018): "'",
    chr(0x2019): "'",
    chr(0x201C): '"',
    chr(0x201D): '"',
    chr(0x2026): "...",
    chr(0x2022): "*",
    chr(0x00A0): " ",  # no-break space
    chr(0x20AC): "EUR ",
}
_MARGIN = 15
_GREY = (110, 110, 120)


def latin1(text: str | None) -> str:
    """Text the core fonts can render: Latin-1 only."""
    if not text:
        return ""
    for old, new in _REPLACEMENTS.items():
        text = text.replace(old, new)
    return text.encode("latin-1", "replace").decode("latin-1")


def money(amount: int, currency: str = "GBP") -> str:
    """``123456`` -> ``£1,234.56``; negatives as ``-£12.00``. Integer maths only."""
    symbol = _SYMBOLS.get(currency, f"{currency} ")
    sign = "-" if amount < 0 else ""
    pounds, pence = divmod(abs(amount), 100)
    return f"{sign}{symbol}{pounds:,}.{pence:02d}"


def rate_label(rate_bp: int) -> str:
    whole, part = divmod(rate_bp, 100)
    return f"{whole}%" if part == 0 else f"{whole}.{part:02d}".rstrip("0") + "%"


def long_date(value: date | None) -> str:
    return f"{value.day} {value.strftime('%B %Y')}" if value else ""


def short_date(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def quantity_label(value: Decimal) -> str:
    return f"{value:,.2f}"


def hours_label(minutes: int) -> str:
    return f"{minutes // 60}:{minutes % 60:02d}"


@dataclass(frozen=True, slots=True)
class PdfTemplate:
    """What an invoice template changes on a document (``app.services.document_templates``)."""

    layout: str = "standard"
    show_logo: bool = True
    show_bank_details: bool = True
    footer_text: str | None = None


@dataclass(frozen=True, slots=True)
class ItemDetail:
    """One WIP item an invoice line bills, for the ``detailed`` layout."""

    day: date
    who: str | None
    narrative: str
    minutes: int | None
    amount: int


@dataclass(slots=True)
class Letterhead:
    name: str
    lines: list[str]
    vat_number: str | None
    company_number: str | None
    bank_details: str | None
    footer: str | None
    show_logo: bool = True

    @classmethod
    def of(cls, org: Organization, template: PdfTemplate | None = None) -> "Letterhead":
        template = template or PdfTemplate()
        lines = [
            v
            for v in (org.address_line1, org.address_line2, org.city, org.postcode, org.email)
            if v
        ]
        if org.phone:
            lines.append(org.phone)
        return cls(
            name=org.name,
            lines=lines,
            vat_number=org.vat_number if org.vat_registered else None,
            company_number=org.company_number,
            bank_details=org.bank_details if template.show_bank_details else None,
            footer=template.footer_text or org.invoice_footer,
            show_logo=template.show_logo,
        )


def client_address(client: Client, addressee: str | None) -> list[str]:
    lines = [addressee] if addressee else []
    lines.append(client.name)
    lines += [v for v in (client.address_line1, client.address_line2, client.city) if v]
    if client.postcode:
        lines.append(client.postcode)
    return lines


@dataclass(slots=True)
class StatementLine:
    day: date
    kind: str
    number: str | None
    description: str
    amount: int
    balance: int


@dataclass(slots=True)
class StatementData:
    client: Client
    currency: str
    date_from: date | None
    date_to: date
    opening_balance: int
    closing_balance: int
    unallocated_cash: int
    entries: list[StatementLine] = field(default_factory=list)


class _Document(FPDF):  # type: ignore[misc]
    def __init__(self, title: str, watermark: str | None) -> None:
        super().__init__(orientation="P", unit="mm", format="A4")
        self.watermark = watermark
        self.set_title(latin1(title))
        self.set_creator("Capium Time & Fees")
        self.set_compression(False)
        self.set_margins(_MARGIN, _MARGIN, _MARGIN)
        self.set_auto_page_break(auto=True, margin=20)
        self.alias_nb_pages()

    def header(self) -> None:
        if not self.watermark:
            return
        self.set_font("Helvetica", "B", 90)
        self.set_text_color(235, 235, 240)
        with self.rotation(45, self.w / 2, self.h / 2):
            self.text(self.w / 2 - 70, self.h / 2 + 15, self.watermark)
        self.set_text_color(0, 0, 0)
        self.set_xy(_MARGIN, _MARGIN)

    def footer(self) -> None:
        self.set_y(-14)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*_GREY)
        self.cell(0, 5, f"Page {self.page_no()} of {{nb}}", align="R")
        self.set_text_color(0, 0, 0)


def _write_lines(
    pdf: Any, lines: Sequence[str], *, size: float = 9, bold_first: bool = False
) -> None:
    for index, text in enumerate(lines):
        pdf.set_font("Helvetica", "B" if bold_first and index == 0 else "", size)
        pdf.multi_cell(90, 4.5, latin1(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _head(
    pdf: Any,
    letterhead: Letterhead,
    title: str,
    facts: Sequence[tuple[str, str]],
    address: Sequence[str],
) -> None:
    pdf.add_page()
    registrations = []
    if letterhead.vat_number:
        registrations.append(f"VAT registration {letterhead.vat_number}")
    if letterhead.company_number:
        registrations.append(f"Company number {letterhead.company_number}")
    if letterhead.show_logo:
        pdf.set_font("Helvetica", "B", 16)
        pdf.multi_cell(110, 8, latin1(letterhead.name), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_text_color(*_GREY)
        _write_lines(pdf, letterhead.lines, size=9)
    else:
        # No letterhead block (pre-printed stationery): who the document is from stays.
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*_GREY)
        pdf.cell(0, 5, "From", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        _write_lines(pdf, [letterhead.name], size=9, bold_first=True)
    if registrations:
        _write_lines(pdf, [" | ".join(registrations)], size=8)
    pdf.set_text_color(0, 0, 0)
    top = pdf.get_y() + 6

    pdf.set_xy(130, _MARGIN)
    pdf.set_font("Helvetica", "B", 20)
    pdf.cell(65, 10, latin1(title), align="R", new_x=XPos.LEFT, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 9)
    for label, value in facts:
        pdf.set_x(105)
        pdf.cell(38, 5, latin1(label), align="R")
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(52, 5, latin1(value), align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", "", 9)
    pdf.set_y(max(top, pdf.get_y() + 6))
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*_GREY)
    pdf.cell(0, 5, "To", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(0, 0, 0)
    _write_lines(pdf, address, size=10, bold_first=True)
    pdf.ln(6)


def _totals(pdf: Any, rows: Sequence[tuple[str, str, bool]]) -> None:
    for label, value, bold in rows:
        pdf.set_x(110)
        pdf.set_font("Helvetica", "B" if bold else "", 10 if bold else 9)
        pdf.cell(50, 6, latin1(label), align="R")
        pdf.cell(35, 6, latin1(value), align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _note(pdf: Any, heading: str, text: str | None) -> None:
    if not text:
        return
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(0, 5, latin1(heading), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 9)
    pdf.multi_cell(0, 4.5, latin1(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _vat_table(
    pdf: Any, vat_summary: Sequence[tuple[int, int, int]], currency: str, registered: bool
) -> None:
    if not registered or not vat_summary:
        return
    pdf.ln(4)
    pdf.set_font("Helvetica", "", 8)
    with pdf.table(
        col_widths=(30, 40, 40),
        width=110,
        align="LEFT",
        text_align=("LEFT", "RIGHT", "RIGHT"),
        line_height=5,
    ) as table:
        head = table.row()
        for text in ("VAT rate", "Net", "VAT"):
            head.cell(text)
        for rate_bp, net, vat in vat_summary:
            row = table.row()
            row.cell(rate_label(rate_bp))
            row.cell(money(net, currency))
            row.cell(money(vat, currency))


def _lines_table(
    pdf: Any,
    lines: Sequence[InvoiceLine],
    details: Mapping[UUID, Sequence[ItemDetail]],
    currency: str,
    registered: bool,
) -> None:
    """One row per line (the ``standard`` layout); with ``details`` every line is followed by
    one small row per item it bills (the ``detailed`` layout)."""
    widths = (95, 20, 25, 15, 25) if registered else (110, 20, 25, 25)
    headings = ["Description", "Qty", "Rate"] + (["VAT"] if registered else []) + ["Amount"]
    aligns = ["LEFT", "RIGHT", "RIGHT"] + (["RIGHT"] if registered else []) + ["RIGHT"]
    small = FontFace(size_pt=8, color=_GREY)
    with pdf.table(
        col_widths=widths,
        width=sum(widths),
        text_align=tuple(aligns),
        line_height=5,
    ) as table:
        head = table.row()
        for text in headings:
            head.cell(text)
        for line in lines:
            row = table.row()
            row.cell(latin1(line.description))
            row.cell(quantity_label(line.quantity))
            row.cell(money(line.unit_amount, currency))
            if registered:
                row.cell(rate_label(line.vat_rate_bp))
            row.cell(money(line.net_amount, currency))
            for item in details.get(line.id, ()):
                who = f"{item.who}  " if item.who else ""
                sub = table.row()
                sub.cell(latin1(f"   {short_date(item.day)}  {who}{item.narrative}"), style=small)
                sub.cell(hours_label(item.minutes) if item.minutes is not None else "", style=small)
                sub.cell("", style=small)
                if registered:
                    sub.cell("", style=small)
                sub.cell(money(item.amount, currency), style=small)


def _summary_table(
    pdf: Any,
    lines: Sequence[InvoiceLine],
    job_names: Mapping[UUID, str],
    currency: str,
    registered: bool,
) -> None:
    """The ``summary`` layout: the lines of a job (at one VAT rate) become one row named after
    the job; lines without a job keep their own row."""
    rows: list[list[Any]] = []  # [description, vat_rate_bp, net]
    by_job: dict[tuple[UUID, int], list[Any]] = {}
    for line in lines:
        if line.job_id is None:
            rows.append([line.description, line.vat_rate_bp, line.net_amount])
            continue
        key = (line.job_id, line.vat_rate_bp)
        if key not in by_job:
            by_job[key] = [job_names.get(line.job_id) or line.description, line.vat_rate_bp, 0]
            rows.append(by_job[key])
        by_job[key][2] += line.net_amount
    widths = (130, 20, 30) if registered else (150, 30)
    headings = ["Description"] + (["VAT"] if registered else []) + ["Amount"]
    aligns = ["LEFT"] + (["RIGHT"] if registered else []) + ["RIGHT"]
    with pdf.table(
        col_widths=widths, width=sum(widths), text_align=tuple(aligns), line_height=5
    ) as table:
        head = table.row()
        for text in headings:
            head.cell(text)
        for description, rate_bp, net in rows:
            row = table.row()
            row.cell(latin1(description))
            if registered:
                row.cell(rate_label(rate_bp))
            row.cell(money(net, currency))


def render_invoice(
    *,
    organization: Organization,
    client: Client,
    addressee: str | None,
    invoice: Invoice,
    lines: Sequence[InvoiceLine],
    vat_summary: Sequence[tuple[int, int, int]],
    template: PdfTemplate | None = None,
    details: Mapping[UUID, Sequence[ItemDetail]] | None = None,
    job_names: Mapping[UUID, str] | None = None,
) -> bytes:
    """``vat_summary`` is ``(rate_bp, net, vat)`` per rate.

    ``template`` sets the layout (``detailed`` lists the items of every line from ``details``,
    by line id; ``summary`` merges the lines of a job, named by ``job_names``), the letterhead,
    the bank details and the footer.
    """
    template = template or PdfTemplate()
    currency = invoice.currency
    unissued = invoice.status in ("draft", "awaiting_approval", "approved")
    watermark = "DRAFT" if unissued else ("VOID" if invoice.status == "void" else None)
    number = invoice.number or "DRAFT"
    pdf = _Document(f"Invoice {number}", watermark)
    letterhead = Letterhead.of(organization, template)
    facts = [("Invoice number", number)]
    if invoice.issue_date:
        facts.append(("Date", long_date(invoice.issue_date)))
    if invoice.due_date:
        facts.append(("Due", long_date(invoice.due_date)))
    if invoice.reference:
        facts.append(("Reference", invoice.reference))
    if client.vat_number:
        facts.append(("Client VAT", client.vat_number))
    _head(pdf, letterhead, "INVOICE", facts, client_address(client, addressee))

    registered = organization.vat_registered
    pdf.set_font("Helvetica", "", 9)
    if template.layout == "summary":
        _summary_table(pdf, lines, job_names or {}, currency, registered)
    else:
        shown = details if template.layout == "detailed" else None
        _lines_table(pdf, lines, shown or {}, currency, registered)
    pdf.ln(3)
    totals = [("Net", money(invoice.net_total, currency), False)]
    if registered:
        totals.append(("VAT", money(invoice.vat_total, currency), False))
    totals.append(("Total", money(invoice.gross_total, currency), True))
    if invoice.status == "issued" and (invoice.paid_total or invoice.credited_total):
        if invoice.credited_total:
            totals.append(("Credited", money(-invoice.credited_total, currency), False))
        if invoice.paid_total:
            totals.append(("Paid", money(-invoice.paid_total, currency), False))
        totals.append(("Balance due", money(invoice.balance, currency), True))
    _totals(pdf, totals)
    _vat_table(pdf, vat_summary, currency, registered)
    _note(pdf, "Notes", invoice.notes)
    if invoice.status == "void":
        _note(pdf, "Void", invoice.void_reason)
    _note(pdf, "How to pay", letterhead.bank_details)
    _note(pdf, "", letterhead.footer)
    return bytes(pdf.output())


def render_credit_note(
    *,
    organization: Organization,
    client: Client,
    addressee: str | None,
    note: CreditNote,
    invoice: Invoice,
    lines: Sequence[CreditNoteLine],
    template: PdfTemplate | None = None,
) -> bytes:
    currency = invoice.currency
    pdf = _Document(f"Credit note {note.number}", None)
    facts = [
        ("Credit note", note.number),
        ("Date", long_date(note.issue_date)),
        ("Against invoice", invoice.number or ""),
    ]
    letterhead = Letterhead.of(organization, template)
    _head(pdf, letterhead, "CREDIT NOTE", facts, client_address(client, addressee))
    registered = organization.vat_registered
    widths = (115, 20, 30) if registered else (135, 30)
    headings = ["Description"] + (["VAT"] if registered else []) + ["Amount"]
    aligns = ["LEFT"] + (["RIGHT"] if registered else []) + ["RIGHT"]
    pdf.set_font("Helvetica", "", 9)
    with pdf.table(
        col_widths=widths, width=sum(widths), text_align=tuple(aligns), line_height=5
    ) as table:
        head = table.row()
        for text in headings:
            head.cell(text)
        for line in lines:
            row = table.row()
            row.cell(latin1(line.description))
            if registered:
                row.cell(rate_label(line.vat_rate_bp))
            row.cell(money(line.net_amount, currency))
    pdf.ln(3)
    totals = [("Net", money(note.net_total, currency), False)]
    if registered:
        totals.append(("VAT", money(note.vat_total, currency), False))
    totals.append(("Total credit", money(note.gross_total, currency), True))
    _totals(pdf, totals)
    _note(pdf, "Reason", note.reason)
    _note(pdf, "", letterhead.footer)
    return bytes(pdf.output())


def render_statement(
    *,
    organization: Organization,
    addressee: str | None,
    data: StatementData,
    template: PdfTemplate | None = None,
) -> bytes:
    currency = data.currency
    pdf = _Document(f"Statement {data.client.name}", None)
    period = (
        f"{short_date(data.date_from)} to {short_date(data.date_to)}"
        if data.date_from
        else f"to {short_date(data.date_to)}"
    )
    facts = [("Account", data.client.code), ("Period", period)]
    letterhead = Letterhead.of(organization, template)
    _head(pdf, letterhead, "STATEMENT", facts, client_address(data.client, addressee))
    pdf.set_font("Helvetica", "", 9)
    widths = (24, 26, 70, 30, 30)  # the A4 text width: 180 mm
    with pdf.table(
        col_widths=widths,
        width=sum(widths),
        text_align=("LEFT", "LEFT", "LEFT", "RIGHT", "RIGHT"),
        line_height=5,
    ) as table:
        head = table.row()
        for text in ("Date", "Number", "Details", "Amount", "Balance"):
            head.cell(text)
        opening = table.row()
        opening.cell(short_date(data.date_from) if data.date_from else "")
        opening.cell("")
        opening.cell("Opening balance")
        opening.cell("")
        opening.cell(money(data.opening_balance, currency))
        for entry in data.entries:
            row = table.row()
            row.cell(short_date(entry.day))
            row.cell(latin1(entry.number or ""))
            row.cell(latin1(entry.description))
            row.cell(money(entry.amount, currency))
            row.cell(money(entry.balance, currency))
    pdf.ln(3)
    totals = [("Closing balance", money(data.closing_balance, currency), True)]
    if data.unallocated_cash:
        totals.append(("Cash on account (included)", money(data.unallocated_cash, currency), False))
    _totals(pdf, totals)
    _note(pdf, "How to pay", letterhead.bank_details)
    _note(pdf, "", letterhead.footer)
    return bytes(pdf.output())
