"""Invoice templates: how a practice's invoices look, and which one a client uses."""

import asyncio

import pytest

from tests.central_helpers import new_team, scalar, sql
from tests.clients_helpers import create_client, org_url
from tests.factories import audit_entries, create_user_with_org, error


def templates_url(team, tail: str = "") -> str:
    return org_url(team.org, f"/invoice-templates{tail}")


async def make_template(client, team, name: str = "Detailed", **body) -> dict:
    response = await client.post(
        templates_url(team), headers=team.owner.headers, json={"name": name, **body}
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def list_templates(client, team, actor=None, **params) -> list[dict]:
    response = await client.get(
        templates_url(team), headers=(actor or team.owner).headers, params=params
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def default_of(team) -> list[str]:
    rows = await sql(
        "SELECT name FROM invoice_templates WHERE organization_id = :o AND is_default "
        "AND deleted_at IS NULL",
        o=team.id,
    )
    return [row["name"] for row in rows]


# --- A new practice starts with a Standard template ------------------------------------------


async def test_a_new_practice_has_one_default_standard_template(client):
    team = await new_team(client)
    [standard] = await list_templates(client, team)
    assert {k: standard[k] for k in standard if k not in ("id", "created_at", "updated_at")} == {
        "name": "Standard",
        "layout": "standard",
        "show_logo": True,
        "show_bank_details": True,
        "footer_text": None,
        "is_default": True,
        "status": "active",
        "client_count": 0,
    }
    # Each practice has its own.
    other_owner, other_org = await create_user_with_org(client, "Second Practice")
    theirs = await client.get(org_url(other_org, "/invoice-templates"), headers=other_owner.headers)
    assert [t["name"] for t in theirs.json()["data"]] == ["Standard"]
    assert theirs.json()["data"][0]["id"] != standard["id"]


# --- Create -----------------------------------------------------------------------------------


async def test_create_a_template(client):
    team = await new_team(client)
    response = await client.post(
        templates_url(team),
        headers=team.owner.headers,
        json={
            "name": "  Detailed  ",
            "layout": "detailed",
            "show_logo": False,
            "show_bank_details": True,
            "footer_text": "Thank you for your business.",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["message"] == "Invoice template created"
    data = response.json()["data"]
    assert (data["name"], data["layout"], data["show_logo"], data["show_bank_details"]) == (
        "Detailed",
        "detailed",
        False,
        True,
    )
    assert (data["footer_text"], data["is_default"], data["status"], data["client_count"]) == (
        "Thank you for your business.",
        False,
        "active",
        0,
    )
    assert data["created_at"] and data["updated_at"]
    [entry] = await audit_entries(team.id, "invoice_template.created")
    assert (
        entry["actor_user_id"] == str(team.owner.id) and entry["target_type"] == "invoice_template"
    )
    assert entry["target_id"] == data["id"]
    assert entry["metadata"]["name"] == "Detailed" and entry["metadata"]["layout"] == "detailed"
    # Listed with the default first, then by name.
    await make_template(client, team, "Summary", layout="summary")
    assert [t["name"] for t in await list_templates(client, team)] == [
        "Standard",
        "Detailed",
        "Summary",
    ]


async def test_create_defaults_and_blank_footer(client):
    team = await new_team(client)
    data = await make_template(client, team, "Plain", footer_text="   ")
    assert (data["layout"], data["show_logo"], data["show_bank_details"]) == (
        "standard",
        True,
        True,
    )
    assert data["footer_text"] is None and data["is_default"] is False


async def test_creating_the_default_unsets_the_previous_one(client):
    team = await new_team(client)
    new_default = await make_template(client, team, "Branded", is_default=True)
    assert new_default["is_default"] is True
    assert [(t["name"], t["is_default"]) for t in await list_templates(client, team)] == [
        ("Branded", True),
        ("Standard", False),
    ]
    assert await default_of(team) == ["Branded"]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 81},
        {"name": "T", "layout": "fancy"},
        {"name": "T", "show_logo": "yes please"},
        {"name": "T", "footer_text": "x" * 1001},
        {"name": "T", "status": "archived"},
        {"name": "T", "is_default": True, "status": "inactive"},
        {"name": "T", "colour": "violet"},
    ],
)
async def test_create_is_validated(client, body):
    team = await new_team(client)
    response = await client.post(templates_url(team), headers=team.owner.headers, json=body)
    assert response.status_code == 422, response.text
    assert len(await list_templates(client, team)) == 1


async def test_template_names_are_unique_ignoring_case(client):
    team = await new_team(client)
    taken = await client.post(
        templates_url(team), headers=team.owner.headers, json={"name": "STANDARD"}
    )
    assert taken.status_code == 409, taken.text
    assert error(taken)["code"] == "TEMPLATE_NAME_TAKEN"
    assert error(taken)["details"][0]["field"] == "name"
    other = await make_template(client, team, "Other")
    clash = await client.patch(
        templates_url(team, f"/{other['id']}"),
        headers=team.owner.headers,
        json={"name": "standard"},
    )
    assert clash.status_code == 409 and error(clash)["code"] == "TEMPLATE_NAME_TAKEN"
    # The same name in another practice is fine.
    owner, org = await create_user_with_org(client, "Second Practice")
    mine = await client.post(
        org_url(org, "/invoice-templates"), headers=owner.headers, json={"name": "Detailed"}
    )
    assert mine.status_code == 201
    # And a deleted template frees its name.
    gone = await client.delete(templates_url(team, f"/{other['id']}"), headers=team.owner.headers)
    assert gone.status_code == 200
    again = await client.post(
        templates_url(team), headers=team.owner.headers, json={"name": "Other"}
    )
    assert again.status_code == 201


async def test_concurrent_creates_of_one_name_make_one_template(client):
    team = await new_team(client)
    responses = await asyncio.gather(
        *(
            client.post(templates_url(team), headers=team.owner.headers, json={"name": "Race"})
            for _ in range(5)
        )
    )
    assert sorted(r.status_code for r in responses) == [201, 409, 409, 409, 409]
    assert (
        await scalar(
            "SELECT count(*) FROM invoice_templates WHERE organization_id = :o AND name = 'Race'",
            o=team.id,
        )
        == 1
    )


# --- Read -------------------------------------------------------------------------------------


async def test_any_member_reads_the_active_templates_and_inactive_ones_need_settings_manage(client):
    team = await new_team(client)
    detailed = await make_template(client, team, "Detailed")
    retired = await make_template(client, team, "Retired")
    await client.patch(
        templates_url(team, f"/{retired['id']}"),
        headers=team.owner.headers,
        json={"status": "inactive"},
    )
    for role in ("staff", "viewer", "manager", "billing"):
        member = await team.member(role)
        names = [t["name"] for t in await list_templates(client, team, member)]
        assert names == ["Standard", "Detailed"], role
        one = await client.get(templates_url(team, f"/{detailed['id']}"), headers=member.headers)
        assert one.status_code == 200 and one.json()["data"]["name"] == "Detailed"
        hidden = await client.get(templates_url(team, f"/{retired['id']}"), headers=member.headers)
        assert hidden.status_code == 404, role
        every = await client.get(
            templates_url(team), headers=member.headers, params={"include_inactive": "true"}
        )
        assert every.status_code == 403, role
    admin = await team.member("admin")
    everything = await list_templates(client, team, admin, include_inactive="true")
    assert [t["name"] for t in everything] == ["Standard", "Detailed", "Retired"]
    shown = await client.get(templates_url(team, f"/{retired['id']}"), headers=admin.headers)
    assert shown.status_code == 200 and shown.json()["data"]["status"] == "inactive"
    assert (
        await client.get(
            templates_url(team), headers=team.owner.headers, params={"include_inactive": "maybe"}
        )
    ).status_code == 422


async def test_templates_of_another_practice_are_not_found(client):
    team = await new_team(client)
    owner, org = await create_user_with_org(client, "Second Practice")
    theirs = (await client.get(org_url(org, "/invoice-templates"), headers=owner.headers)).json()[
        "data"
    ][0]
    url = templates_url(team, f"/{theirs['id']}")
    assert (await client.get(url, headers=team.owner.headers)).status_code == 404
    assert (
        await client.patch(url, headers=team.owner.headers, json={"name": "Mine"})
    ).status_code == 404
    assert (await client.delete(url, headers=team.owner.headers)).status_code == 404
    assert (await client.post(f"{url}/make-default", headers=team.owner.headers)).status_code == 404
    assert (await client.get(templates_url(team), headers=owner.headers)).status_code == 404
    missing = templates_url(team, "/00000000-0000-4000-8000-000000000000")
    assert (await client.get(missing, headers=team.owner.headers)).status_code == 404
    assert (
        await client.get(templates_url(team, "/not-a-uuid"), headers=team.owner.headers)
    ).status_code == 422
    assert (await client.get(templates_url(team))).status_code == 401


# --- Update -----------------------------------------------------------------------------------


async def test_update_a_template_and_audit_the_changes(client):
    team = await new_team(client)
    made = await make_template(
        client, team, "Detailed", layout="detailed", footer_text="Old footer"
    )
    url = templates_url(team, f"/{made['id']}")
    response = await client.patch(
        url,
        headers=team.owner.headers,
        json={"name": "Detailed v2", "layout": "summary", "show_logo": False, "footer_text": None},
    )
    assert response.status_code == 200, response.text
    assert response.json()["message"] == "Invoice template updated"
    data = response.json()["data"]
    assert (data["name"], data["layout"], data["show_logo"], data["footer_text"]) == (
        "Detailed v2",
        "summary",
        False,
        None,
    )
    [entry] = await audit_entries(team.id, "invoice_template.updated")
    assert entry["metadata"]["changes"] == {
        "name": ["Detailed", "Detailed v2"],
        "layout": ["detailed", "summary"],
        "show_logo": [True, False],
        "footer_text": ["Old footer", None],
    }
    # Changing only the case of the name is fine; saying nothing new changes nothing and logs nothing.
    assert (
        await client.patch(url, headers=team.owner.headers, json={"name": "DETAILED V2"})
    ).status_code == 200
    again = await client.patch(url, headers=team.owner.headers, json={"layout": "summary"})
    assert again.status_code == 200
    assert len(await audit_entries(team.id, "invoice_template.updated")) == 2
    blank = await client.patch(url, headers=team.owner.headers, json={"footer_text": "  "})
    assert blank.json()["data"]["footer_text"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"name": None},
        {"layout": None},
        {"show_logo": None},
        {"show_bank_details": None},
        {"status": None},
        {"name": ""},
        {"layout": "fancy"},
        {"status": "archived"},
        {"footer_text": "x" * 1001},
        {"is_default": True},
    ],
)
async def test_update_is_validated(client, body):
    team = await new_team(client)
    made = await make_template(client, team, "Detailed")
    response = await client.patch(
        templates_url(team, f"/{made['id']}"), headers=team.owner.headers, json=body
    )
    assert response.status_code == 422, response.text


