"""The WIP ledger and write-offs without an invoice."""

from datetime import date, timedelta

from sqlalchemy import text

from app.db.session import get_sessionmaker
from tests.billing_helpers import billing_status, count_rows, draft, hourly_job, new_practice
from tests.domain_factories import make_client, make_disbursement, make_time_entry, staff
from tests.factories import audit_entries, error

TODAY = date.today()


async def test_summary_values_hours_and_age_buckets(client):
    p = await new_practice(client)
    me = p.owner
    job = await hourly_job(p)
    # 90 minutes at £120/h = £180, 20 days old
    await make_time_entry(
        p.org, me, client=p.client, job=job, minutes=90, entry_date=TODAY - timedelta(days=20)
    )
    # 45 at £100/h = £75, 45 days old
    await make_time_entry(
        p.org,
        me,
        client=p.client,
        minutes=45,
        charge_rate=10_000,
        entry_date=TODAY - timedelta(days=45),
    )
    # £13 disbursement, 75 days old; £50 one 120 days old
    await make_disbursement(p.org, p.client, 1_300, incurred_on=TODAY - timedelta(days=75))
    await make_disbursement(p.org, p.client, 5_000, incurred_on=TODAY - timedelta(days=120))
    # Not WIP: non-billable, not approved, already on a draft, non-billable disbursement.
    await make_time_entry(p.org, me, client=p.client, is_billable=False)
    await make_time_entry(
        p.org,
        me,
        client=p.client,
        entry_date=TODAY - timedelta(days=200),
        timesheet_status="submitted",
    )
    await make_time_entry(p.org, me, client=p.client, billing_status="in_draft")
    await make_disbursement(p.org, p.client, 999, is_billable=False)

    response = await client.get(p.url("/wip"), headers=me.headers)
    assert response.status_code == 200, response.text
    [row] = response.json()["data"]
    assert row["client"]["name"] == "Widgets Ltd"
    assert row["minutes"] == 135 and row["hours"] == "2.25"
    assert row["time_value"] == 18_000 + 7_500
    assert row["disbursement_value"] == 6_300
    assert row["total"] == 31_800
    assert row["oldest_date"] == (TODAY - timedelta(days=120)).isoformat()
    assert row["ages"] == {
        "days_0_30": 18_000,
        "days_31_60": 7_500,
        "days_61_90": 1_300,
        "days_91_plus": 5_000,
    }
    assert row["item_count"] == 4


async def test_without_approval_requirement_unapproved_time_is_wip(client):
    p = await new_practice(client, require_timesheet_approval=False)
    await make_time_entry(p.org, p.owner, client=p.client, timesheet_status="open")
    [row] = (await client.get(p.url("/wip"), headers=p.owner.headers)).json()["data"]
    assert row["time_value"] == 12_000


async def test_summary_filters_and_pagination(client):
    p = await new_practice(client)
    partner = await staff(p.org, "partner")
    other = await make_client(p.org, "Alpha Ltd", partner_user_id=partner.id)
    job = await hourly_job(p)
    await make_time_entry(p.org, p.owner, client=p.client, job=job)
    await make_time_entry(p.org, p.owner, client=p.client)
    await make_time_entry(p.org, p.owner, client=other)

    rows = (await client.get(p.url("/wip"), headers=p.owner.headers)).json()["data"]
    assert [r["client"]["name"] for r in rows] == ["Alpha Ltd", "Widgets Ltd"]

    by_partner = await client.get(
        p.url("/wip"), headers=p.owner.headers, params={"partner_user_id": str(partner.id)}
    )
    assert [r["client"]["name"] for r in by_partner.json()["data"]] == ["Alpha Ltd"]
    by_job = await client.get(
        p.url("/wip"), headers=p.owner.headers, params={"job_id": str(job.id)}
    )
    [only] = by_job.json()["data"]
    assert only["time_value"] == 12_000
    by_client = await client.get(
        p.url("/wip"), headers=p.owner.headers, params={"client_id": str(other.id)}
    )
    assert len(by_client.json()["data"]) == 1

    first = (await client.get(p.url("/wip"), headers=p.owner.headers, params={"limit": 1})).json()
    assert first["meta"]["has_more"] is True
    second = await client.get(
        p.url("/wip"),
        headers=p.owner.headers,
        params={"limit": 1, "cursor": first["meta"]["next_cursor"]},
    )
    assert [r["client"]["name"] for r in second.json()["data"]] == ["Widgets Ltd"]
    assert second.json()["meta"]["has_more"] is False


