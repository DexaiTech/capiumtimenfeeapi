"""Estimates: writing and pricing, sending (numbering), answering, converting to an invoice."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tests.billing_helpers import get_invoice, hourly_job, new_practice, org_url
from tests.domain_factories import make_client, vat_rate_id
from tests.factories import audit_entries, error


def london_today():
    """The practices of these tests keep London time."""
    return datetime.now(ZoneInfo("Europe/London")).date()


LINE = {"description": "Year-end accounts", "quantity": "2", "unit_amount": 50_000}


async def create(client, p, **body):
    response = await client.post(
        org_url(p.org, "/estimates"),
        headers=p.owner.headers,
        json={"client_id": str(p.client.id), "lines": [LINE], **body},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def act(client, p, estimate_id, action, **body):
    return await client.post(
        org_url(p.org, f"/estimates/{estimate_id}/{action}"),
        headers=p.owner.headers,
        json=body or None,
    )


async def test_create_prices_lines_and_starts_as_an_unnumbered_draft(client):
    p = await new_practice(client, vat_registered=True)
    standard = await vat_rate_id(p.org, "standard")
    estimate = await create(
        client, p, reference="Quote for 2026", lines=[{**LINE, "vat_rate_id": str(standard)}]
    )
    assert estimate["status"] == estimate["display_status"] == "draft"
    assert estimate["number"] is None
    [line] = estimate["lines"]
    assert (line["net_amount"], line["vat_rate_bp"], line["vat_amount"]) == (100_000, 2000, 20_000)
    assert (estimate["net_total"], estimate["vat_total"], estimate["gross_total"]) == (
        100_000,
        20_000,
        120_000,
    )
    assert estimate["client"]["id"] == str(p.client.id)
    [entry] = await audit_entries(p.id, "estimate.created")
    assert entry["target_id"] == estimate["id"]


async def test_non_vat_registered_practice_charges_no_vat(client):
    p = await new_practice(client, vat_registered=False)
    standard = await vat_rate_id(p.org, "standard")
    estimate = await create(client, p, lines=[{**LINE, "vat_rate_id": str(standard)}])
    assert estimate["vat_total"] == 0 and estimate["gross_total"] == 100_000


async def test_create_rules(client):
    p = await new_practice(client)
    url = org_url(p.org, "/estimates")
    empty = await client.post(
        url, headers=p.owner.headers, json={"client_id": str(p.client.id), "lines": []}
    )
    assert empty.status_code == 422
    other = await make_client(p.org, "Other Ltd")
    job = await hourly_job(p, other, name="Other work")
    wrong_job = await client.post(
        url,
        headers=p.owner.headers,
        json={"client_id": str(p.client.id), "lines": [{**LINE, "job_id": str(job.id)}]},
    )
    assert wrong_job.status_code == 422
    missing = await client.post(
        url,
        headers=p.owner.headers,
        json={"client_id": "00000000-0000-4000-8000-000000000000", "lines": [LINE]},
    )
    assert missing.status_code == 404


async def test_update_replaces_lines_only_while_a_draft(client):
    p = await new_practice(client, vat_registered=False)
    estimate = await create(client, p)
    url = org_url(p.org, f"/estimates/{estimate['id']}")
    response = await client.patch(
        url,
        headers=p.owner.headers,
        json={
            "notes": "  Valid for 30 days ",
            "lines": [{"description": "Advice", "unit_amount": 7_500}],
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["notes"] == "Valid for 30 days"
    assert [(row["description"], row["net_amount"]) for row in data["lines"]] == [("Advice", 7_500)]
    assert data["gross_total"] == 7_500
    nulled = await client.patch(url, headers=p.owner.headers, json={"lines": None})
    assert nulled.status_code == 422

    assert (await act(client, p, estimate["id"], "send")).status_code == 200
    locked = await client.patch(url, headers=p.owner.headers, json={"notes": "late"})
    assert locked.status_code == 409 and error(locked)["code"] == "ESTIMATE_NOT_DRAFT"


async def test_send_numbers_the_estimate_and_fixes_its_dates(client):
    p = await new_practice(client)
    first = await create(client, p)
    second = await create(client, p)
    sent = await act(
        client, p, first["id"], "send", issue_date="2026-10-05", expiry_date="2026-11-04"
    )
    data = sent.json()["data"]
    assert (data["number"], data["status"]) == ("EST00001", "sent")
    assert (data["issue_date"], data["expiry_date"]) == ("2026-10-05", "2026-11-04")
    assert data["sent_at"] is not None
    again = await act(client, p, first["id"], "send")
    assert again.status_code == 409 and error(again)["code"] == "ESTIMATE_NOT_DRAFT"
    # Numbers are gapless and sequential.
    assert (await act(client, p, second["id"], "send")).json()["data"]["number"] == "EST00002"
    [entry, _] = await audit_entries(p.id, "estimate.sent")
    assert entry["metadata"]["number"] == "EST00001"


async def test_send_refuses_an_expiry_before_the_issue_date(client):
    p = await new_practice(client)
    estimate = await create(client, p)
    response = await act(
        client, p, estimate["id"], "send", issue_date="2026-10-05", expiry_date="2026-10-01"
    )
    assert response.status_code == 422


async def test_expired_is_derived_from_the_expiry_date(client):
    p = await new_practice(client)
    estimate = await create(client, p)
    yesterday = (london_today() - timedelta(days=1)).isoformat()
    await act(
        client,
        p,
        estimate["id"],
        "send",
        issue_date=(london_today() - timedelta(days=10)).isoformat(),
        expiry_date=yesterday,
    )
    shown = await client.get(
        org_url(p.org, f"/estimates/{estimate['id']}"), headers=p.owner.headers
    )
    assert shown.json()["data"]["display_status"] == "expired"
    assert shown.json()["data"]["status"] == "sent"
    listed = await client.get(org_url(p.org, "/estimates?status=expired"), headers=p.owner.headers)
    assert [e["id"] for e in listed.json()["data"]] == [estimate["id"]]
    summary = await client.get(org_url(p.org, "/estimates/summary"), headers=p.owner.headers)
    counts = summary.json()["data"]["counts"]
    assert (counts["expired"], counts["sent"], counts["total"]) == (1, 0, 1)
    assert summary.json()["data"]["open_gross"] == 0
    # A late "yes" is still recorded.
    assert (await act(client, p, estimate["id"], "accept")).status_code == 200


async def test_accept_and_decline_only_a_sent_estimate(client):
    p = await new_practice(client)
    draft = await create(client, p)
    early = await act(client, p, draft["id"], "accept")
    assert early.status_code == 409 and error(early)["code"] == "ESTIMATE_NOT_SENT"

    yes, no = await create(client, p), await create(client, p)
    await act(client, p, yes["id"], "send")
    await act(client, p, no["id"], "send")
    accepted = (await act(client, p, yes["id"], "accept")).json()["data"]
    declined = (await act(client, p, no["id"], "decline")).json()["data"]
    assert (accepted["status"], declined["status"]) == ("accepted", "declined")
    assert accepted["responded_at"] is not None
    twice = await act(client, p, yes["id"], "decline")
    assert twice.status_code == 409 and error(twice)["code"] == "ESTIMATE_NOT_SENT"


async def test_convert_raises_a_draft_invoice_once(client):
    p = await new_practice(client, vat_registered=True)
    standard = await vat_rate_id(p.org, "standard")
    job = await hourly_job(p, name="Accounts 2026")
    estimate = await create(
        client,
        p,
        job_id=str(job.id),
        reference="Q-7",
        lines=[{**LINE, "vat_rate_id": str(standard)}],
    )
    early = await act(client, p, estimate["id"], "convert")
    assert early.status_code == 409 and error(early)["code"] == "ESTIMATE_NOT_CONVERTIBLE"

    await act(client, p, estimate["id"], "send")
    await act(client, p, estimate["id"], "accept")
    response = await act(client, p, estimate["id"], "convert")
    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["estimate"]["converted_invoice_id"] == body["invoice_id"]
    invoice = await get_invoice(client, p.owner, p.org, body["invoice_id"])
    assert invoice["status"] == "draft"
    assert invoice["reference"] == "Q-7"
    assert (invoice["net_total"], invoice["vat_total"], invoice["gross_total"]) == (
        100_000,
        20_000,
        120_000,
    )
    [line] = invoice["lines"]
    assert line["description"] == "Year-end accounts"
    assert line["job_id"] == str(job.id)  # the estimate's job fills a line without one
    again = await act(client, p, estimate["id"], "convert")
    assert again.status_code == 409 and error(again)["code"] == "ESTIMATE_ALREADY_CONVERTED"


async def test_a_declined_estimate_cannot_be_converted(client):
    p = await new_practice(client)
    estimate = await create(client, p)
    await act(client, p, estimate["id"], "send")
    await act(client, p, estimate["id"], "decline")
    response = await act(client, p, estimate["id"], "convert")
    assert response.status_code == 409 and error(response)["code"] == "ESTIMATE_NOT_CONVERTIBLE"


async def test_only_a_draft_can_be_deleted(client):
    p = await new_practice(client)
    draft, sent = await create(client, p), await create(client, p)
    await act(client, p, sent["id"], "send")
    kept = await client.delete(org_url(p.org, f"/estimates/{sent['id']}"), headers=p.owner.headers)
    assert kept.status_code == 409 and error(kept)["code"] == "ESTIMATE_NOT_DRAFT"
    gone = await client.delete(org_url(p.org, f"/estimates/{draft['id']}"), headers=p.owner.headers)
    assert gone.status_code == 200
    missing = await client.get(org_url(p.org, f"/estimates/{draft['id']}"), headers=p.owner.headers)
    assert missing.status_code == 404


async def test_list_filters_by_client_status_and_search(client):
    p = await new_practice(client)
    other = await make_client(p.org, "Gadgets plc")
    mine = await create(client, p, reference="Alpha")
    theirs = await client.post(
        org_url(p.org, "/estimates"),
        headers=p.owner.headers,
        json={"client_id": str(other.id), "lines": [LINE]},
    )
    theirs_id = theirs.json()["data"]["id"]
    await act(client, p, theirs_id, "send")

    async def ids(query):
        response = await client.get(org_url(p.org, f"/estimates{query}"), headers=p.owner.headers)
        assert response.status_code == 200, response.text
        return {e["id"] for e in response.json()["data"]}

    assert await ids("") == {mine["id"], theirs_id}
    assert await ids(f"?client_id={other.id}") == {theirs_id}
    assert await ids("?status=draft") == {mine["id"]}
    assert await ids("?status=sent&status=draft") == {mine["id"], theirs_id}
    assert await ids("?q=alpha") == {mine["id"]}
    assert await ids("?q=gadgets") == {theirs_id}
    assert await ids("?q=EST00001") == {theirs_id}