async def test_deactivating_and_reactivating_a_template(client):
    team = await new_team(client)
    made = await make_template(client, team, "Detailed")
    url = templates_url(team, f"/{made['id']}")
    off = await client.patch(url, headers=team.owner.headers, json={"status": "inactive"})
    assert off.status_code == 200 and off.json()["data"]["status"] == "inactive"
    on = await client.patch(url, headers=team.owner.headers, json={"status": "active"})
    assert on.json()["data"]["status"] == "active"
    actions = [
        e["action"] for e in await audit_entries(team.id) if e["target_type"] == "invoice_template"
    ]
    assert actions == [
        "invoice_template.created",
        "invoice_template.deactivated",
        "invoice_template.activated",
    ]


async def test_the_default_template_cannot_be_deactivated(client):
    team = await new_team(client)
    [standard] = await list_templates(client, team)
    response = await client.patch(
        templates_url(team, f"/{standard['id']}"),
        headers=team.owner.headers,
        json={"status": "inactive"},
    )
    assert response.status_code == 409, response.text
    assert error(response)["code"] == "TEMPLATE_IS_DEFAULT"
    assert (await list_templates(client, team))[0]["status"] == "active"


# --- Delete -----------------------------------------------------------------------------------


async def test_delete_a_template_nobody_uses(client):
    team = await new_team(client)
    made = await make_template(client, team, "Disposable")
    url = templates_url(team, f"/{made['id']}")
    response = await client.delete(url, headers=team.owner.headers)
    assert response.status_code == 200, response.text
    assert response.json()["message"] == "Invoice template deleted"
    assert [t["name"] for t in await list_templates(client, team)] == ["Standard"]
    assert (await client.get(url, headers=team.owner.headers)).status_code == 404
    assert (await client.delete(url, headers=team.owner.headers)).status_code == 404
    [entry] = await audit_entries(team.id, "invoice_template.deleted")
    assert entry["metadata"]["name"] == "Disposable" and entry["target_id"] == made["id"]
    assert (
        await scalar(
            "SELECT deleted_at IS NOT NULL FROM invoice_templates WHERE id = :i", i=made["id"]
        )
        is True
    )


