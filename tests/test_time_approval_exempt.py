"""Time that needs no approval: the practice setting, the ``timesheet.exempt`` permission and
how an exempt person's week counts as approved for WIP and billing."""

import uuid

from httpx import AsyncClient
from sqlalchemy import select

from app.db.models import Organization, TimeEntry
from app.db.session import get_sessionmaker
from app.services.time_entries import wip_eligibility_clause
from tests.domain_factories import make_client, practice, staff
from tests.foundation_helpers import add_custom_role
from tests.time_helpers import log_time, org_url


async def eligible(org: dict) -> set[str]:
    """The time entries that are billable WIP under the practice's current settings."""
    async with get_sessionmaker()() as session:
        organization = await session.get(Organization, uuid.UUID(org["id"]))
        assert organization is not None
        rows = await session.scalars(
            select(TimeEntry.id).where(
                TimeEntry.organization_id == organization.id,
                wip_eligibility_clause(organization),
            )
        )
        return {str(row) for row in rows}


async def set_exemption(client: AsyncClient, owner, org: dict, value: bool) -> None:
    response = await client.patch(
        f"{org_url(org, '')}", headers=owner.headers, json={"auto_approve_privileged_time": value}
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["auto_approve_privileged_time"] is value


async def sheet_of(client: AsyncClient, actor, org: dict, entry: dict) -> dict:
    response = await client.get(
        org_url(org, f"/timesheets/{entry['timesheet_id']}"), headers=actor.headers
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def test_the_setting_is_on_by_default(client: AsyncClient) -> None:
    _, org = await practice(client)
    assert org["auto_approve_privileged_time"] is True
    assert org["require_timesheet_approval"] is True


async def test_an_owner_admin_or_partner_needs_no_approval_but_staff_do(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd")
    entries = {}
    for role in ("owner", "admin", "partner", "manager", "staff"):
        actor = owner if role == "owner" else await staff(org, role)
        entries[role] = await log_time(client, actor, org, client_id=acme.id)
    counted = await eligible(org)
    assert {role for role, entry in entries.items() if entry["id"] in counted} == {
        "owner",
        "admin",
        "partner",
    }


async def test_a_designated_role_can_be_given_the_permission(client: AsyncClient) -> None:
    _, org = await practice(client)
    acme = await make_client(org, "Acme Ltd")
    await add_custom_role(org["id"], "Senior managers", ["time.log", "timesheet.exempt"])
    await add_custom_role(org["id"], "Juniors", ["time.log"])
    senior = await staff(org, "Senior managers")
    junior = await staff(org, "Juniors")
    first = await log_time(client, senior, org, client_id=acme.id)
    await log_time(client, junior, org, client_id=acme.id)
    assert await eligible(org) == {first["id"]}


async def test_switching_the_setting_off_and_on_applies_to_existing_weeks(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd")
    entry = await log_time(client, owner, org, client_id=acme.id)
    assert await eligible(org) == {entry["id"]}
    await set_exemption(client, owner, org, False)
    assert await eligible(org) == set()
    await set_exemption(client, owner, org, True)
    assert await eligible(org) == {entry["id"]}


async def test_changing_a_members_role_changes_their_exemption(client: AsyncClient) -> None:
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd")
    person = await staff(org, "manager")
    entry = await log_time(client, person, org, client_id=acme.id)
    assert await eligible(org) == set()
    roles = await client.get(org_url(org, "/roles"), headers=owner.headers)
    partner = next(r for r in roles.json()["data"] if r.get("system_key") == "partner")
    response = await client.patch(
        org_url(org, f"/members/{person.id}"),
        headers=owner.headers,
        json={"role_id": partner["id"]},
    )
    assert response.status_code == 200, response.text
    assert await eligible(org) == {entry["id"]}


async def test_submitting_an_exempt_week_approves_it_without_asking_anybody(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd")
    partner = await staff(org, "partner")
    entry = await log_time(client, partner, org, client_id=acme.id)
    # Time on an exempt week can be changed until it is submitted.
    updated = await client.patch(
        org_url(org, f"/time-entries/{entry['id']}"), headers=partner.headers, json={"minutes": 90}
    )
    assert updated.status_code == 200, updated.text

    submitted = await client.post(
        org_url(org, f"/timesheets/{entry['timesheet_id']}/submit"), headers=partner.headers
    )
    assert submitted.status_code == 200, submitted.text
    sheet = submitted.json()["data"]
    assert sheet["status"] == "approved" and sheet["decision_note"] == "Approval not required"
    queue = await client.get(org_url(org, "/approvals"), headers=owner.headers)
    assert queue.json()["data"] == []
    # A week that needs approval still goes to the queue.
    other = await staff(org, "staff")
    theirs = await log_time(client, other, org, client_id=acme.id)
    await client.post(
        org_url(org, f"/timesheets/{theirs['timesheet_id']}/submit"), headers=other.headers
    )
    queue = await client.get(org_url(org, "/approvals"), headers=owner.headers)
    assert [row["id"] for row in queue.json()["data"]] == [theirs["timesheet_id"]]
