"""POST /invoices/bulk: delete up to 200 drafts, all or nothing; issued invoices are locked."""

import uuid
from datetime import date

from httpx import AsyncClient

from tests.billing_helpers import (
    billing_status,
    count_rows,
    draft,
    hourly_job,
    issued,
    new_practice,
    org_url,
)
from tests.domain_factories import make_time_entry
from tests.factories import audit_entries
from tests.job_helpers import role_without


async def _bulk(client: AsyncClient, actor, org, ids, status=200, **extra):
    response = await client.post(
        org_url(org, "/invoices/bulk"),
        headers=actor.headers,
        json={"invoice_ids": [str(i) for i in ids], "action": "delete", **extra},
    )
    assert response.status_code == status, response.text
    return response.json()["data"] if status == 200 else response.json()["error"]


async def _draft_with_time(client: AsyncClient, p, job, day: int):
    entry = await make_time_entry(
        p.org, p.owner, client=p.client, job=job, entry_date=date(2026, 9, day)
    )
    invoice = await draft(client, p.owner, p.org, p.client.id, time=[entry])
    return invoice, entry


async def test_delete_drafts_releases_their_wip(client: AsyncClient) -> None:
    p = await new_practice(client)
    job = await hourly_job(p)
    one, e1 = await _draft_with_time(client, p, job, 1)
    two, e2 = await _draft_with_time(client, p, job, 2)
    assert set((await billing_status("time_entries", [e1.id, e2.id])).values()) == {"in_draft"}
    result = await _bulk(client, p.owner, p.org, [one["id"], two["id"]])
    assert result == {"action": "delete", "affected": 2, "changed": 2}
    assert set((await billing_status("time_entries", [e1.id, e2.id])).values()) == {"unbilled"}
    assert await count_rows("SELECT count(*) FROM invoices WHERE organization_id = :o", o=p.id) == 0
    rows = await audit_entries(p.id, "invoice.draft_deleted")
    assert len(rows) == 2
    assert all(r["metadata"]["bulk"] is True and r["metadata"]["item_count"] == 1 for r in rows)


async def test_an_issued_invoice_refuses_the_whole_selection(client: AsyncClient) -> None:
    p = await new_practice(client)
    job = await hourly_job(p)
    one, _ = await _draft_with_time(client, p, job, 1)
    two, _ = await _draft_with_time(client, p, job, 2)
    locked = await issued(client, p.owner, p.org, two["id"])
    error = await _bulk(client, p.owner, p.org, [one["id"], two["id"]], 409)
    assert error["code"] == "INVOICE_NOT_DRAFT"
    assert [d["invoice_id"] for d in error["details"]] == [two["id"]]
    assert error["details"][0]["number"] == locked["number"]
    assert error["details"][0]["status"] == "issued"
    assert await count_rows("SELECT count(*) FROM invoices WHERE organization_id = :o", o=p.id) == 2
    assert await audit_entries(p.id, "invoice.draft_deleted") == []


async def test_unknown_ids_and_bad_bodies(client: AsyncClient) -> None:
    p = await new_practice(client)
    job = await hourly_job(p)
    one, _ = await _draft_with_time(client, p, job, 1)
    ghost = str(uuid.uuid4())
    error = await _bulk(client, p.owner, p.org, [one["id"], ghost], 404)
    assert error["details"] == {"invoice_ids": [ghost]}
    await _bulk(client, p.owner, p.org, [], 422)
    await _bulk(client, p.owner, p.org, [one["id"]], 422, value="x")
    response = await client.post(
        org_url(p.org, "/invoices/bulk"),
        headers=p.owner.headers,
        json={"invoice_ids": [one["id"]], "action": "void"},
    )
    assert response.status_code == 422
    assert await count_rows("SELECT count(*) FROM invoices WHERE organization_id = :o", o=p.id) == 1


async def test_it_needs_invoice_create(client: AsyncClient) -> None:
    p = await new_practice(client)
    job = await hourly_job(p)
    one, _ = await _draft_with_time(client, p, job, 1)
    nobody = await role_without(p.org, "invoice.create")
    await _bulk(client, nobody, p.org, [one["id"]], 403)
    assert await count_rows("SELECT count(*) FROM invoices WHERE organization_id = :o", o=p.id) == 1