async def test_a_template_a_client_uses_cannot_be_deleted(client):
    team = await new_team(client)
    made = await make_template(client, team, "Detailed")
    one = await create_client(
        client, team.owner, team.org, "One Ltd", invoice_template_id=made["id"]
    )
    two = await create_client(
        client, team.owner, team.org, "Two Ltd", invoice_template_id=made["id"]
    )
    assert (await list_templates(client, team))[1]["client_count"] == 2
    response = await client.delete(
        templates_url(team, f"/{made['id']}"), headers=team.owner.headers
    )
    assert response.status_code == 409, response.text
    assert error(response)["code"] == "TEMPLATE_IN_USE"
    assert error(response)["details"] == {"clients": 2}
    # Deactivating is the way out: the clients keep it, the pickers no longer offer it.
    off = await client.patch(
        templates_url(team, f"/{made['id']}"),
        headers=team.owner.headers,
        json={"status": "inactive"},
    )
    assert off.status_code == 200 and off.json()["data"]["client_count"] == 2
    kept = await client.get(org_url(team.org, f"/clients/{one['id']}"), headers=team.owner.headers)
    assert kept.json()["data"]["invoice_template"] == {"id": made["id"], "name": "Detailed"}
    # A client that moved away, or was deleted, no longer counts.
    moved = await client.patch(
        org_url(team.org, f"/clients/{one['id']}"),
        headers=team.owner.headers,
        json={"invoice_template_id": None},
    )
    assert moved.status_code == 200, moved.text
    assert error(
        await client.delete(templates_url(team, f"/{made['id']}"), headers=team.owner.headers)
    )["details"] == {"clients": 1}
    gone = await client.delete(
        org_url(team.org, f"/clients/{two['id']}"), headers=team.owner.headers
    )
    assert gone.status_code in (200, 204), gone.text
    deletable = await client.delete(
        templates_url(team, f"/{made['id']}"), headers=team.owner.headers
    )
    assert deletable.status_code == 200, deletable.text


