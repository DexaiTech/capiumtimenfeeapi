"""Overtime approval (APT81, phase C): the list, approve and reject (only the premium is removed),
who may decide, locked periods and the audit trail; and the OpenAPI documentation of the
overtime and Staff Cost routes."""

from datetime import timedelta

from httpx import AsyncClient

from tests.domain_factories import staff
from tests.factories import add_member, audit_entries
from tests.foundation_helpers import add_custom_role
from tests.overtime_helpers import MONDAY, Scene, log, scene
from tests.period_lock_helpers import set_lock
from tests.reports_helpers import report
from tests.time_helpers import err, ok_data, org_url, set_manager

PREFIX = "/api/v1/organizations/{organization_id}"


async def with_overtime(client: AsyncClient) -> tuple[Scene, dict]:
    """Sam with 600 standard minutes a week: 60 overtime minutes on Job X, pending."""
    s = await scene(client, standard_minutes=600)
    await log(client, s, MONDAY, 600)
    await log(client, s, MONDAY + timedelta(days=1), 60, confirm=True)
    [row] = ok_data(await client.get(org_url(s.org, "/overtime"), headers=s.owner.headers))
    return s, row


async def decide(client: AsyncClient, actor, s: Scene, row: dict, action: str, **body):
    return await client.post(
        org_url(s.org, f"/overtime/{row['id']}/{action}"), headers=actor.headers, json=body
    )


async def test_the_list_shows_overtime_with_its_entry_job_and_client(client: AsyncClient) -> None:
    s, row = await with_overtime(client)
    assert row["user"] == {"id": str(s.person.id), "full_name": "Sam Senior"}
    assert row["job"]["name"] == "Job X" and row["client"]["name"] == "Acme Ltd"
    assert (row["entry_date"], row["overtime_minutes"], row["allocated"], row["status"]) == (
        "2026-09-08",
        60,
        True,
        "pending",
    )
    assert (row["overtime_rate"], row["overtime_cost"], row["premium_cost"]) == (
        3_000,
        3_000,
        1_000,
    )
    assert row["confirmed_by"] == str(s.person.id)
    url = org_url(s.org, "/overtime")
    for params, count in (
        ({"status": "approved"}, 0),
        ({"status": "pending", "allocated": "true"}, 1),
        ({"allocated": "false"}, 0),
        ({"date_from": "2026-09-09"}, 0),
        ({"from": "2026-09-08", "to": "2026-09-08"}, 1),
        ({"user_id": str(s.person.id)}, 1),
    ):
        found = ok_data(await client.get(url, headers=s.owner.headers, params=params))
        assert len(found) == count, params
    # Sam sees his own overtime, without amounts (no report.staff_cost).
    [mine] = ok_data(await client.get(url, headers=s.person.headers))
    assert (mine["overtime_rate"], mine["overtime_cost"], mine["premium_cost"]) == (None,) * 3
    # Someone else sees none of it, and may not ask for Sam's.
    other = await staff(s.org)
    assert ok_data(await client.get(url, headers=other.headers)) == []
    asked = await client.get(url, headers=other.headers, params={"user_id": str(s.person.id)})
    assert asked.status_code == 403


async def test_rejecting_removes_only_the_premium_and_approving_puts_it_back(
    client: AsyncClient,
) -> None:
    s, row = await with_overtime(client)
    assert (await decide(client, s.owner, s, row, "reject")).status_code == 422
    rejected = ok_data(await decide(client, s.owner, s, row, "reject", note="Not agreed"))
    assert (rejected["status"], rejected["decision_note"]) == ("rejected", "Not agreed")
    assert rejected["decided_by"] == str(s.owner.id) and rejected["decided_at"]
    week = {"date_from": MONDAY, "date_to": MONDAY + timedelta(days=6)}
    [line] = (await report(client, s.owner, s.org, "staff-cost", **week))["rows"]
    # The hours stay, at their standard cost (£20/h): 11 hours, £220, no overtime.
    assert (line["standard_minutes"], line["standard_cost"]) == (660, 22_000)
    assert (line["overtime_minutes_allocated"], line["total_cost"]) == (0, 22_000)
    again = await decide(client, s.owner, s, row, "reject", note="Twice")
    assert err(again, 409)["code"] == "INVALID_STATUS_TRANSITION"

    approved = ok_data(await decide(client, s.owner, s, row, "approve"))
    assert approved["status"] == "approved"
    [line] = (await report(client, s.owner, s.org, "staff-cost", **week))["rows"]
    assert (line["standard_cost"], line["overtime_cost_allocated"], line["total_cost"]) == (
        20_000,
        3_000,
        23_000,
    )
    assert line["pending_overtime_cost"] == 0
    trail = await audit_entries(s.org["id"], "overtime.rejected")
    assert trail[0]["target_type"] == "time_entry"
    assert trail[0]["metadata"]["changes"] == {"status": ["pending", "rejected"]}
    [approved_entry] = await audit_entries(s.org["id"], "overtime.approved")
    assert approved_entry["metadata"]["changes"] == {"status": ["rejected", "approved"]}


