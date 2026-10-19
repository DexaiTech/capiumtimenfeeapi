"""Downloads of the reports as CSV, Excel and PDF: content, permission, audit, masking and layout."""

import csv
import io
import re
from datetime import date

import pytest
from httpx import AsyncClient

from tests.domain_factories import make_client, make_time_entry, practice, staff
from tests.expense_helpers import make_claim, make_line
from tests.factories import add_member, audit_entries, error
from tests.report_helpers import (
    SEPTEMBER,
    XLSX,
    call,
    download,
    get_data,
    pdf_page_count,
    pdf_text,
    wide_dataset,
    xlsx_lines_above,
    xlsx_table,
)
from tests.reports_helpers import LineSpec, custom_role, make_invoice
from tests.time_gap_helpers import pin_today

AS_AT = "2026-10-17"
# The reports that download (WIP has its own export), their parameters and their file titles.
REPORTS = [
    ("aged-debtors", {"as_at": AS_AT}, "Aged Debtors"),
    ("time-summary", {"group_by": "client", **SEPTEMBER}, "Time Analysis"),
    ("utilisation", SEPTEMBER, "Utilisation"),
    ("recoverability", SEPTEMBER, "Recovery"),
    ("billing-summary", SEPTEMBER, "Billing"),
    ("job-profitability", SEPTEMBER, "Job Profitability"),
    ("vat-summary", SEPTEMBER, "VAT Summary"),
    ("expenses", SEPTEMBER, "Expenses"),
    ("payments", SEPTEMBER, "Payments"),
    ("missing-timesheets", {}, "Missing Timesheets"),
]
FILES = {"csv": "text/csv; charset=utf-8", "xlsx": XLSX, "pdf": "application/pdf"}


async def everything(client: AsyncClient, monkeypatch):
    """The wide dataset plus two expense lines; today is Wednesday 21 October, so the last
    completed week (for the missing timesheets) is the one starting 12 October."""
    pin_today(monkeypatch, date(2026, 10, 21))
    d = await wide_dataset(client)
    claim = await make_claim(d.org, d.sam, status="approved", decided_by=d.owner)
    await make_line(
        d.org, claim, code="TRAVEL", incurred_on=date(2026, 9, 9), net=4_300, client=d.alpha,
        is_billable=True, recharge_amount=4_730,
    )  # fmt: skip
    await make_line(d.org, claim, code="POSTAGE", incurred_on=date(2026, 9, 10), net=500)
    return d


def csv_rows(content: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))


def sheet_of(rows: list[list]) -> dict:
    """The table's first data row by header."""
    return dict(zip(rows[0], rows[1], strict=False))


@pytest.mark.parametrize(("name", "params", "title"), REPORTS)
async def test_every_report_downloads_in_every_format(
    client: AsyncClient, monkeypatch, name: str, params: dict, title: str
) -> None:
    d = await everything(client, monkeypatch)
    data = await get_data(client, d.owner, d.org, name, **params)
    count = len(data["rows"])
    assert count >= 1, name

    responses = {fmt: await download(client, d.owner, d.org, name, fmt, **params) for fmt in FILES}
    for fmt, response in responses.items():
        assert response.status_code == 200, (fmt, response.text[:300])
        assert response.headers["content-type"] == FILES[fmt]
        disposition = response.headers["content-disposition"]
        assert re.fullmatch(rf'attachment; filename="{name}_[0-9_-]+\.{fmt}"', disposition), (
            disposition
        )

    table = csv_rows(responses["csv"].content)
    assert responses["csv"].content.startswith(b"\xef\xbb\xbf")
    assert len(table) == count + 2  # the header, every row, the Total row
    assert table[-1][0] == "Total"

    workbook = xlsx_table(responses["xlsx"].content, table[0][0])
    assert workbook[0] == table[0]  # the same columns as the CSV
    assert len(workbook) == count + 2 and workbook[-1][0] == "Total"
    above = xlsx_lines_above(responses["xlsx"].content, table[0][0])
    assert above[0].startswith(f"{title} - Acme Accountants")

    pdf = responses["pdf"].content
    assert pdf.startswith(b"%PDF")
    text = pdf_text(pdf)
    assert title in text and "Acme Accountants" in text
    assert "Total" in text and "Page 1 of" in text
    assert pdf_page_count(pdf) >= 1


async def test_downloads_ignore_paging(client: AsyncClient, monkeypatch) -> None:
    d = await everything(client, monkeypatch)
    paged = await get_data(client, d.owner, d.org, "job-profitability", limit=1, **SEPTEMBER)
    assert len(paged["rows"]) == 1 and paged["page"]["total"] == 3
    for fmt in ("csv", "xlsx"):
        response = await download(
            client, d.owner, d.org, "job-profitability", fmt, limit=1, offset=1, **SEPTEMBER
        )
        assert response.status_code == 200
        rows = (
            csv_rows(response.content)
            if fmt == "csv"
            else xlsx_table(response.content, "Job number")
        )
        assert len(rows) == 3 + 2, fmt  # all three jobs, whatever the page says


