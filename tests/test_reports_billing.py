"""Recoverability, billing summary, job profitability and VAT summary reports."""

from datetime import UTC, date, datetime

from httpx import AsyncClient

from tests.domain_factories import (
    make_client,
    make_disbursement,
    make_job,
    make_time_entry,
    practice,
    staff,
    vat_rate_id,
)
from tests.reports_helpers import (
    LineSpec,
    allocate,
    credit_note,
    make_invoice,
    payment,
    report,
    write_off,
)

SEPTEMBER = {"date_from": date(2026, 9, 1), "date_to": date(2026, 9, 30)}


async def _recovery_dataset(client: AsyncClient):
    owner, org = await practice(client, full_name="Olivia Owner")
    member = await staff(org, "staff", full_name="Sam Staff")
    alpha = await make_client(org, "Alpha Ltd", code="ALPHA")
    beta = await make_client(org, "Beta LLP", code="BETA")
    a1 = await make_job(
        org, alpha, "Accounts", number="J00001", budget_minutes=600, budget_fee=100_000
    )
    b1 = await make_job(org, beta, "VAT Q3", number="J00002")
    day = date(2026, 9, 7)
    t1 = await make_time_entry(org, owner, client=alpha, job=a1, entry_date=day)  # 12000
    t2 = await make_time_entry(org, member, client=alpha, job=a1, entry_date=day, minutes=30)
    t3 = await make_time_entry(org, owner, client=alpha, job=a1, entry_date=day, minutes=15)
    t4 = await make_time_entry(org, owner, client=alpha, job=a1, entry_date=day, minutes=25)
    t5 = await make_time_entry(org, owner, client=beta, job=b1, entry_date=day, minutes=45)
    d1 = await make_disbursement(org, alpha, 1_300, job=a1, incurred_on=day)
    # Billed 15000 for 18000 of time (a 3000 write-down) plus the disbursement at cost.
    i1 = await make_invoice(
        org,
        alpha,
        [LineSpec(15_000, kind="time", job=a1), LineSpec(1_300, kind="disbursement")],
        issue_date=date(2026, 9, 20),
    )
    await allocate(org, i1, t1, 10_000)
    await allocate(org, i1, t2, 5_000)
    await allocate(org, i1, d1, 1_300, line=1)
    await write_off(org, alpha, [t3], written_off_on=date(2026, 9, 25), job=a1)
    # A voided invoice's allocation was reversed: it does not count.
    i2 = await make_invoice(
        org, alpha, [LineSpec(5_000)], issue_date=date(2026, 9, 10), status="void"
    )
    await allocate(org, i2, t4, reversed_at=datetime.now(UTC))
    # Beta: 9000 of WIP billed at 9900 (a write-up).
    i3 = await make_invoice(org, beta, [LineSpec(9_900, job=b1)], issue_date=date(2026, 9, 15))
    await allocate(org, i3, t5, 9_900)
    # Out of the range or not issued: an August invoice, a draft and an October write-off.
    t6 = await make_time_entry(org, owner, client=alpha, job=a1, entry_date=date(2026, 8, 3))
    aug = await make_invoice(org, alpha, [LineSpec(12_000)], issue_date=date(2026, 8, 31))
    await allocate(org, aug, t6)
    t7 = await make_time_entry(org, owner, client=alpha, job=a1, entry_date=day, minutes=5)
    draft = await make_invoice(org, alpha, [LineSpec(1_000)], issue_date=None, status="draft")
    await allocate(org, draft, t7)
    t8 = await make_time_entry(org, owner, client=beta, job=b1, entry_date=day, minutes=10)
    await write_off(org, beta, [t8], written_off_on=date(2026, 10, 1))
    return owner, org, member, alpha, beta, a1, b1


def _recovery(row: dict) -> tuple:
    return (
        row["billed_wip_value"],
        row["billed_value"],
        row["billing_adjustment"],
        row["written_off_value"],
        row["wip_value"],
        row["net_adjustment"],
        row["recovery_bp"],
    )