async def test_who_may_decide(client: AsyncClient) -> None:
    s, row = await with_overtime(client)
    colleague = await staff(s.org)
    await add_custom_role(s.org["id"], "Approver", ["time.log", "timesheet.approve"])
    approver = await add_member(s.org["id"], "Approver")
    # An approver who is not Sam's, and a colleague without the permission: 403.
    assert (await decide(client, approver, s, row, "approve")).status_code == 403
    assert (await decide(client, colleague, s, row, "approve")).status_code == 403
    # Sam's approver may; so may anyone with time.manage_all (a manager).
    await set_manager(s.org, s.person, approver)
    assert (await decide(client, approver, s, row, "approve")).status_code == 200
    manager = await staff(s.org, "manager")
    assert (await decide(client, manager, s, row, "reject", note="No")).status_code == 200
    unknown = await client.post(
        org_url(s.org, "/overtime/00000000-0000-0000-0000-000000000000/approve"),
        headers=s.owner.headers,
    )
    assert unknown.status_code == 404


async def test_ones_own_overtime_needs_organization_update(client: AsyncClient) -> None:
    s = await scene(client, role="manager", standard_minutes=60)
    await log(client, s, MONDAY, 60)
    await log(client, s, MONDAY + timedelta(days=1), 30, confirm=True)
    [row] = ok_data(await client.get(org_url(s.org, "/overtime"), headers=s.person.headers))
    refused = await decide(client, s.person, s, row, "approve")
    assert err(refused, 403)["code"] == "SELF_APPROVAL_FORBIDDEN"


async def test_overtime_in_a_locked_period_cannot_be_decided(client: AsyncClient) -> None:
    s, row = await with_overtime(client)
    await set_lock(s.org, MONDAY + timedelta(days=6))
    assert err(await decide(client, s.owner, s, row, "approve"), 409)["code"] == "PERIOD_LOCKED"


async def test_the_overtime_and_staff_cost_routes_are_documented(client: AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()
    routes = {
        ("get", "/overtime"): ["time.read_all", "report.staff_cost"],
        ("post", "/overtime/{overtime_id}/approve"): [
            "time.manage_all",
            "SELF_APPROVAL_FORBIDDEN",
            "INVALID_STATUS_TRANSITION",
            "PERIOD_LOCKED",
        ],
        ("post", "/overtime/{overtime_id}/reject"): ["time.manage_all", "PERIOD_LOCKED"],
        ("get", "/reports/staff-cost"): ["report.staff_cost", "report.export"],
        ("get", "/reports/staff-cost/jobs"): ["report.staff_cost", "report.export"],
        ("post", "/time-entries"): ["OVERTIME_CONFIRMATION_REQUIRED"],
        ("post", "/time-entries/bulk"): ["OVERTIME_CONFIRMATION_REQUIRED"],
        ("patch", "/time-entries/{entry_id}"): ["OVERTIME_CONFIRMATION_REQUIRED"],
        ("post", "/time-entries/{entry_id}/copy"): ["OVERTIME_CONFIRMATION_REQUIRED"],
        ("post", "/time-entries/copy-week"): ["OVERTIME_CONFIRMATION_REQUIRED"],
        ("post", "/time-entries/{entry_id}/correct"): ["OVERTIME_CONFIRMATION_REQUIRED"],
        ("post", "/timer/stop"): ["OVERTIME_CONFIRMATION_REQUIRED"],
        ("post", "/timer/start"): ["OVERTIME_CONFIRMATION_REQUIRED"],
    }
    for (method, path), words in routes.items():
        operation = schema["paths"][PREFIX + path][method]
        assert operation["summary"], path
        for word in words:
            assert f"`{word}`" in operation["description"], (path, word)
        for parameter in operation.get("parameters", []):
            if parameter["in"] == "query":
                assert parameter.get("description"), (path, parameter["name"])
    components = schema["components"]["schemas"]
    missing = [
        f"{name}.{field}"
        for name in (
            "OvertimeRead",
            "OvertimeUserRef",
            "OvertimeJobRef",
            "OvertimeClientRef",
            "OvertimeReject",
            "OvertimeApprove",
            "StaffCostRow",
            "StaffCostFigures",
            "StaffCostJobRow",
            "StaffCostJobTotals",
        )
        for field, prop in components[name]["properties"].items()
        if not prop.get("description")
    ]
    assert not missing, missing
