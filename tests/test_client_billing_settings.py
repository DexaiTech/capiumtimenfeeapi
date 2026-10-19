"""Client billing settings: the invoice email, the default service and the invoice template."""

from app.db.models import ClientContact
from app.db.session import get_sessionmaker
from tests.billing_helpers import draft, issued, new_practice
from tests.billing_helpers import org_url as billing_url
from tests.central_helpers import new_team, scalar
from tests.clients_helpers import create_client, org_url
from tests.conftest import run_jobs
from tests.domain_factories import make_time_entry, service_id
from tests.factories import audit_entries, create_user_with_org, error

SETTINGS = ("invoice_email", "default_service_id", "invoice_template_id")


async def standard_template(client, team) -> dict:
    response = await client.get(org_url(team.org, "/invoice-templates"), headers=team.owner.headers)
    return response.json()["data"][0]


async def new_template(client, team, name: str = "Detailed", **body) -> dict:
    response = await client.post(
        org_url(team.org, "/invoice-templates"),
        headers=team.owner.headers,
        json={"name": name, **body},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def patch(client, team, client_id: str, **body):
    return client.patch(
        org_url(team.org, f"/clients/{client_id}"), headers=team.owner.headers, json=body
    )


# --- Create, read, change -------------------------------------------------------------------


async def test_a_client_is_created_and_read_with_its_billing_settings(client):
    team = await new_team(client)
    standard = await standard_template(client, team)
    accs = await service_id(team.org, "ACCS")
    response = await client.post(
        org_url(team.org, "/clients"),
        headers=team.owner.headers,
        json={
            "name": "Acme Widgets Ltd",
            "invoice_email": "accounts@acme.example",
            "default_service_id": str(accs),
            "invoice_template_id": standard["id"],
        },
    )
    assert response.status_code == 201, response.text
    created = response.json()["data"]
    assert created["invoice_email"] == "accounts@acme.example"
    assert (created["default_service_id"], created["invoice_template_id"]) == (
        str(accs),
        standard["id"],
    )
    assert created["default_service"] == {
        "service_id": str(accs),
        "code": "ACCS",
        "name": created["default_service"]["name"],
    }
    assert created["default_service"]["name"]
    assert created["invoice_template"] == {"id": standard["id"], "name": "Standard"}
    # The same fields on the detail and in the list.
    detail = (
        await client.get(org_url(team.org, f"/clients/{created['id']}"), headers=team.owner.headers)
    ).json()["data"]
    assert detail["invoice_email"] == "accounts@acme.example"
    listed = (await client.get(org_url(team.org, "/clients"), headers=team.owner.headers)).json()[
        "data"
    ][0]
    assert [listed[f] for f in SETTINGS] == [
        "accounts@acme.example",
        str(accs),
        standard["id"],
    ]
    # A client created without them has none (and the detail says so).
    bare = await create_client(client, team.owner, team.org, "Bare Ltd")
    assert [bare[f] for f in SETTINGS] == [None, None, None]
    assert (bare["default_service"], bare["invoice_template"]) == (None, None)


async def test_billing_settings_can_be_changed_and_cleared_and_are_audited(client):
    team = await new_team(client)
    acme = await create_client(client, team.owner, team.org, "Acme Widgets Ltd")
    detailed = await new_template(client, team)
    vat = await service_id(team.org, "VAT")
    response = await patch(
        client,
        team,
        acme["id"],
        invoice_email="Accounts@Acme.example",
        default_service_id=str(vat),
        invoice_template_id=detailed["id"],
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["invoice_email"].lower() == "accounts@acme.example"
    assert (data["default_service_id"], data["invoice_template_id"]) == (str(vat), detailed["id"])
    assert data["default_service"]["code"] == "VAT"
    assert data["invoice_template"] == {"id": detailed["id"], "name": "Detailed"}
    [entry] = await audit_entries(team.id, "client.updated")
    changes = entry["metadata"]["changes"]
    assert set(changes) == set(SETTINGS)
    assert changes["default_service_id"] == [None, str(vat)]
    assert changes["invoice_template_id"] == [None, detailed["id"]]
    assert changes["invoice_email"][0] is None

    # null clears each one; so does a blank email.
    cleared = await patch(
        client,
        team,
        acme["id"],
        invoice_email=None,
        default_service_id=None,
        invoice_template_id=None,
    )
    assert [cleared.json()["data"][f] for f in SETTINGS] == [None, None, None]
    assert (
        cleared.json()["data"]["default_service"],
        cleared.json()["data"]["invoice_template"],
    ) == (None, None)
    await patch(client, team, acme["id"], invoice_email="back@acme.example")
    blank = await patch(client, team, acme["id"], invoice_email="   ")
    assert blank.status_code == 200 and blank.json()["data"]["invoice_email"] is None
    # Nothing new, nothing audited.
    before = len(await audit_entries(team.id, "client.updated"))
    same = await patch(client, team, acme["id"], invoice_email=None)
    assert same.status_code == 200
    assert len(await audit_entries(team.id, "client.updated")) == before


async def test_the_invoice_email_is_validated(client):
    team = await new_team(client)
    acme = await create_client(client, team.owner, team.org, "Acme Widgets Ltd")
    too_long = "a" * 250 + "@acme.example"
    for bad in ("not-an-email", "two@@acme.example", "spaces in@acme.example", too_long):
        response = await patch(client, team, acme["id"], invoice_email=bad)
        assert response.status_code == 422, bad
        assert response.json()["error"]["details"][0]["field"] == "invoice_email", bad
        created = await client.post(
            org_url(team.org, "/clients"),
            headers=team.owner.headers,
            json={"name": "Other", "invoice_email": bad},
        )
        assert created.status_code == 422, bad
    assert (await scalar("SELECT invoice_email FROM clients WHERE id = :i", i=acme["id"])) is None
    assert (await patch(client, team, acme["id"], invoice_email=123)).status_code == 422


# --- The default service ------------------------------------------------------------------------


async def test_the_default_service_must_be_an_active_service_of_the_practice(client):
    team = await new_team(client)
    _, other_org = await create_user_with_org(client, "Second Practice")
    foreign = await service_id(other_org, "ACCS")
    books = await service_id(team.org, "BOOKS")
    acme = await create_client(client, team.owner, team.org, "Acme Widgets Ltd")
    for bad in (foreign, "00000000-0000-4000-8000-000000000000"):
        response = await patch(client, team, acme["id"], default_service_id=str(bad))
        assert response.status_code == 404, response.text
        assert error(response)["code"] == "RESOURCE_NOT_FOUND"
        created = await client.post(
            org_url(team.org, "/clients"),
            headers=team.owner.headers,
            json={"name": "Other", "default_service_id": str(bad)},
        )
        assert created.status_code == 404
    assert (await patch(client, team, acme["id"], default_service_id="nope")).status_code == 422
    # An inactive service is not offered: 422 on the field.
    off = await client.post(
        org_url(team.org, f"/services/{books}/deactivate"), headers=team.owner.headers
    )
    assert off.status_code == 200
    inactive = await patch(client, team, acme["id"], default_service_id=str(books))
    assert inactive.status_code == 422, inactive.text
    assert inactive.json()["error"]["details"][0]["field"] == "default_service_id"
    created = await client.post(
        org_url(team.org, "/clients"),
        headers=team.owner.headers,
        json={"name": "Other", "default_service_id": str(books)},
    )
    assert created.status_code == 422
    assert await scalar("SELECT count(*) FROM clients WHERE organization_id = :o", o=team.id) == 1


async def test_a_default_service_that_later_goes_inactive_does_not_block_other_edits(client):
    team = await new_team(client)
    books = await service_id(team.org, "BOOKS")
    payroll = await service_id(team.org, "PAYROLL")
    acme = await create_client(
        client, team.owner, team.org, "Acme Widgets Ltd", default_service_id=str(books)
    )
    await client.post(
        org_url(team.org, f"/services/{books}/deactivate"), headers=team.owner.headers
    )
    await client.post(
        org_url(team.org, f"/services/{payroll}/deactivate"), headers=team.owner.headers
    )
    # Not re-validated while unchanged ...
    assert (await patch(client, team, acme["id"], notes="Still fine")).status_code == 200
    assert (await patch(client, team, acme["id"], default_service_id=str(books))).status_code == 200
    # ... but another inactive service is refused, and clearing is always possible.
    assert (
        await patch(client, team, acme["id"], default_service_id=str(payroll))
    ).status_code == 422
    cleared = await patch(client, team, acme["id"], default_service_id=None)
    assert cleared.status_code == 200 and cleared.json()["data"]["default_service_id"] is None


async def test_a_service_that_is_a_clients_default_is_deactivated_not_deleted(client):
    team = await new_team(client)
    secretarial = await service_id(team.org, "SECRETARIAL")
    unused = await service_id(team.org, "PAYROLL")
    await create_client(
        client, team.owner, team.org, "Acme Widgets Ltd", default_service_id=str(secretarial)
    )
    services = (
        await client.get(org_url(team.org, "/services"), headers=team.owner.headers)
    ).json()["data"]
    used = {s["code"]: s["is_used"] for s in services}
    assert used["SECRETARIAL"] is True and used["PAYROLL"] is False
    deleted = await client.delete(
        org_url(team.org, f"/services/{secretarial}"), headers=team.owner.headers
    )
    assert deleted.status_code == 200 and deleted.json()["data"]["outcome"] == "deactivated"
    assert await scalar("SELECT is_active FROM services WHERE id = :i", i=str(secretarial)) is False
    unused_result = await client.delete(
        org_url(team.org, f"/services/{unused}"), headers=team.owner.headers
    )
    assert unused_result.json()["data"]["outcome"] == "deleted"


# --- The invoice template -----------------------------------------------------------------------


async def test_the_invoice_template_must_be_an_active_live_template_of_the_practice(client):
    team = await new_team(client)
    other_owner, other_org = await create_user_with_org(client, "Second Practice")
    foreign = (
        await client.get(org_url(other_org, "/invoice-templates"), headers=other_owner.headers)
    ).json()["data"][0]
    acme = await create_client(client, team.owner, team.org, "Acme Widgets Ltd")
    for bad in (foreign["id"], "00000000-0000-4000-8000-000000000000"):
        response = await patch(client, team, acme["id"], invoice_template_id=bad)
        assert response.status_code == 404, response.text
        created = await client.post(
            org_url(team.org, "/clients"),
            headers=team.owner.headers,
            json={"name": "Other", "invoice_template_id": bad},
        )
        assert created.status_code == 404
    assert (await patch(client, team, acme["id"], invoice_template_id="nope")).status_code == 422
    retired = await new_template(client, team, "Retired")
    await client.patch(
        org_url(team.org, f"/invoice-templates/{retired['id']}"),
        headers=team.owner.headers,
        json={"status": "inactive"},
    )
    inactive = await patch(client, team, acme["id"], invoice_template_id=retired["id"])
    assert inactive.status_code == 422, inactive.text
    assert inactive.json()["error"]["details"][0]["field"] == "invoice_template_id"
    gone = await new_template(client, team, "Gone")
    await client.delete(
        org_url(team.org, f"/invoice-templates/{gone['id']}"), headers=team.owner.headers
    )
    assert (
        await patch(client, team, acme["id"], invoice_template_id=gone["id"])
    ).status_code == 404
    assert (
        await scalar("SELECT invoice_template_id FROM clients WHERE id = :i", i=acme["id"])
    ) is None


async def test_billing_settings_need_client_update(client):
    team = await new_team(client)
    acme = await create_client(client, team.owner, team.org, "Acme Widgets Ltd")
    for role in ("staff", "viewer"):
        member = await team.member(role)
        response = await client.patch(
            org_url(team.org, f"/clients/{acme['id']}"),
            headers=member.headers,
            json={"invoice_email": "x@acme.example"},
        )
        assert response.status_code == 403, role
    billing = await team.member("billing")
    done = await client.patch(
        org_url(team.org, f"/clients/{acme['id']}"),
        headers=billing.headers,
        json={"invoice_email": "x@acme.example"},
    )
    assert done.status_code == 200


# --- Invoice emails go to the invoice email first -------------------------------------------------


async def add_contact(client_row, **fields) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            ClientContact(
                organization_id=client_row.organization_id, client_id=client_row.id, **fields
            )
        )
        await session.commit()


async def issued_invoice(client, p) -> dict:
    entry = await make_time_entry(p.org, p.owner, client=p.client)
    invoice = await draft(client, p.owner, p.org, p.client.id, time=[entry])
    return await issued(client, p.owner, p.org, invoice["id"])


async def send(client, p, invoice: dict, **body):
    return await client.post(
        billing_url(p.org, f"/invoices/{invoice['id']}/send"),
        headers=p.owner.headers,
        json=body or None,
    )


async def test_invoices_are_emailed_to_the_invoice_email_first(client, worker, mailbox):
    p = await new_practice(client)
    await add_contact(p.client, name="Pat Primary", email="pat@widgets.example", is_primary=True)
    await add_contact(p.client, name="Bill Payer", email="bills@widgets.example", is_billing=True)
    set_up = await client.patch(
        billing_url(p.org, f"/clients/{p.client.id}"),
        headers=p.owner.headers,
        json={"email": "hello@widgets.example", "invoice_email": "accounts@widgets.example"},
    )
    assert set_up.status_code == 200, set_up.text

    first = await issued_invoice(client, p)
    response = await send(client, p, first)
    assert response.status_code == 202, response.text
    assert response.json()["data"]["to"] == ["accounts@widgets.example"]
    assert await run_jobs(worker) == 1
    [message] = mailbox.to("accounts@widgets.example")
    assert "Invoice INV-00001" in message["Subject"]
    assert mailbox.to("bills@widgets.example") == [] and mailbox.to("pat@widgets.example") == []

    # Without an invoice email it is the billing contacts, then the primary contact, then the client.
    await client.patch(
        billing_url(p.org, f"/clients/{p.client.id}"),
        headers=p.owner.headers,
        json={"invoice_email": None},
    )
    second = await issued_invoice(client, p)
    assert (await send(client, p, second)).json()["data"]["to"] == ["bills@widgets.example"]
    assert await run_jobs(worker) == 1

    # Addresses given in the request always win.
    await client.patch(
        billing_url(p.org, f"/clients/{p.client.id}"),
        headers=p.owner.headers,
        json={"invoice_email": "accounts@widgets.example"},
    )
    third = await issued_invoice(client, p)
    explicit = await send(client, p, third, to=["boss@widgets.example"])
    assert explicit.json()["data"]["to"] == ["boss@widgets.example"]
    assert await run_jobs(worker) == 1
    assert len(mailbox.to("boss@widgets.example")) == 1
    assert len(mailbox.to("accounts@widgets.example")) == 1  # still only the first invoice


async def test_the_invoice_email_alone_is_enough_to_send(client):
    p = await new_practice(client)
    # A client with no contacts and no email of its own has nobody to send to ...
    refused = await send(client, p, await issued_invoice(client, p))
    assert refused.status_code == 422 and error(refused)["code"] == "NO_RECIPIENT"
    # ... until it has an invoice email.
    await client.patch(
        billing_url(p.org, f"/clients/{p.client.id}"),
        headers=p.owner.headers,
        json={"invoice_email": "accounts@widgets.example"},
    )
    again = await send(client, p, await issued_invoice(client, p))
    assert again.status_code == 202 and again.json()["data"]["to"] == ["accounts@widgets.example"]
