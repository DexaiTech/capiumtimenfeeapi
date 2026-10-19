"""Services and activity types: the new fields, unique names and codes, deactivate-not-delete,
service-to-activity links and the rules time entries follow from them."""

import uuid

import pytest
from httpx import AsyncClient

from tests.domain_factories import (
    activity_code_id,
    make_client,
    make_job,
    make_time_entry,
    practice,
    service_id,
    staff,
    vat_rate_id,
)
from tests.factories import audit_entries, error
from tests.team_helpers import new_rate
from tests.time_helpers import log_time, ok_data, org_url


def services_url(org: dict, path: str = "") -> str:
    return org_url(org, f"/services{path}")


def activities_url(org: dict, path: str = "") -> str:
    return org_url(org, f"/activity-codes{path}")


async def make_service(client: AsyncClient, actor, org: dict, **body) -> dict:
    body = {"code": "NEWS", "name": "New service"} | body
    response = await client.post(services_url(org), headers=actor.headers, json=body)
    return ok_data(response, 201)


async def make_activity(client: AsyncClient, actor, org: dict, **body) -> dict:
    body = {"code": "NEWA", "name": "New activity"} | body
    response = await client.post(activities_url(org), headers=actor.headers, json=body)
    return ok_data(response, 201)


async def set_links(client: AsyncClient, actor, org: dict, service: dict, ids: list) -> dict:
    response = await client.put(
        services_url(org, f"/{service['id']}/activity-codes"),
        headers=actor.headers,
        json={"activity_code_ids": [str(i) for i in ids]},
    )
    return ok_data(response)


# --- Fields and uniqueness --------------------------------------------------------------------------


async def test_seeded_services_and_activities_carry_the_new_fields(client: AsyncClient) -> None:
    owner, org = await practice(client)
    services = ok_data(await client.get(services_url(org), headers=owner.headers))
    assert len(services) == 8
    for service in services:
        assert service["default_billable"] is True and service["invoice_description"] is None
        assert (service["status"], service["is_active"]) == ("active", True)
        assert service["activity_code_ids"] == [] and service["is_used"] is False
        assert service["can_delete"] is True
    activities = ok_data(await client.get(activities_url(org), headers=owner.headers))
    assert len(activities) == 11
    for activity in activities:
        assert activity["vat_rate_id"] is None and activity["invoice_description"] is None
        assert activity["status"] == "active" and activity["is_used"] is False
        assert activity["can_delete"] is True
    # is_chargeable is the activity type's default billable status.
    assert {a["code"]: a["is_chargeable"] for a in activities}["ADM"] is False


async def test_create_and_update_a_service_with_every_field(client: AsyncClient) -> None:
    owner, org = await practice(client)
    standard = await vat_rate_id(org, "standard")
    zero = await vat_rate_id(org, "zero")
    created = await make_service(
        client,
        owner,
        org,
        code="pay2",
        name="Payroll bureau",
        description="Monthly",
        billing_basis="fixed",
        default_fee=5_000,
        default_billable=False,
        vat_rate_id=str(standard),
        invoice_description="  Payroll for the month  ",
        sort_order=3,
        is_active=True,
    )
    assert created["code"] == "PAY2" and created["invoice_description"] == "Payroll for the month"
    assert (created["default_billable"], created["vat_rate_id"], created["default_fee"]) == (
        False,
        str(standard),
        5_000,
    )
    url = services_url(org, f"/{created['id']}")
    updated = ok_data(
        await client.patch(
            url,
            headers=owner.headers,
            json={
                "default_billable": True,
                "vat_rate_id": str(zero),
                "invoice_description": "New wording",
                "name": "Payroll bureau 2",
            },
        )
    )
    assert (
        updated["default_billable"],
        updated["vat_rate_id"],
        updated["invoice_description"],
        updated["name"],
    ) == (True, str(zero), "New wording", "Payroll bureau 2")
    cleared = ok_data(
        await client.patch(
            url,
            headers=owner.headers,
            json={
                "invoice_description": "",
                "vat_rate_id": None,
                "default_fee": None,
                "description": None,
            },
        )
    )
    assert (
        cleared["invoice_description"] is None
        and cleared["vat_rate_id"] is None
        and cleared["default_fee"] is None
    )
    for body in (
        {"name": None},
        {"default_billable": None},
        {"vat_rate_id": "00000000-0000-0000-0000-000000000000"},
        {"invoice_description": "x" * 501},
        {"code": "BAD CODE"},
    ):
        assert (await client.patch(url, headers=owner.headers, json=body)).status_code == 422, body
    off = ok_data(await client.post(f"{url}/deactivate", headers=owner.headers))
    assert (off["is_active"], off["status"]) == (False, "inactive")
    on = ok_data(await client.post(f"{url}/activate", headers=owner.headers))
    assert (on["is_active"], on["status"]) == (True, "active")
    changes = [e["metadata"]["changes"] for e in await audit_entries(org["id"], "service.updated")]
    assert "default_billable" in changes[0] and changes[0]["name"] == [
        "Payroll bureau",
        "Payroll bureau 2",
    ]