async def test_the_files_hold_the_figures_of_the_report(client: AsyncClient, monkeypatch) -> None:
    d = await everything(client, monkeypatch)
    params = {"group_by": "client", **SEPTEMBER}
    # Alpha: 60 + 30 minutes, 120.00 + 60.00 of charge-out value, cost 40.00 + 20.00.
    csv_table = csv_rows(
        (await download(client, d.owner, d.org, "time-summary", "csv", **params)).content
    )
    assert csv_table[0][:3] == ["Client", "Client code", "Entries"]
    assert csv_table[1][:4] == ["Alpha Ltd", "ALPHA", "2", "1.50"]
    assert csv_table[1][-2:] == ["180.00", "60.00"]
    book = (await download(client, d.owner, d.org, "time-summary", "xlsx", **params)).content
    alpha = sheet_of(xlsx_table(book, "Client"))
    assert alpha["Client code"] == "ALPHA"
    assert alpha["Entries"] == 2
    assert alpha["Total hours"] == pytest.approx(1.5)
    assert alpha["Charge value"] == pytest.approx(180.0)
    assert alpha["Cost value"] == pytest.approx(60.0)
    assert alpha["Chargeable %"] == pytest.approx(100.0)
    pdf = pdf_text(
        (await download(client, d.owner, d.org, "time-summary", "pdf", **params)).content
    )
    assert "Alpha Ltd" in pdf and "1:30" in pdf and "\xa3180.00" in pdf
    assert "Period: 01/09/2026 to 30/09/2026" in pdf and "Grouped by: client" in pdf


async def test_the_files_name_the_filters_in_words(client: AsyncClient, monkeypatch) -> None:
    d = await everything(client, monkeypatch)
    params = {"group_by": "job", "client_id": d.alpha.id, "user_id": d.sam.id, **SEPTEMBER}
    book = (await download(client, d.owner, d.org, "time-summary", "xlsx", **params)).content
    notes = xlsx_lines_above(book, "Job")
    assert "Client: Alpha Ltd (ALPHA)" in notes
    assert "Person: Sam Staff" in notes
    assert "Grouped by: job" in notes and "Period: 01/09/2026 to 30/09/2026" in notes
    pdf = pdf_text(
        (await download(client, d.owner, d.org, "time-summary", "pdf", **params)).content
    )
    # Parentheses are escaped inside a PDF string.
    assert "Client: Alpha Ltd \\(ALPHA\\)" in pdf and "Person: Sam Staff" in pdf
    debtors = await download(client, d.owner, d.org, "aged-debtors", "pdf", as_at=AS_AT)
    assert "As at: 17/10/2026" in pdf_text(debtors.content)
    expenses = await download(
        client,
        d.owner,
        d.org,
        "expenses",
        "pdf",
        status=["approved"],
        is_billable="true",
        **SEPTEMBER,
    )
    text = pdf_text(expenses.content)
    assert "Status: approved" in text and "Billable: yes" in text


# --- Permission and audit ------------------------------------------------------------------


async def test_a_download_needs_report_export(client: AsyncClient, monkeypatch) -> None:
    d = await everything(client, monkeypatch)
    for role in ("staff", "viewer"):  # report.read, but no report.export
        person = await staff(d.org, role)
        assert (await call(client, person, d.org, "time-summary", **SEPTEMBER)).status_code == 200
        for fmt in FILES:
            response = await download(client, person, d.org, "time-summary", fmt, **SEPTEMBER)
            assert response.status_code == 403, (role, fmt)
            assert error(response)["code"] == "PERMISSION_DENIED"
    # Financial reports: report.financial alone does not download either.
    await custom_role(d.org, "Finance only", {"report.read", "report.financial"})
    finance = await add_member(d.org["id"], "Finance only")
    for name, params in (
        ("aged-debtors", {"as_at": AS_AT}),
        ("billing-summary", SEPTEMBER),
        ("payments", SEPTEMBER),
        ("expenses", SEPTEMBER),
    ):
        assert (await call(client, finance, d.org, name, **params)).status_code == 200
        for fmt in FILES:
            response = await download(client, finance, d.org, name, fmt, **params)
            assert response.status_code == 403, (name, fmt)
    assert await audit_entries(d.org["id"], "report.exported") == []  # nothing left the building
    # With the permission every format works.
    exporter = await staff(d.org, "manager")
    for fmt in FILES:
        response = await download(client, exporter, d.org, "time-summary", fmt, **SEPTEMBER)
        assert response.status_code == 200, fmt


