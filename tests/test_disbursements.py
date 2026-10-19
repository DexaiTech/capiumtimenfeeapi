"""Disbursements: recording, permissions, the unbilled-only rule and tenancy."""

from datetime import date

from tests.billing_helpers import draft, hourly_job, new_practice, org_url
from tests.domain_factories import make_client, make_disbursement, staff, vat_rate_id
from tests.factories import add_member, audit_entries, error


def body(client, **extra):
    return {
        "client_id": str(client.id),
        "incurred_on": "2026-09-10",
        "description": "Companies House confirmation statement",
        "supplier": "Companies House",
        "amount": 3400,
        **extra,
    }


async def test_record_get_update_delete(client):
    p = await new_practice(client)
    job = await hourly_job(p)
    url = p.url("/disbursements")
    response = await client.post(
        url, headers=p.owner.headers, json=body(p.client, job_id=str(job.id))
    )
    assert response.status_code == 201, response.text
    created = response.json()["data"]
    assert created["amount"] == 3400
    assert created["billing_status"] == "unbilled"
    assert created["job_id"] == str(job.id)
    assert created["created_by"] == str(p.owner.id)

    got = await client.get(f"{url}/{created['id']}", headers=p.owner.headers)
    assert got.json()["data"]["description"].startswith("Companies House")

    reduced = await vat_rate_id(p.org, "zero")
    patched = await client.patch(
        f"{url}/{created['id']}",
        headers=p.owner.headers,
        json={"amount": 1300, "supplier": "", "vat_rate_id": str(reduced)},
    )
    assert patched.status_code == 200, patched.text
    data = patched.json()["data"]
    assert (data["amount"], data["supplier"], data["vat_rate_id"]) == (1300, None, str(reduced))

    listed = await client.get(url, headers=p.owner.headers)
    assert [d["id"] for d in listed.json()["data"]] == [created["id"]]

    deleted = await client.delete(f"{url}/{created['id']}", headers=p.owner.headers)
    assert deleted.status_code == 200
    assert (await client.get(f"{url}/{created['id']}", headers=p.owner.headers)).status_code == 404

    actions = [e["action"] for e in await audit_entries(p.id) if e["target_type"] == "disbursement"]
    assert actions == ["disbursement.created", "disbursement.updated", "disbursement.deleted"]
    updated = (await audit_entries(p.id, "disbursement.updated"))[0]["metadata"]
    assert updated["changes"]["amount"] == [3400, 1300]


async def test_validation(client):
    p = await new_practice(client)
    url = p.url("/disbursements")
    for bad in (
        body(p.client, amount=0),
        body(p.client, amount=-5),
        body(p.client, description=""),
        body(p.client, unknown="x"),
        {k: v for k, v in body(p.client).items() if k != "incurred_on"},
    ):
        response = await client.post(url, headers=p.owner.headers, json=bad)
        assert response.status_code == 422, bad
    created = (await client.post(url, headers=p.owner.headers, json=body(p.client))).json()
    null_amount = await client.patch(
        f"{url}/{created['data']['id']}", headers=p.owner.headers, json={"amount": None}
    )
    assert null_amount.status_code == 422


async def test_job_must_be_the_clients(client):
    p = await new_practice(client)
    other = await make_client(p.org, "Other Ltd")
    other_job = await hourly_job(p, other)
    response = await client.post(
        p.url("/disbursements"),
        headers=p.owner.headers,
        json=body(p.client, job_id=str(other_job.id)),
    )
    assert response.status_code == 422
    assert error(response)["details"][0]["field"] == "job_id"


async def test_unknown_vat_rate_is_rejected(client):
    p = await new_practice(client)
    other = await new_practice(client, "Other Practice")
    foreign_rate = await vat_rate_id(other.org)
    response = await client.post(
        p.url("/disbursements"),
        headers=p.owner.headers,
        json=body(p.client, vat_rate_id=str(foreign_rate)),
    )
    assert response.status_code == 422


async def test_inactive_and_archived_clients_refuse_new_disbursements(client):
    p = await new_practice(client)
    for status in ("inactive", "archived"):
        dormant = await make_client(p.org, f"{status} Ltd", status=status)
        response = await client.post(
            p.url("/disbursements"), headers=p.owner.headers, json=body(dormant)
        )
        assert response.status_code == 409
        assert error(response)["code"] == "CLIENT_INACTIVE"
    prospect = await make_client(p.org, "Prospect Ltd", status="prospect")
    ok = await client.post(p.url("/disbursements"), headers=p.owner.headers, json=body(prospect))
    assert ok.status_code == 201


async def test_staff_see_and_change_only_their_own(client):
    p = await new_practice(client)
    alice = await staff(p.org, "staff")
    bob = await staff(p.org, "staff")
    url = p.url("/disbursements")
    mine = (await client.post(url, headers=alice.headers, json=body(p.client))).json()["data"]
    theirs = (await client.post(url, headers=bob.headers, json=body(p.client))).json()["data"]

    listed = await client.get(url, headers=alice.headers)
    assert [d["id"] for d in listed.json()["data"]] == [mine["id"]]
    assert (await client.get(f"{url}/{mine['id']}", headers=alice.headers)).status_code == 200
    assert (await client.get(f"{url}/{theirs['id']}", headers=alice.headers)).status_code == 403
    edit = await client.patch(f"{url}/{theirs['id']}", headers=alice.headers, json={"amount": 1})
    assert edit.status_code == 403
    assert (await client.delete(f"{url}/{theirs['id']}", headers=alice.headers)).status_code == 403
    own_edit = await client.patch(f"{url}/{mine['id']}", headers=alice.headers, json={"amount": 9})
    assert own_edit.status_code == 200

    # Billing has disbursement.manage and wip.read: sees and edits anyone's.
    billing = await staff(p.org, "billing")
    assert len((await client.get(url, headers=billing.headers)).json()["data"]) == 2
    other_edit = await client.patch(
        f"{url}/{theirs['id']}", headers=billing.headers, json={"amount": 5}
    )
    assert other_edit.status_code == 200