async def test_service_names_and_codes_are_unique_ignoring_case(client: AsyncClient) -> None:
    owner, org = await practice(client)
    for body, code in (
        ({"code": "accs", "name": "Brand new"}, "SERVICE_CODE_TAKEN"),
        ({"code": "FRESH", "name": "year-END accounts"}, "SERVICE_NAME_TAKEN"),
    ):
        response = await client.post(services_url(org), headers=owner.headers, json=body)
        assert response.status_code == 409 and error(response)["code"] == code
        assert error(response)["details"][0]["code"] == "duplicate"
    fresh = await make_service(client, owner, org, code="FRESH", name="Fresh")
    for body, code in (
        ({"code": "vat"}, "SERVICE_CODE_TAKEN"),
        ({"name": "VAT RETURN"}, "SERVICE_NAME_TAKEN"),
    ):
        response = await client.patch(
            services_url(org, f"/{fresh['id']}"), headers=owner.headers, json=body
        )
        assert response.status_code == 409 and error(response)["code"] == code
    # Its own values are fine; other practices are separate.
    assert (
        await client.patch(
            services_url(org, f"/{fresh['id']}"),
            headers=owner.headers,
            json={"name": "FRESH", "code": "fresh"},
        )
    ).status_code == 200
    other_owner, other = await practice(client, "Other & Co")
    assert (await make_service(client, other_owner, other, code="FRESH", name="Fresh"))[
        "code"
    ] == "FRESH"
    # An inactive service still holds its name.
    await client.post(services_url(org, f"/{fresh['id']}/deactivate"), headers=owner.headers)
    response = await client.post(
        services_url(org), headers=owner.headers, json={"code": "OTHER", "name": "fresh"}
    )
    assert response.status_code == 409


async def test_activity_types_new_fields_and_unique_names(client: AsyncClient) -> None:
    owner, org = await practice(client)
    standard = await vat_rate_id(org, "standard")
    created = await make_activity(
        client,
        owner,
        org,
        code="rev",
        name="Review",
        is_chargeable=False,
        vat_rate_id=str(standard),
        invoice_description="Review time",
    )
    assert (
        created["code"],
        created["is_chargeable"],
        created["vat_rate_id"],
        created["invoice_description"],
    ) == ("REV", False, str(standard), "Review time")
    url = activities_url(org, f"/{created['id']}")
    updated = ok_data(
        await client.patch(
            url,
            headers=owner.headers,
            json={
                "is_chargeable": True,
                "invoice_description": None,
                "vat_rate_id": None,
                "name": "Peer review",
            },
        )
    )
    assert (updated["is_chargeable"], updated["invoice_description"], updated["vat_rate_id"]) == (
        True,
        None,
        None,
    )
    for body, code in (
        ({"code": "acc"}, "ACTIVITY_CODE_TAKEN"),
        ({"name": "bookkeeping"}, "ACTIVITY_NAME_TAKEN"),
    ):
        response = await client.patch(url, headers=owner.headers, json=body)
        assert response.status_code == 409 and error(response)["code"] == code
        created_again = await client.post(
            activities_url(org),
            headers=owner.headers,
            json={"code": body.get("code", "NEW2"), "name": body.get("name", "Fresh")},
        )
        assert created_again.status_code == 409 and error(created_again)["code"] == code
    for body in (
        {"name": None},
        {"is_chargeable": None},
        {"vat_rate_id": "00000000-0000-0000-0000-000000000000"},
    ):
        assert (await client.patch(url, headers=owner.headers, json=body)).status_code == 422, body
    off = ok_data(await client.post(f"{url}/deactivate", headers=owner.headers))
    assert off["status"] == "inactive"
    assert (
        ok_data(await client.post(f"{url}/activate", headers=owner.headers))["status"] == "active"
    )