async def test_the_default_template_cannot_be_deleted(client):
    team = await new_team(client)
    [standard] = await list_templates(client, team)
    response = await client.delete(
        templates_url(team, f"/{standard['id']}"), headers=team.owner.headers
    )
    assert response.status_code == 409 and error(response)["code"] == "TEMPLATE_IS_DEFAULT"
    assert len(await list_templates(client, team)) == 1


# --- Make default -------------------------------------------------------------------------------


async def test_make_default_moves_the_default_in_one_step(client):
    team = await new_team(client)
    [standard] = await list_templates(client, team)
    made = await make_template(client, team, "Detailed")
    response = await client.post(
        templates_url(team, f"/{made['id']}/make-default"), headers=team.owner.headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["message"] == "Default template changed"
    assert response.json()["data"]["is_default"] is True
    assert await default_of(team) == ["Detailed"]
    [entry] = await audit_entries(team.id, "invoice_template.made_default")
    assert entry["target_id"] == made["id"]
    assert entry["metadata"]["previous_default_id"] == standard["id"]
    assert entry["metadata"]["previous_default_name"] == "Standard"
    # Making the default the default again changes nothing (and logs nothing).
    again = await client.post(
        templates_url(team, f"/{made['id']}/make-default"), headers=team.owner.headers
    )
    assert again.status_code == 200 and again.json()["data"]["is_default"] is True
    assert len(await audit_entries(team.id, "invoice_template.made_default")) == 1
    # The old default can now be changed and deleted like any other.
    assert (
        await client.delete(templates_url(team, f"/{standard['id']}"), headers=team.owner.headers)
    ).status_code == 200


async def test_an_inactive_template_cannot_become_the_default(client):
    team = await new_team(client)
    made = await make_template(client, team, "Detailed")
    await client.patch(
        templates_url(team, f"/{made['id']}"),
        headers=team.owner.headers,
        json={"status": "inactive"},
    )
    response = await client.post(
        templates_url(team, f"/{made['id']}/make-default"), headers=team.owner.headers
    )
    assert response.status_code == 409 and error(response)["code"] == "TEMPLATE_INACTIVE"
    assert await default_of(team) == ["Standard"]


async def test_there_is_always_exactly_one_default_under_concurrency(client):
    team = await new_team(client)
    made = [await make_template(client, team, f"Template {n}") for n in range(4)]
    responses = await asyncio.gather(
        *(
            client.post(templates_url(team, f"/{t['id']}/make-default"), headers=team.owner.headers)
            for t in made
        ),
        client.post(
            templates_url(team),
            headers=team.owner.headers,
            json={"name": "Born default", "is_default": True},
        ),
    )
    assert all(r.status_code in (200, 201) for r in responses), [r.text for r in responses]
    assert len(await default_of(team)) == 1
    defaults = [t for t in await list_templates(client, team) if t["is_default"]]
    assert len(defaults) == 1


# --- Permissions --------------------------------------------------------------------------------


async def test_writing_templates_needs_settings_manage(client):
    team = await new_team(client)
    made = await make_template(client, team, "Detailed")
    other = await make_template(client, team, "Spare")
    for role in ("staff", "manager", "viewer", "billing"):
        member = await team.member(role)
        h = member.headers
        assert (
            await client.post(templates_url(team), headers=h, json={"name": f"By {role}"})
        ).status_code == 403, role
        assert (
            await client.patch(
                templates_url(team, f"/{made['id']}"), headers=h, json={"layout": "summary"}
            )
        ).status_code == 403, role
        assert (
            await client.delete(templates_url(team, f"/{other['id']}"), headers=h)
        ).status_code == 403, role
        assert (
            await client.post(templates_url(team, f"/{made['id']}/make-default"), headers=h)
        ).status_code == 403, role
    for role in ("admin", "partner"):
        member = await team.member(role)
        ok = await client.post(
            templates_url(team), headers=member.headers, json={"name": f"By {role}"}
        )
        assert ok.status_code == 201, role
    assert await default_of(team) == ["Standard"]
    outsider, _ = await create_user_with_org(client, "Elsewhere")
    assert (
        await client.post(templates_url(team), headers=outsider.headers, json={"name": "Nope"})
    ).status_code == 404
