"""A person's own permissions on top of their role: grants, denials, limits, and where they apply."""

from httpx import AsyncClient

from app.core.permissions import effective_permissions
from tests.domain_factories import make_client, practice, staff
from tests.factories import API, audit_entries, error
from tests.foundation_helpers import add_custom_role
from tests.test_time_approval_exempt import eligible
from tests.time_helpers import log_time, org_url


def permissions_url(org: dict, member) -> str:
    return org_url(org, f"/members/{member.id}/permissions")


async def put(client: AsyncClient, actor, org: dict, member, *, status: int = 200, **body):
    response = await client.put(
        permissions_url(org, member),
        headers=actor.headers,
        json={"grants": [], "denies": [], **body},
    )
    assert response.status_code == status, response.text
    return response.json()["data"] if status < 300 else error(response)


async def get(client: AsyncClient, actor, org: dict, member) -> dict:
    response = await client.get(permissions_url(org, member), headers=actor.headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_the_effective_permissions_are_the_roles_plus_grants_less_denials() -> None:
    assert effective_permissions({"a", "b"}, [("c", "grant"), ("a", "deny")]) == {"b", "c"}
    assert effective_permissions(set(), []) == frozenset()


async def test_a_granted_permission_opens_a_section_for_one_person(client: AsyncClient) -> None:
    owner, org = await practice(client)
    credit_control = await staff(org, "staff")
    colleague = await staff(org, "staff")
    wip = org_url(org, "/wip")
    assert (await client.get(wip, headers=credit_control.headers)).status_code == 403

    saved = await put(client, owner, org, credit_control, grants=["wip.read"])
    assert saved["grants"] == ["wip.read"] and "wip.read" in saved["effective"]
    assert saved["denies"] == [] and "wip.read" not in saved["role_permissions"]

    assert (await client.get(wip, headers=credit_control.headers)).status_code == 200
    # Their colleague with the same role is not affected.
    assert (await client.get(wip, headers=colleague.headers)).status_code == 403
    assert "wip.read" not in (await get(client, owner, org, colleague))["effective"]


async def test_a_denied_permission_is_taken_from_one_person(client: AsyncClient) -> None:
    owner, org = await practice(client)
    biller = await staff(org, "billing")
    other = await staff(org, "billing")
    invoices = org_url(org, "/invoices")
    assert (await client.get(invoices, headers=biller.headers)).status_code == 200

    saved = await put(client, owner, org, biller, denies=["invoice.read"])
    assert saved["denies"] == ["invoice.read"] and "invoice.read" not in saved["effective"]
    assert "invoice.read" in saved["role_permissions"]

    assert (await client.get(invoices, headers=biller.headers)).status_code == 403
    assert (await client.get(invoices, headers=other.headers)).status_code == 200


async def test_the_practice_list_the_app_gates_on_carries_the_persons_own_permissions(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)
    person = await staff(org, "staff")
    await put(client, owner, org, person, grants=["wip.read"], denies=["time.log"])

    mine = await client.get(f"{API}/users/me/organizations", headers=person.headers)
    [entry] = [e for e in mine.json()["data"] if e["organization"]["id"] == org["id"]]
    assert "wip.read" in entry["permissions"] and "time.log" not in entry["permissions"]
    by_slug = await client.get(f"{API}/organizations/by-slug/{org['slug']}", headers=person.headers)
    assert "wip.read" in by_slug.json()["data"]["permissions"]
    single = await client.get(org_url(org, ""), headers=person.headers)
    assert "wip.read" in single.json()["data"]["permissions"]


async def test_only_what_differs_from_the_role_is_kept(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await staff(org, "manager")
    saved = await put(
        client,
        owner,
        org,
        person,
        grants=["time.log", "wip.write_off"],  # the first the role has already
        denies=["time.manage_all", "organization.delete"],  # the second the role never had
    )
    assert saved["grants"] == ["wip.write_off"]
    assert saved["denies"] == ["time.manage_all"]
    # Saving nothing puts the person back on their role.
    cleared = await put(client, owner, org, person)
    assert cleared["grants"] == [] and cleared["denies"] == []
    assert cleared["effective"] == cleared["role_permissions"]


async def test_a_permission_cannot_be_granted_and_denied_or_unknown(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await staff(org, "staff")
    both = await put(
        client, owner, org, person, status=422, grants=["wip.read"], denies=["wip.read"]
    )
    assert both["code"] == "VALIDATION_ERROR"
    unknown = await put(client, owner, org, person, status=422, grants=["not.a.permission"])
    assert unknown["code"] == "VALIDATION_ERROR"
    assert (await get(client, owner, org, person))["grants"] == []


async def test_nobody_grants_what_they_do_not_hold(client: AsyncClient) -> None:
    _, org = await practice(client)
    await add_custom_role(
        org["id"], "People managers", ["role.manage", "role.read", "member.read", "wip.read"]
    )
    manager = await staff(org, "People managers")
    person = await staff(org, "staff")
    refused = await put(client, manager, org, person, status=403, grants=["invoice.issue"])
    assert refused["code"] == "PERMISSION_DENIED"
    allowed = await put(client, manager, org, person, grants=["wip.read"])
    assert allowed["grants"] == ["wip.read"]


async def test_nobody_changes_their_own_or_an_owners_permissions(client: AsyncClient) -> None:
    owner, org = await practice(client)
    admin = await staff(org, "admin")
    own = await put(client, admin, org, admin, status=403, denies=["invoice.read"])
    assert own["code"] == "PERMISSION_DENIED"
    fixed = await put(client, admin, org, owner, status=409, denies=["invoice.read"])
    assert fixed["code"] == "OWNER_PERMISSIONS_FIXED"


async def test_changing_permissions_needs_role_manage_and_reading_them_needs_role_read(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)
    person = await staff(org, "staff")
    partner = await staff(org, "partner")  # may not manage roles
    denied = await put(client, partner, org, person, status=403, grants=["wip.read"])
    assert denied["code"] == "PERMISSION_DENIED"
    viewer = await staff(org, "viewer")
    assert (
        await client.get(permissions_url(org, person), headers=viewer.headers)
    ).status_code == 403
    assert (
        await client.get(permissions_url(org, person), headers=owner.headers)
    ).status_code == 200


async def test_another_practices_member_is_not_found(client: AsyncClient) -> None:
    owner, org = await practice(client)
    _, other = await practice(client, "Other & Co")
    outsider = await staff(other, "staff")
    response = await client.put(
        permissions_url(org, outsider), headers=owner.headers, json={"grants": ["wip.read"]}
    )
    assert response.status_code == 404


async def test_a_change_is_audited_with_what_was_added_and_removed(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await staff(org, "staff")
    await put(client, owner, org, person, grants=["wip.read"])
    await put(client, owner, org, person, grants=["audit_log.read"], denies=["time.log"])
    first, second = await audit_entries(org["id"], "member.permissions_changed")
    assert first["metadata"]["grants_added"] == ["wip.read"]
    assert second["metadata"]["grants_added"] == ["audit_log.read"]
    assert second["metadata"]["grants_removed"] == ["wip.read"]
    assert second["metadata"]["denies_added"] == ["time.log"]
    assert second["target_id"] == str(person.id)


async def test_time_needs_no_approval_for_a_person_who_was_given_that_permission(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd")
    manager = await staff(org, "manager")  # the role does not include it
    partner = await staff(org, "partner")  # the role does
    mine = await log_time(client, manager, org, client_id=acme.id)
    theirs = await log_time(client, partner, org, client_id=acme.id)
    assert await eligible(org) == {theirs["id"]}

    await put(client, owner, org, manager, grants=["timesheet.exempt"])
    await put(client, owner, org, partner, denies=["timesheet.exempt"])
    assert await eligible(org) == {mine["id"]}