async def test_list_search_and_inactive_filters(client: AsyncClient) -> None:
    owner, org = await practice(client)
    await client.post(
        services_url(org, f"/{await service_id(org, 'SA')}/deactivate"), headers=owner.headers
    )
    assert len(ok_data(await client.get(services_url(org), headers=owner.headers))) == 7
    assert (
        len(
            ok_data(
                await client.get(
                    services_url(org), headers=owner.headers, params={"include_inactive": "true"}
                )
            )
        )
        == 8
    )
    assert [
        s["code"]
        for s in ok_data(
            await client.get(services_url(org), headers=owner.headers, params={"q": "payroll"})
        )
    ] == ["PAYROLL"]
    assert [
        s["code"]
        for s in ok_data(
            await client.get(services_url(org), headers=owner.headers, params={"q": "tax return"})
        )
    ] == ["CT"]
    assert [
        a["code"]
        for a in ok_data(
            await client.get(activities_url(org), headers=owner.headers, params={"q": "book"})
        )
    ] == ["BKP"]


async def test_a_service_that_is_not_billable_by_default_makes_non_billable_jobs(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)
    person = await staff(org)
    acme = await make_client(org)
    internal = await make_service(
        client, owner, org, code="INT", name="Internal", default_billable=False
    )
    billable = await make_service(
        client, owner, org, code="BILL", name="Billable", billing_basis="fixed", default_fee=1_000
    )

    async def job(**body):
        response = await client.post(
            org_url(org, "/jobs"),
            headers=owner.headers,
            json={"client_id": str(acme.id), "name": "J", **body},
        )
        return ok_data(response, 201)

    assert (await job(service_id=internal["id"]))["billing_basis"] == "non_billable"
    assert (await job(service_id=billable["id"]))["billing_basis"] == "fixed"
    assert (await job(service_id=internal["id"], billing_basis="hourly"))[
        "billing_basis"
    ] == "hourly"
    quiet = await job(service_id=internal["id"])
    entry = await log_time(client, person, org, job_id=quiet["id"])
    assert entry["is_billable"] is False  # follows the job


# --- Used ones are deactivated -----------------------------------------------------------------------


async def test_used_services_are_deactivated_never_deleted(client: AsyncClient) -> None:
    owner, org = await practice(client)
    acme = await make_client(org)
    unused = await make_service(client, owner, org, code="U1", name="Unused")
    by_job = await make_service(client, owner, org, code="U2", name="By job")
    by_rate = await make_service(client, owner, org, code="U3", name="By rate")
    await make_job(org, acme, service_id=uuid.UUID(by_job["id"]))
    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        client_id=acme.id,
        service_id=by_rate["id"],
        rate=1_000,
        effective_from="2026-01-01",
    )

    for service in (by_job, by_rate):
        url = services_url(org, f"/{service['id']}")
        assert ok_data(await client.get(url, headers=owner.headers))["is_used"] is True
        assert ok_data(await client.get(url, headers=owner.headers))["can_delete"] is False
        outcome = ok_data(await client.delete(url, headers=owner.headers))
        assert outcome == {"outcome": "deactivated", "id": service["id"]}
        assert ok_data(await client.get(url, headers=owner.headers))["status"] == "inactive"
    gone = ok_data(
        await client.delete(services_url(org, f"/{unused['id']}"), headers=owner.headers)
    )
    assert gone["outcome"] == "deleted"
    assert (
        await client.get(services_url(org, f"/{unused['id']}"), headers=owner.headers)
    ).status_code == 404
    assert len(await audit_entries(org["id"], "service.deactivated")) == 2