async def test_the_report_permission_still_applies_to_downloads(
    client: AsyncClient, monkeypatch
) -> None:
    d = await everything(client, monkeypatch)
    await custom_role(d.org, "Exporter", {"report.export"})  # export, but no report at all
    person = await add_member(d.org["id"], "Exporter")
    for name in ("time-summary", "aged-debtors", "expenses", "missing-timesheets"):
        for fmt in ("json", "csv", "xlsx", "pdf"):
            response = await download(client, person, d.org, name, fmt, **SEPTEMBER)
            assert response.status_code == 403, (name, fmt)
    other_owner, _ = await practice(client, "Other & Co")
    response = await download(client, other_owner, d.org, "time-summary", "pdf", **SEPTEMBER)
    assert response.status_code == 404  # not a member: another practice


async def test_a_download_is_audited_without_its_rows(client: AsyncClient, monkeypatch) -> None:
    d = await everything(client, monkeypatch)
    await get_data(client, d.owner, d.org, "time-summary", **SEPTEMBER)  # JSON: not audited
    assert await audit_entries(d.org["id"], "report.exported") == []
    await download(client, d.owner, d.org, "time-summary", "csv", **SEPTEMBER)
    await download(
        client,
        d.owner,
        d.org,
        "expenses",
        "pdf",
        status=["approved"],
        client_id=d.alpha.id,
        **SEPTEMBER,
    )
    first, second = await audit_entries(d.org["id"], "report.exported")
    assert (first["actor_user_id"], first["target_type"]) == (str(d.owner.id), "report")
    assert first["metadata"] == {
        "report": "time-summary",
        "format": "csv",
        "filters": {"date_from": "2026-09-01", "date_to": "2026-09-30", "group_by": "person"},
        "row_count": 2,  # Olivia and Sam
    }
    assert second["metadata"]["report"] == "expenses"
    assert second["metadata"]["format"] == "pdf"
    assert second["metadata"]["filters"]["status"] == ["approved"]
    assert second["metadata"]["filters"]["client_id"] == str(d.alpha.id)
    assert second["metadata"]["row_count"] == 1
    # The names and figures of the rows are not in the log.
    log = str(first["metadata"]) + str(second["metadata"])
    assert "Olivia" not in log and "Alpha" not in log and "4300" not in log


# --- Masking, injection and layout ---------------------------------------------------------


async def test_cost_and_margin_stay_masked_in_every_format(
    client: AsyncClient, monkeypatch
) -> None:
    d = await everything(client, monkeypatch)
    # report.financial and report.export, but no rate.read.
    await custom_role(
        d.org, "Finance exporter", {"report.financial", "report.export", "report.read"}
    )
    person = await add_member(d.org["id"], "Finance exporter")
    seen = await get_data(client, person, d.org, "job-profitability", **SEPTEMBER)
    assert all(r["cost_value"] is None and r["margin"] is None for r in seen["rows"])

    masked_csv = csv_rows(
        (await download(client, person, d.org, "job-profitability", "csv", **SEPTEMBER)).content
    )
    full_csv = csv_rows(
        (await download(client, d.owner, d.org, "job-profitability", "csv", **SEPTEMBER)).content
    )
    assert masked_csv[0] == full_csv[0]
    cost, margin = full_csv[0].index("Cost"), full_csv[0].index("Margin")
    assert all(row[cost] == "" and row[margin] == "" for row in masked_csv[1:])
    assert all(row[cost] != "" for row in full_csv[1:])

    masked_book = xlsx_table(
        (await download(client, person, d.org, "job-profitability", "xlsx", **SEPTEMBER)).content,
        "Job number",
    )
    assert all(row[cost] is None and row[margin] is None for row in masked_book[1:])
    full_book = xlsx_table(
        (await download(client, d.owner, d.org, "job-profitability", "xlsx", **SEPTEMBER)).content,
        "Job number",
    )
    assert all(row[cost] is not None for row in full_book[1:])

    people = await download(client, person, d.org, "time-summary", "csv", **SEPTEMBER)
    table = csv_rows(people.content)
    assert all(row[table[0].index("Cost value")] == "" for row in table[1:])


async def test_text_that_looks_like_a_formula_is_neutralised(
    client: AsyncClient, monkeypatch
) -> None:
    pin_today(monkeypatch, date(2026, 10, 21))
    owner, org = await practice(client)
    evil = await make_client(org, '=HYPERLINK("http://evil.example","x")', code="EVIL")
    await make_time_entry(org, owner, client=evil, entry_date=date(2026, 9, 7))
    params = {"group_by": "client", **SEPTEMBER}
    table = csv_rows((await download(client, owner, org, "time-summary", "csv", **params)).content)
    assert table[1][0].startswith("'=HYPERLINK")
    book = xlsx_table(
        (await download(client, owner, org, "time-summary", "xlsx", **params)).content, "Client"
    )
    assert str(book[1][0]).startswith("'=HYPERLINK")
    pdf = await download(client, owner, org, "time-summary", "pdf", **params)
    assert pdf.status_code == 200