async def test_recoverability_by_client(client: AsyncClient) -> None:
    owner, org, _, alpha, beta, *_ = await _recovery_dataset(client)
    data = await report(client, owner, org, "recoverability", **SEPTEMBER)
    assert [(r["key"], r["code"], r["name"]) for r in data["rows"]] == [
        (str(alpha.id), "ALPHA", "Alpha Ltd"),
        (str(beta.id), "BETA", "Beta LLP"),
    ]
    # 16300 billed of 22300 WIP cleared = 73.094% -> 7309.
    assert _recovery(data["rows"][0]) == (19_300, 16_300, -3_000, 3_000, 22_300, -6_000, 7_309)
    assert _recovery(data["rows"][1]) == (9_000, 9_900, 900, 0, 9_000, 900, 11_000)
    assert _recovery(data["totals"]) == (28_300, 26_200, -2_100, 3_000, 31_300, -5_100, 8_371)


async def test_recoverability_by_person_and_job(client: AsyncClient) -> None:
    owner, org, member, _, _, a1, b1 = await _recovery_dataset(client)
    by_person = await report(client, owner, org, "recoverability", group_by="person", **SEPTEMBER)
    assert [(r["key"], r["name"]) for r in by_person["rows"]] == [
        (str(owner.id), "Olivia Owner"),
        (str(member.id), "Sam Staff"),
        (None, "Disbursements"),
    ]
    # Owner: 12000 -> 10000 and 9000 -> 9900 billed, 3000 written off: 19900 / 24000.
    assert _recovery(by_person["rows"][0]) == (21_000, 19_900, -1_100, 3_000, 24_000, -4_100, 8_292)
    assert _recovery(by_person["rows"][1]) == (6_000, 5_000, -1_000, 0, 6_000, -1_000, 8_333)
    assert _recovery(by_person["rows"][2]) == (1_300, 1_300, 0, 0, 1_300, 0, 10_000)

    by_job = await report(client, owner, org, "recoverability", group_by="job", **SEPTEMBER)
    assert [(r["key"], r["code"], r["client_name"]) for r in by_job["rows"]] == [
        (str(a1.id), "J00001", "Alpha Ltd"),
        (str(b1.id), "J00002", "Beta LLP"),
    ]

    staff_only = await report(client, owner, org, "recoverability", user_id=member.id, **SEPTEMBER)
    assert _recovery(staff_only["totals"]) == (6_000, 5_000, -1_000, 0, 6_000, -1_000, 8_333)


async def test_recoverability_range_picks_issue_and_write_off_dates(client: AsyncClient) -> None:
    owner, org, *_ = await _recovery_dataset(client)
    data = await report(
        client, owner, org, "recoverability", date_from=date(2026, 8, 1), date_to=date(2026, 10, 31)
    )
    # The August invoice (12000 at value) and the October write-off (2000) join.
    assert data["totals"]["billed_wip_value"] == 28_300 + 12_000
    assert data["totals"]["written_off_value"] == 3_000 + 2_000


async def test_job_profitability(client: AsyncClient) -> None:
    owner, org, _, alpha, _, a1, _ = await _recovery_dataset(client)
    await make_time_entry(
        org, owner, client=alpha, job=a1, entry_date=date(2026, 9, 9), minutes=30, is_billable=False
    )
    await make_job(org, alpha, "Idle job")  # nothing logged or billed: no row
    data = await report(client, owner, org, "job-profitability", **SEPTEMBER)
    assert [r["job_number"] for r in data["rows"]] == ["J00001", "J00002"]
    accounts = data["rows"][0]
    # Time in September on J00001: 60 + 30 + 15 + 25 + 5 billable, 30 non-billable.
    assert accounts["logged_minutes"] == 165
    assert accounts["time_value"] == 12_000 + 6_000 + 3_000 + 5_000 + 1_000
    assert accounts["cost_value"] == 165 * 4_000 // 60
    assert accounts["billed_time_value"] == 15_000
    assert accounts["billed_disbursement_value"] == 1_300
    assert accounts["billed_value"] == 16_300
    assert accounts["written_off_value"] == 3_000
    assert accounts["margin"] == 15_000 - 11_000
    assert accounts["margin_bp"] == 2_667  # 4000 / 15000
    assert accounts["budget_minutes"] == 600
    assert accounts["budget_minutes_used_bp"] == 2_750  # 165 / 600
    assert accounts["budget_fee"] == 100_000
    assert accounts["budget_fee_used_bp"] == 2_700  # 27000 / 100000
    assert accounts["client_name"] == "Alpha Ltd"
    vat = data["rows"][1]
    assert (vat["billed_time_value"], vat["budget_minutes_used_bp"]) == (9_900, None)
    assert data["totals"]["billed_value"] == 16_300 + 9_900
    # Costs: 11000 on J00001, 3000 + 667 (45 and 10 minutes) on J00002.
    assert data["totals"]["cost_value"] == 14_667
    assert data["totals"]["margin"] == 15_000 + 9_900 - 14_667

    # All time (no range): the August time and invoice join.
    lifetime = await report(client, owner, org, "job-profitability", job_id=a1.id)
    assert lifetime["rows"][0]["billed_time_value"] == 15_000 + 12_000
    assert lifetime["filters"]["date_from"] is None