async def test_client_items(client):
    p = await new_practice(client)
    worker = await staff(p.org, "staff", full_name="Grace Hopper")
    job = await hourly_job(p, name="Accounts 2026")
    entry = await make_time_entry(
        p.org,
        worker,
        client=p.client,
        job=job,
        minutes=30,
        description="Bank rec",
        entry_date=date(2026, 9, 8),
    )
    disbursement = await make_disbursement(
        p.org, p.client, 2_500, incurred_on=date(2026, 9, 7), supplier="Royal Mail"
    )
    response = await client.get(p.url(f"/clients/{p.client.id}/wip"), headers=p.owner.headers)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert [i["id"] for i in data["items"]] == [str(disbursement.id), str(entry.id)]
    time_item = data["items"][1]
    assert time_item["person"]["full_name"] == "Grace Hopper"
    assert time_item["job"]["name"] == "Accounts 2026"
    assert (time_item["minutes"], time_item["rate"], time_item["value"]) == (30, 12_000, 6_000)
    assert data["items"][0]["supplier"] == "Royal Mail"
    assert data["totals"] == {
        "minutes": 30,
        "hours": "0.50",
        "time_value": 6_000,
        "disbursement_value": 2_500,
        "total": 8_500,
    }


async def test_permissions(client):
    p = await new_practice(client)
    entry = await make_time_entry(p.org, p.owner, client=p.client)
    staff_member = await staff(p.org, "staff")
    viewer = await staff(p.org, "viewer")
    manager = await staff(p.org, "manager")
    partner = await staff(p.org, "partner")
    assert (await client.get(p.url("/wip"), headers=staff_member.headers)).status_code == 403
    items_url = p.url(f"/clients/{p.client.id}/wip")
    assert (await client.get(items_url, headers=staff_member.headers)).status_code == 403
    assert (await client.get(p.url("/wip"), headers=viewer.headers)).status_code == 200
    body = {
        "client_id": str(p.client.id),
        "items": [{"time_entry_id": str(entry.id)}],
        "reason": "Training time",
    }
    for denied in (viewer, manager):
        response = await client.post(p.url("/wip/write-offs"), headers=denied.headers, json=body)
        assert response.status_code == 403
    response = await client.post(p.url("/wip/write-offs"), headers=partner.headers, json=body)
    assert response.status_code == 201, response.text
    write_off_id = response.json()["data"]["id"]
    assert (await client.get(p.url("/wip/write-offs"), headers=viewer.headers)).status_code == 200
    denied = await client.delete(p.url(f"/wip/write-offs/{write_off_id}"), headers=manager.headers)
    assert denied.status_code == 403