async def test_roles_without_disbursement_permissions(client):
    p = await new_practice(client)
    viewer = await staff(p.org, "viewer")
    url = p.url("/disbursements")
    assert (await client.post(url, headers=viewer.headers, json=body(p.client))).status_code == 403
    row = await make_disbursement(p.org, p.client)
    # Viewer has wip.read: may read but not change.
    assert (await client.get(f"{url}/{row.id}", headers=viewer.headers)).status_code == 200
    patched = await client.patch(f"{url}/{row.id}", headers=viewer.headers, json={"amount": 1})
    assert patched.status_code == 403


async def test_manage_without_wip_read_lists_own(client):
    """A custom role with disbursement.manage but neither wip.read nor disbursement.log
    reads its own disbursements in the list as it does by id."""
    p = await new_practice(client)
    created = await client.post(
        p.url("/roles"),
        headers=p.owner.headers,
        json={"name": "Expenses", "permissions": ["organization.read", "disbursement.manage"]},
    )
    assert created.status_code == 201, created.text
    clerk = await add_member(p.org["id"], "Expenses")
    url = p.url("/disbursements")
    mine = await client.post(url, headers=clerk.headers, json=body(p.client))
    assert mine.status_code == 201, mine.text
    await make_disbursement(p.org, p.client)  # someone else's
    listed = await client.get(url, headers=clerk.headers)
    assert listed.status_code == 200, listed.text
    assert [d["id"] for d in listed.json()["data"]] == [mine.json()["data"]["id"]]


async def test_filters(client):
    p = await new_practice(client)
    other = await make_client(p.org, "Other Ltd")
    await make_disbursement(p.org, p.client, incurred_on=date(2026, 8, 1))
    september = await make_disbursement(p.org, p.client, incurred_on=date(2026, 9, 1))
    await make_disbursement(p.org, other, incurred_on=date(2026, 9, 2))
    await make_disbursement(p.org, p.client, incurred_on=date(2026, 9, 3), is_billable=False)
    url = p.url("/disbursements")
    response = await client.get(
        url,
        headers=p.owner.headers,
        params={
            "client_id": str(p.client.id),
            "date_from": "2026-09-01",
            "is_billable": "true",
        },
    )
    assert [d["id"] for d in response.json()["data"]] == [str(september.id)]
    paged = await client.get(url, headers=p.owner.headers, params={"limit": 2})
    first = paged.json()
    assert len(first["data"]) == 2 and first["meta"]["has_more"] is True
    rest = await client.get(
        url, headers=p.owner.headers, params={"limit": 2, "cursor": first["meta"]["next_cursor"]}
    )
    assert len(rest.json()["data"]) == 2
    seen = {d["id"] for d in first["data"]} | {d["id"] for d in rest.json()["data"]}
    assert len(seen) == 4


async def test_only_unbilled_disbursements_can_change(client):
    p = await new_practice(client)
    row = await make_disbursement(p.org, p.client)
    await draft(client, p.owner, p.org, p.client.id, disbursements=[row])
    url = org_url(p.org, f"/disbursements/{row.id}")
    patched = await client.patch(url, headers=p.owner.headers, json={"amount": 1})
    assert patched.status_code == 409
    assert error(patched)["code"] == "DISBURSEMENT_LOCKED"
    deleted = await client.delete(url, headers=p.owner.headers)
    assert error(deleted)["code"] == "DISBURSEMENT_LOCKED"


async def test_moving_to_another_client_clears_the_job(client):
    p = await new_practice(client)
    job = await hourly_job(p)
    other = await make_client(p.org, "Other Ltd")
    row = await make_disbursement(p.org, p.client, job=job)
    response = await client.patch(
        org_url(p.org, f"/disbursements/{row.id}"),
        headers=p.owner.headers,
        json={"client_id": str(other.id)},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["client_id"] == str(other.id)
    assert response.json()["data"]["job_id"] is None


async def test_other_practice_answers_404(client):
    p = await new_practice(client)
    other = await new_practice(client, "Other Practice")
    theirs = await make_disbursement(other.org, other.client)
    url = org_url(p.org, f"/disbursements/{theirs.id}")
    assert (await client.get(url, headers=p.owner.headers)).status_code == 404
    patch = await client.patch(url, headers=p.owner.headers, json={"amount": 1})
    assert patch.status_code == 404
    assert (await client.delete(url, headers=p.owner.headers)).status_code == 404
    # Another practice's client is not found either.
    response = await client.post(
        p.url("/disbursements"), headers=p.owner.headers, json=body(other.client)
    )
    assert response.status_code == 404
    # And a non-member cannot reach the practice at all.
    listed = await client.get(org_url(other.org, "/disbursements"), headers=p.owner.headers)
    assert listed.status_code == 404