async def test_job_profitability_hides_cost_without_rate_read(client: AsyncClient) -> None:
    _, org, *_ = await _recovery_dataset(client)
    billing = await staff(org, "billing")
    data = await report(client, billing, org, "job-profitability", **SEPTEMBER)
    row = data["rows"][0]
    assert (row["cost_value"], row["margin"], row["margin_bp"]) == (None, None, None)
    assert row["billed_value"] == 16_300
    assert (data["totals"]["cost_value"], data["totals"]["margin"]) == (None, None)


async def _billing_dataset(client: AsyncClient):
    owner, org = await practice(client)
    partner = await staff(org, "partner", full_name="Pat Partner")
    alpha = await make_client(org, "Alpha Ltd", partner_user_id=partner.id)
    beta = await make_client(org, "Beta LLP")
    standard, zero = await vat_rate_id(org, "standard"), await vat_rate_id(org, "zero")
    reduced = await vat_rate_id(org, "reduced")
    await make_invoice(
        org, alpha, [LineSpec(1_000, vat_rate_id=standard)], issue_date=date(2026, 8, 5)
    )
    # Two lines of 10.03 at 20%: VAT is rounded per line (2.01 + 2.01), not on the total.
    sep = await make_invoice(
        org,
        alpha,
        [LineSpec(1_003, vat_rate_id=standard), LineSpec(1_003, vat_rate_id=standard)],
        issue_date=date(2026, 9, 3),
    )
    await make_invoice(
        org,
        beta,
        [LineSpec(500, vat_bp=0, vat_rate_id=zero), LineSpec(333, vat_bp=500, vat_rate_id=reduced)],
        issue_date=date(2026, 9, 20),
    )
    await make_invoice(org, beta, [LineSpec(9_999)], issue_date=date(2026, 9, 10), status="void")
    await make_invoice(org, beta, [LineSpec(8_888)], issue_date=None, status="draft")
    await make_invoice(org, beta, [LineSpec(7_777)], issue_date=date(2026, 11, 1))
    await credit_note(org, sep, [(200, 2000)], issue_date=date(2026, 9, 25), vat_rate_id=standard)
    await payment(org, alpha, 1_200, received_on=date(2026, 9, 30), allocations=[(sep, 1_200)])
    await payment(org, beta, 600, received_on=date(2026, 10, 2))  # on account
    await payment(org, beta, 5_000, received_on=date(2026, 9, 12), voided=True)
    return owner, org, partner, alpha, beta, standard, zero, reduced