async def test_pdf_layout_follows_the_width_of_the_table(client: AsyncClient, monkeypatch) -> None:
    d = await everything(client, monkeypatch)

    def page_size(content: bytes) -> tuple[float, float]:
        match = re.search(rb"/MediaBox \[0(?:\.0+)? 0(?:\.0+)? ([0-9.]+) ([0-9.]+)\]", content)
        assert match, "no media box"
        return float(match.group(1)), float(match.group(2))

    wide = await download(client, d.owner, d.org, "job-profitability", "pdf", **SEPTEMBER)
    width, height = page_size(wide.content)
    assert width > height  # nineteen columns are printed landscape
    narrow = await download(client, d.owner, d.org, "payments", "pdf", **SEPTEMBER)
    width, height = page_size(narrow.content)
    assert width < height  # seven columns fit a portrait page


async def test_a_long_report_runs_over_pages_with_the_header_repeated(
    client: AsyncClient, monkeypatch
) -> None:
    pin_today(monkeypatch, date(2026, 10, 21))
    owner, org = await practice(client)
    for number in range(1, 71):
        debtor = await make_client(org, f"Debtor {number:03d} Ltd", code=f"D{number:03d}")
        await make_invoice(
            org, debtor, [LineSpec(10_000 + number, vat_bp=0)], issue_date=date(2026, 9, 1)
        )
    response = await download(client, owner, org, "aged-debtors", "pdf", as_at=AS_AT)
    assert response.status_code == 200
    text = pdf_text(response.content)
    pages = pdf_page_count(response.content)
    assert pages >= 2
    # The page count is a separate string in the page footer: (Page 3 of ) (3).
    assert re.search(rf"\(Page {pages} of \) Tj \({pages}\) Tj", text), pages
    assert "(Page 1 of " in text
    assert text.count("Client code") >= pages  # the headings come again on every page
    assert "Debtor 001 Ltd" in text and "Debtor 070 Ltd" in text
    assert text.count("Total") >= 1
    book = xlsx_table(
        (await download(client, owner, org, "aged-debtors", "xlsx", as_at=AS_AT)).content,
        "Client code",
    )
    assert len(book) == 70 + 2


async def test_text_the_pdf_fonts_cannot_draw_does_not_break_it(
    client: AsyncClient, monkeypatch
) -> None:
    pin_today(monkeypatch, date(2026, 10, 21))
    owner, org = await practice(client, "Caf\u00e9 \u2013 Partners")
    odd = await make_client(org, "Zo\u00eb \u2018Quotes\u2019 \u4f60\u597d Ltd", code="ODD")
    await make_time_entry(org, owner, client=odd, entry_date=date(2026, 9, 7))
    response = await download(
        client, owner, org, "time-summary", "pdf", group_by="client", **SEPTEMBER
    )
    assert response.status_code == 200
    text = pdf_text(response.content)
    assert "Zo\xeb 'Quotes' ?? Ltd" in text  # typographic quotes made plain, CJK shown as ?
    assert "Caf\xe9 - Partners" in text


async def test_an_empty_report_still_downloads(client: AsyncClient, monkeypatch) -> None:
    pin_today(monkeypatch, date(2026, 10, 21))
    owner, org = await practice(client)
    params = {"as_at": AS_AT}
    table = csv_rows((await download(client, owner, org, "aged-debtors", "csv", **params)).content)
    assert len(table) == 2 and table[1][0] == "Total"
    book = xlsx_table(
        (await download(client, owner, org, "aged-debtors", "xlsx", **params)).content,
        "Client code",
    )
    assert len(book) == 2
    pdf = await download(client, owner, org, "aged-debtors", "pdf", **params)
    assert pdf.status_code == 200 and "Nothing to report" in pdf_text(pdf.content)


async def test_json_is_unchanged_by_the_formats(client: AsyncClient, monkeypatch) -> None:
    d = await everything(client, monkeypatch)
    plain = await call(client, d.owner, d.org, "billing-summary", **SEPTEMBER)
    explicit = await call(client, d.owner, d.org, "billing-summary", format="json", **SEPTEMBER)
    assert plain.status_code == explicit.status_code == 200
    assert plain.json() == explicit.json()
    assert plain.headers["content-type"].startswith("application/json")
    bad = await download(client, d.owner, d.org, "billing-summary", "docx", **SEPTEMBER)
    assert bad.status_code == 422