async def test_write_off_items_and_reverse(client):
    p = await new_practice(client)
    entry = await make_time_entry(p.org, p.owner, client=p.client, minutes=90)
    disbursement = await make_disbursement(p.org, p.client, 1_300)
    keep = await make_time_entry(p.org, p.owner, client=p.client)
    response = await client.post(
        p.url("/wip/write-offs"),
        headers=p.owner.headers,
        json={
            "client_id": str(p.client.id),
            "items": [{"time_entry_id": str(entry.id)}, {"disbursement_id": str(disbursement.id)}],
            "reason": "Agreed with the client",
            "written_off_on": "2026-09-30",
        },
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["total_wip_value"] == 18_000 + 1_300
    assert data["item_count"] == 2 and data["written_off_on"] == "2026-09-30"
    assert await billing_status("time_entries", [entry.id, keep.id]) == {
        entry.id: "written_off",
        keep.id: "unbilled",
    }
    assert (await billing_status("disbursements", [disbursement.id]))[disbursement.id] == (
        "written_off"
    )
    assert (
        await count_rows(
            "SELECT count(*) FROM wip_allocations WHERE write_off_id = :id AND billed_value = 0",
            id=data["id"],
        )
        == 2
    )
    # It left the WIP ledger.
    [row] = (await client.get(p.url("/wip"), headers=p.owner.headers)).json()["data"]
    assert row["total"] == 12_000

    listed = (await client.get(p.url("/wip/write-offs"), headers=p.owner.headers)).json()["data"]
    assert [(w["id"], w["item_count"]) for w in listed] == [(data["id"], 2)]
    detail = (
        await client.get(p.url(f"/wip/write-offs/{data['id']}"), headers=p.owner.headers)
    ).json()["data"]
    assert {i["id"] for i in detail["items"]} == {str(entry.id), str(disbursement.id)}

    reversed_ = await client.delete(p.url(f"/wip/write-offs/{data['id']}"), headers=p.owner.headers)
    assert reversed_.status_code == 200
    assert (await billing_status("time_entries", [entry.id]))[entry.id] == "unbilled"
    assert (await billing_status("disbursements", [disbursement.id]))[disbursement.id] == "unbilled"
    assert await count_rows("SELECT count(*) FROM wip_allocations") == 0
    again = await client.delete(p.url(f"/wip/write-offs/{data['id']}"), headers=p.owner.headers)
    assert again.status_code == 404
    actions = [e["action"] for e in await audit_entries(p.id) if e["action"].startswith("wip.")]
    assert actions == ["wip.written_off", "wip.write_off_reversed"]


async def test_write_off_a_whole_job(client):
    p = await new_practice(client)
    job = await hourly_job(p)
    a = await make_time_entry(p.org, p.owner, client=p.client, job=job)
    b = await make_time_entry(p.org, p.owner, client=p.client, job=job, minutes=30)
    d = await make_disbursement(p.org, p.client, 700, job=job)
    elsewhere = await make_time_entry(p.org, p.owner, client=p.client)
    response = await client.post(
        p.url("/wip/write-offs"),
        headers=p.owner.headers,
        json={"client_id": str(p.client.id), "job_id": str(job.id), "reason": "Abortive work"},
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["job_id"] == str(job.id)
    assert data["total_wip_value"] == 12_000 + 6_000 + 700
    assert date.fromisoformat(data["written_off_on"])  # defaults to today
    statuses = await billing_status("time_entries", [a.id, b.id, elsewhere.id])
    assert statuses == {a.id: "written_off", b.id: "written_off", elsewhere.id: "unbilled"}
    assert (await billing_status("disbursements", [d.id]))[d.id] == "written_off"
    # Nothing left on the job.
    empty = await client.post(
        p.url("/wip/write-offs"),
        headers=p.owner.headers,
        json={"client_id": str(p.client.id), "job_id": str(job.id), "reason": "Again"},
    )
    assert empty.status_code == 409 and error(empty)["code"] == "WIP_NOT_AVAILABLE"


async def test_write_off_rules(client):
    p = await new_practice(client)
    other_client = await make_client(p.org, "Other Ltd")
    theirs = await make_time_entry(p.org, p.owner, client=other_client)
    drafted = await make_time_entry(p.org, p.owner, client=p.client)
    await draft(client, p.owner, p.org, p.client.id, time=[drafted])
    pending = await make_time_entry(
        p.org, p.owner, client=p.client, entry_date=date(2026, 8, 3), timesheet_status="submitted"
    )
    fine = await make_time_entry(p.org, p.owner, client=p.client)
    url = p.url("/wip/write-offs")

    def body(**extra):
        return {"client_id": str(p.client.id), "reason": "Write off", **extra}

    mismatch = await client.post(
        url, headers=p.owner.headers, json=body(items=[{"time_entry_id": str(theirs.id)}])
    )
    assert mismatch.status_code == 422 and error(mismatch)["code"] == "CLIENT_MISMATCH"
    for item in (drafted, pending):
        response = await client.post(
            url, headers=p.owner.headers, json=body(items=[{"time_entry_id": str(item.id)}])
        )
        assert response.status_code == 409 and error(response)["code"] == "WIP_NOT_AVAILABLE"
    assert error(response)["details"][0]["reason"] == "timesheet_not_approved"
    for bad in (
        body(),
        body(items=[{"time_entry_id": str(fine.id)}], job_id=str(fine.id)),
        body(items=[{}]),
        body(items=[{"time_entry_id": str(fine.id), "disbursement_id": str(fine.id)}]),
        body(items=[{"time_entry_id": str(fine.id)}], reason=""),
        body(items=[{"time_entry_id": str(fine.id)}, {"time_entry_id": str(fine.id)}]),
    ):
        response = await client.post(url, headers=p.owner.headers, json=bad)
        assert response.status_code == 422, bad
    # Nothing was written off by the failed attempts.
    assert await count_rows("SELECT count(*) FROM wip_write_offs") == 0


async def test_reverse_refused_once_items_moved_on(client):
    p = await new_practice(client)
    entry = await make_time_entry(p.org, p.owner, client=p.client)
    response = await client.post(
        p.url("/wip/write-offs"),
        headers=p.owner.headers,
        json={
            "client_id": str(p.client.id),
            "items": [{"time_entry_id": str(entry.id)}],
            "reason": "x",
        },
    )
    write_off_id = response.json()["data"]["id"]
    async with get_sessionmaker()() as session:
        await session.execute(
            text("UPDATE time_entries SET billing_status = 'billed' WHERE id = :id"),
            {"id": str(entry.id)},
        )
        await session.commit()
    refused = await client.delete(p.url(f"/wip/write-offs/{write_off_id}"), headers=p.owner.headers)
    assert refused.status_code == 409 and error(refused)["code"] == "WRITE_OFF_BILLED"


async def test_other_practice_is_404(client):
    p = await new_practice(client)
    other = await new_practice(client, "Other Practice")
    theirs = await make_time_entry(other.org, other.owner, client=other.client)
    items = await client.get(p.url(f"/clients/{other.client.id}/wip"), headers=p.owner.headers)
    assert items.status_code == 404
    response = await client.post(
        p.url("/wip/write-offs"),
        headers=p.owner.headers,
        json={
            "client_id": str(p.client.id),
            "items": [{"time_entry_id": str(theirs.id)}],
            "reason": "x",
        },
    )
    assert response.status_code == 404
    write_off = await client.post(
        other.url("/wip/write-offs"),
        headers=other.owner.headers,
        json={
            "client_id": str(other.client.id),
            "items": [{"time_entry_id": str(theirs.id)}],
            "reason": "x",
        },
    )
    write_off_id = write_off.json()["data"]["id"]
    for method in ("get", "delete"):
        response = await client.request(
            method, p.url(f"/wip/write-offs/{write_off_id}"), headers=p.owner.headers
        )
        assert response.status_code == 404
    # The other practice's WIP never shows up in this one.
    assert (await client.get(p.url("/wip"), headers=p.owner.headers)).json()["data"] == []