async def test_billing_summary_by_month(client: AsyncClient) -> None:
    owner, org, *_ = await _billing_dataset(client)
    data = await report(
        client,
        owner,
        org,
        "billing-summary",
        date_from=date(2026, 8, 1),
        date_to=date(2026, 10, 31),
    )
    rows = {r["key"]: r for r in data["rows"]}
    assert [r["key"] for r in data["rows"]] == ["2026-08", "2026-09", "2026-10"]
    assert rows["2026-08"]["name"] == "Aug 2026"
    assert (rows["2026-08"]["invoiced_net"], rows["2026-08"]["invoiced_gross"]) == (1_000, 1_200)
    sep = rows["2026-09"]
    assert (sep["invoice_count"], sep["invoiced_net"], sep["invoiced_vat"]) == (2, 2_839, 419)
    assert sep["invoiced_gross"] == 3_258
    assert (sep["credit_note_count"], sep["credited_net"], sep["credited_gross"]) == (1, 200, 240)
    assert (sep["net_billed"], sep["receipts"]) == (2_639, 1_200)
    oct_ = rows["2026-10"]
    assert (oct_["invoice_count"], oct_["invoiced_net"], oct_["receipts"]) == (0, 0, 600)
    assert data["totals"] == {
        "invoice_count": 3,
        "invoiced_net": 3_839,
        "invoiced_vat": 619,
        "invoiced_gross": 4_458,
        "credit_note_count": 1,
        "credited_net": 200,
        "credited_vat": 40,
        "credited_gross": 240,
        "net_billed": 3_639,
        "receipts": 1_800,
    }


async def test_billing_summary_by_partner_and_filters(client: AsyncClient) -> None:
    owner, org, partner, alpha, *_ = await _billing_dataset(client)
    data = await report(client, owner, org, "billing-summary", group_by="partner", **SEPTEMBER)
    assert [(r["key"], r["name"]) for r in data["rows"]] == [
        (str(partner.id), "Pat Partner"),
        (None, "No partner"),
    ]
    pat, none = data["rows"]
    assert (pat["invoiced_net"], pat["credited_net"], pat["receipts"]) == (2_006, 200, 1_200)
    assert (none["invoiced_net"], none["receipts"]) == (833, 0)

    only = await report(
        client, owner, org, "billing-summary", partner_user_id=partner.id, **SEPTEMBER
    )
    assert only["totals"]["invoiced_net"] == 2_006
    by_client = await report(client, owner, org, "billing-summary", client_id=alpha.id, **SEPTEMBER)
    assert by_client["totals"]["invoiced_net"] == 2_006


async def test_vat_summary(client: AsyncClient) -> None:
    owner, org, _, alpha, _, standard, zero, reduced = await _billing_dataset(client)
    data = await report(client, owner, org, "vat-summary", **SEPTEMBER)
    assert [(r["vat_rate_id"], r["name"], r["code"], r["rate_bp"]) for r in data["rows"]] == [
        (str(standard), "Standard 20%", "standard", 2_000),
        (str(reduced), "Reduced 5%", "reduced", 500),
        (str(zero), "Zero rated", "zero", 0),
    ]
    std, red, zer = data["rows"]
    assert (std["invoiced_net"], std["invoiced_vat"]) == (2_006, 402)
    assert (std["credited_net"], std["credited_vat"]) == (200, 40)
    assert (std["net"], std["vat"]) == (1_806, 362)
    assert (red["invoiced_net"], red["invoiced_vat"], red["vat"]) == (333, 17, 17)  # 16.65 -> 17
    assert (zer["net"], zer["vat"]) == (500, 0)
    assert data["totals"] == {
        "invoiced_net": 2_839,
        "invoiced_vat": 419,
        "credited_net": 200,
        "credited_vat": 40,
        "net": 2_639,
        "vat": 379,
    }
    alpha_only = await report(client, owner, org, "vat-summary", client_id=alpha.id, **SEPTEMBER)
    assert [r["code"] for r in alpha_only["rows"]] == ["standard"]


async def test_vat_summary_line_without_rate(client: AsyncClient) -> None:
    owner, org = await practice(client)
    acme = await make_client(org, "Acme")
    await make_invoice(org, acme, [LineSpec(1_000, vat_bp=0)], issue_date=date(2026, 9, 1))
    data = await report(client, owner, org, "vat-summary", **SEPTEMBER)
    assert [(r["vat_rate_id"], r["name"], r["rate_bp"]) for r in data["rows"]] == [
        (None, "No VAT rate", 0)
    ]