async def test_used_activity_types_are_deactivated_never_deleted(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await staff(org)
    acme = await make_client(org)
    unused = await make_activity(client, owner, org, code="U1", name="Unused")
    by_time = await make_activity(client, owner, org, code="U2", name="By time")
    by_link = await make_activity(client, owner, org, code="U3", name="By link")
    await make_time_entry(org, person, client=acme, activity_code_id=uuid.UUID(by_time["id"]))
    await set_links(client, owner, org, {"id": str(await service_id(org, "ACCS"))}, [by_link["id"]])

    for activity in (by_time, by_link):
        url = activities_url(org, f"/{activity['id']}")
        assert ok_data(await client.get(url, headers=owner.headers))["is_used"] is True
        outcome = ok_data(await client.delete(url, headers=owner.headers))
        assert outcome == {"outcome": "deactivated", "id": activity["id"]}
        assert ok_data(await client.get(url, headers=owner.headers))["can_delete"] is False
    assert (
        ok_data(
            await client.delete(activities_url(org, f"/{unused['id']}"), headers=owner.headers)
        )["outcome"]
        == "deleted"
    )
    assert len(await audit_entries(org["id"], "activity_code.deactivated")) == 2


async def test_a_vat_rate_used_by_an_activity_type_is_archived_not_deleted(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)
    reduced = await vat_rate_id(org, "reduced")
    await make_activity(client, owner, org, vat_rate_id=str(reduced))
    response = await client.delete(org_url(org, f"/vat-rates/{reduced}"), headers=owner.headers)
    assert ok_data(response)["outcome"] == "archived"
    # An archived rate cannot be chosen any more.
    response = await client.post(
        activities_url(org),
        headers=owner.headers,
        json={"code": "ZZ", "name": "Zz", "vat_rate_id": str(reduced)},
    )
    assert response.status_code == 422


# --- Service to activity links --------------------------------------------------------------------------


async def test_service_to_activity_links_are_read_and_replaced(client: AsyncClient) -> None:
    owner, org = await practice(client)
    service = ok_data(
        await client.get(
            services_url(org, f"/{await service_id(org, 'ACCS')}"), headers=owner.headers
        )
    )
    acc, bkp, vat = (
        await activity_code_id(org, "ACC"),
        await activity_code_id(org, "BKP"),
        await activity_code_id(org, "VAT"),
    )
    url = services_url(org, f"/{service['id']}/activity-codes")
    none = ok_data(await client.get(url, headers=owner.headers))
    assert none == {"service_id": service["id"], "restricted": False, "activity_codes": []}

    replaced = await set_links(client, owner, org, service, [bkp, acc, acc])  # duplicates collapse
    assert replaced["restricted"] is True and [a["code"] for a in replaced["activity_codes"]] == [
        "ACC",
        "BKP",
    ]
    assert {"id", "code", "name", "is_chargeable", "status", "is_used"} <= set(
        replaced["activity_codes"][0]
    )
    assert sorted(
        ok_data(await client.get(services_url(org, f"/{service['id']}"), headers=owner.headers))[
            "activity_code_ids"
        ]
    ) == sorted([str(acc), str(bkp)])
    listed = {
        s["code"]: s["activity_code_ids"]
        for s in ok_data(await client.get(services_url(org), headers=owner.headers))
    }
    assert len(listed["ACCS"]) == 2 and listed["VAT"] == []

    again = await set_links(client, owner, org, service, [vat])
    assert [a["code"] for a in again["activity_codes"]] == ["VAT"]
    [first, second] = await audit_entries(org["id"], "service.activity_codes_changed")
    assert first["metadata"]["added"] == ["ACC", "BKP"] and first["metadata"]["removed"] == []
    assert second["metadata"]["added"] == ["VAT"] and second["metadata"]["removed"] == [
        "ACC",
        "BKP",
    ]
    # Saving the same set changes nothing and logs nothing.
    await set_links(client, owner, org, service, [vat])
    assert len(await audit_entries(org["id"], "service.activity_codes_changed")) == 2
    cleared = await set_links(client, owner, org, service, [])
    assert cleared == {"service_id": service["id"], "restricted": False, "activity_codes": []}

    for body in (
        {"activity_code_ids": ["00000000-0000-0000-0000-000000000000"]},
        {"activity_code_ids": ["x"]},
        {},
        {"activity_code_ids": None},
    ):
        response = await client.put(url, headers=owner.headers, json=body)
        assert response.status_code == 422, body
    # Deactivated types can stay linked.
    await client.post(activities_url(org, f"/{acc}/deactivate"), headers=owner.headers)
    assert (await set_links(client, owner, org, service, [acc]))["restricted"] is True


async def test_links_of_other_practices_are_invisible(client: AsyncClient) -> None:
    owner, org = await practice(client)
    _, other = await practice(client, "Other & Co")
    theirs = await service_id(other, "ACCS")
    url = services_url(org, f"/{theirs}/activity-codes")
    assert (await client.get(url, headers=owner.headers)).status_code == 404
    response = await client.put(url, headers=owner.headers, json={"activity_code_ids": []})
    assert response.status_code == 404
    # Their activity types cannot be linked to our service.
    mine = await service_id(org, "ACCS")
    response = await client.put(
        services_url(org, f"/{mine}/activity-codes"),
        headers=owner.headers,
        json={"activity_code_ids": [str(await activity_code_id(other, "ACC"))]},
    )
    assert response.status_code == 422
    # ... and the other practice's own list is not ours to see.
    assert (await client.get(services_url(other), headers=owner.headers)).status_code == 404


# --- What the links mean for time -------------------------------------------------------------------------


async def test_time_must_use_an_activity_the_service_allows(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await staff(org)
    acme = await make_client(org)
    accounts = await service_id(org, "ACCS")
    job = await make_job(org, acme, service_id=accounts)
    free_job = await make_job(org, acme)  # no service: no limit
    acc, bkp = await activity_code_id(org, "ACC"), await activity_code_id(org, "BKP")
    await set_links(client, owner, org, {"id": str(accounts)}, [acc])

    refused = await log_time(client, person, org, status=422, job_id=job.id, activity_code_id=bkp)
    assert (
        refused["details"][0]["field"] == "activity_code_id"
        and refused["details"][0]["code"] == "activity_not_allowed"
    )
    allowed = await log_time(client, person, org, job_id=job.id, activity_code_id=acc)
    assert allowed["activity_code"] == "ACC"
    assert (await log_time(client, person, org, job_id=job.id))[
        "activity_code_id"
    ] is None  # no activity is fine
    assert (await log_time(client, person, org, job_id=free_job.id, activity_code_id=bkp))[
        "activity_code"
    ] == "BKP"
    assert (await log_time(client, person, org, client_id=acme.id, activity_code_id=bkp))[
        "activity_code"
    ] == "BKP"

    # Changing an entry to a code the service does not allow is refused ...
    url = org_url(org, f"/time-entries/{allowed['id']}")
    response = await client.patch(url, headers=person.headers, json={"activity_code_id": str(bkp)})
    assert (
        response.status_code == 422
        and error(response)["details"][0]["code"] == "activity_not_allowed"
    )
    # ... and so is moving it onto such a job.
    other = await log_time(client, person, org, job_id=free_job.id, activity_code_id=bkp)
    response = await client.patch(
        org_url(org, f"/time-entries/{other['id']}"),
        headers=person.headers,
        json={"job_id": str(job.id)},
    )
    assert response.status_code == 422
    # A batch names the entry that is wrong.
    response = await client.post(
        org_url(org, "/time-entries/bulk"),
        headers=person.headers,
        json={
            "entries": [
                {
                    "entry_date": "2026-09-07",
                    "minutes": 30,
                    "job_id": str(job.id),
                    "activity_code_id": str(acc),
                },
                {
                    "entry_date": "2026-09-07",
                    "minutes": 30,
                    "job_id": str(job.id),
                    "activity_code_id": str(bkp),
                },
            ]
        },
    )
    assert (
        response.status_code == 422
        and error(response)["details"][0]["field"] == "entries.1.activity_code_id"
    )

    # Time already recorded keeps its code when the list changes later, and can still be edited.
    await set_links(client, owner, org, {"id": str(accounts)}, [await activity_code_id(org, "VAT")])
    kept = await client.patch(url, headers=person.headers, json={"description": "still fine"})
    assert kept.status_code == 200 and kept.json()["data"]["activity_code"] == "ACC"
    # With the limit removed anything goes.
    await set_links(client, owner, org, {"id": str(accounts)}, [])
    assert (await log_time(client, person, org, job_id=job.id, activity_code_id=bkp))[
        "activity_code"
    ] == "BKP"


async def test_a_timer_cannot_start_on_a_disallowed_activity(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await staff(org)
    acme = await make_client(org)
    accounts = await service_id(org, "ACCS")
    job = await make_job(org, acme, service_id=accounts)
    acc, bkp = await activity_code_id(org, "ACC"), await activity_code_id(org, "BKP")
    await set_links(client, owner, org, {"id": str(accounts)}, [acc])
    response = await client.post(
        org_url(org, "/timer/start"),
        headers=person.headers,
        json={"job_id": str(job.id), "activity_code_id": str(bkp)},
    )
    assert (
        response.status_code == 422
        and error(response)["details"][0]["code"] == "activity_not_allowed"
    )
    assert ok_data(await client.get(org_url(org, "/timer"), headers=person.headers)) is None
    response = await client.post(
        org_url(org, "/timer/start"),
        headers=person.headers,
        json={"job_id": str(job.id), "activity_code_id": str(acc)},
    )
    assert response.status_code in (200, 201)


# --- Permissions ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "can_manage"),
    [
        ("owner", True),
        ("admin", True),
        ("partner", True),
        ("manager", False),
        ("staff", False),
        ("billing", False),
        ("viewer", False),
    ],
)
async def test_permissions_per_role(client: AsyncClient, role: str, can_manage: bool) -> None:
    owner, org = await practice(client)
    actor = owner if role == "owner" else await staff(org, role)
    accounts = await service_id(org, "ACCS")
    acc = await activity_code_id(org, "ACC")
    # Anyone in the practice can read them.
    for path in (
        services_url(org),
        services_url(org, f"/{accounts}"),
        services_url(org, f"/{accounts}/activity-codes"),
        activities_url(org),
        activities_url(org, f"/{acc}"),
    ):
        assert (await client.get(path, headers=actor.headers)).status_code == 200, path
    wanted = 201 if can_manage else 403
    created = await client.post(
        services_url(org), headers=actor.headers, json={"code": "NEW1", "name": "New 1"}
    )
    assert created.status_code == wanted
    made = await client.post(
        activities_url(org), headers=actor.headers, json={"code": "NEW1", "name": "New 1"}
    )
    assert made.status_code == wanted
    expected = 200 if can_manage else 403
    assert (
        await client.patch(
            services_url(org, f"/{accounts}"), headers=actor.headers, json={"description": "x"}
        )
    ).status_code == expected
    assert (
        await client.put(
            services_url(org, f"/{accounts}/activity-codes"),
            headers=actor.headers,
            json={"activity_code_ids": [str(acc)]},
        )
    ).status_code == expected
    assert (
        await client.post(services_url(org, f"/{accounts}/deactivate"), headers=actor.headers)
    ).status_code == expected
    assert (
        await client.patch(
            activities_url(org, f"/{acc}"), headers=actor.headers, json={"invoice_description": "x"}
        )
    ).status_code == expected
    assert (
        await client.post(activities_url(org, f"/{acc}/deactivate"), headers=actor.headers)
    ).status_code == expected
    assert (
        await client.delete(services_url(org, f"/{accounts}"), headers=actor.headers)
    ).status_code == expected
