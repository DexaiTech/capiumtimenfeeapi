"""The client list (Figma columns), filters, keyset sorting, header summary and CSV export."""

import base64
import csv
import io
import json
import uuid
from datetime import date, timedelta

import pytest

from tests.clients_helpers import count_queries, create_client, org_url, sql
from tests.domain_factories import (
    make_client,
    make_job,
    make_time_entry,
    practice,
    service_id,
    staff,
)
from tests.factories import error


async def list_clients(client, actor, org, query: str = "") -> dict:
    response = await client.get(org_url(org, f"/clients{query}"), headers=actor.headers)
    assert response.status_code == 200, response.text
    return response.json()


async def names(client, actor, org, query: str = "") -> list[str]:
    return [c["name"] for c in (await list_clients(client, actor, org, query))["data"]]


async def test_list_row_has_every_column_of_the_design(client):
    owner, org = await practice(client)
    acme = await create_client(
        client,
        owner,
        org,
        "Acme Widgets Ltd",
        company_number="12345678",
        segment="Gold",
        health_score=72,
        aml_status="clear",
        accounting_software="xero",
        email="office@acme.example",
        contacts=[{"name": "Jane Doe", "email": "jane@acme.example"}],
    )
    await client.post(
        org_url(org, f"/clients/{acme['id']}/services"),
        headers=owner.headers,
        json={"service_id": str(await service_id(org, "VAT"))},
    )
    bare = await create_client(client, owner, org, "Bare Client", email="bare@example.com")
    viewer = await staff(org, "viewer")
    body = await list_clients(client, viewer, org)
    assert body["meta"] == {"next_cursor": None, "has_more": False}
    rows = {r["name"]: r for r in body["data"]}
    row = rows["Acme Widgets Ltd"]
    assert row["initials"] == "AW"
    assert row["code"] == acme["code"]
    assert row["client_type"] == "limited_company"
    assert row["accounting_software"] == "xero"
    assert row["company_number"] == "12345678"
    assert row["segment"] == "Gold"
    assert row["health_score"] == 72
    assert row["contact_name"] == "Jane Doe"
    assert row["contact_email"] == "jane@acme.example"
    assert row["aml_status"] == "clear"
    assert row["services"] == [
        {"service_id": str(await service_id(org, "VAT")), "code": "VAT", "name": "VAT return"}
    ]
    assert row["last_activity_at"]
    assert row["deadlines"]["status"] == "none"
    # No contact: the UI shows "+ Add"; the email falls back to the client's.
    assert rows["Bare Client"]["contact_name"] is None
    assert rows["Bare Client"]["primary_contact"] is None
    assert rows["Bare Client"]["contact_email"] == "bare@example.com"
    assert rows["Bare Client"]["services"] == []
    assert bare["id"] in {r["id"] for r in body["data"]}


async def test_deadlines_from_open_jobs(client):
    owner, org = await practice(client)
    acme = await make_client(org, name="Acme")
    calm = await make_client(org, name="Calm")
    soon = await make_client(org, name="Soon")
    today = date.today()
    await make_job(org, acme, due_date=today - timedelta(days=3))
    await make_job(org, acme, due_date=today - timedelta(days=1))
    await make_job(org, acme, due_date=today)
    await make_job(org, acme, due_date=today + timedelta(days=5))
    await make_job(org, acme, due_date=today + timedelta(days=30))
    # Closed or deleted jobs do not count.
    await make_job(org, acme, due_date=today - timedelta(days=50), status="completed")
    await make_job(org, acme, due_date=today - timedelta(days=60), status="cancelled")
    await make_job(org, acme, due_date=today - timedelta(days=70), deleted_at=date(2026, 1, 1))
    await make_job(org, calm, due_date=today + timedelta(days=40))
    await make_job(org, calm, status="planned")
    await make_job(org, soon, due_date=today + timedelta(days=14), status="on_hold")
    rows = {r["name"]: r for r in (await list_clients(client, owner, org))["data"]}
    assert rows["Acme"]["deadlines"] == {
        "open_jobs": 5,
        "overdue": 2,
        "overdue_days": 3,
        "due_today": 1,
        "due_soon": 1,
        "due_soon_days": 14,
        "next_due_date": today.isoformat(),
        "status": "overdue",
    }
    assert rows["Calm"]["deadlines"]["status"] == "on_track"
    assert rows["Calm"]["deadlines"]["open_jobs"] == 2
    assert rows["Calm"]["deadlines"]["next_due_date"] == (today + timedelta(days=40)).isoformat()
    assert rows["Soon"]["deadlines"]["status"] == "due_soon"
    assert rows["Soon"]["deadlines"]["overdue_days"] == 0


async def test_last_activity_follows_time_jobs_and_invoices(client):
    owner, org = await practice(client)
    member = await staff(org)
    acme = await make_client(org, name="Acme")
    quiet = await make_client(org, name="Quiet")
    entry = await make_time_entry(org, member, client=acme)
    await sql("UPDATE time_entries SET description = 'later' WHERE id = :i", i=str(entry.id))
    rows = {r["name"]: r for r in (await list_clients(client, owner, org))["data"]}
    assert rows["Acme"]["last_activity_at"] > rows["Quiet"]["last_activity_at"]
    order = await names(client, owner, org, "?sort=last_activity_at&order=desc")
    assert order == ["Acme", "Quiet"]
    await make_job(org, quiet)
    order = await names(client, owner, org, "?sort=last_activity_at&order=desc")
    assert order == ["Quiet", "Acme"]
    assert str(quiet.id)


async def test_archived_clients_hidden_by_default(client):
    owner, org = await practice(client)
    for name, status in (("A", "active"), ("P", "prospect"), ("I", "inactive"), ("Z", "archived")):
        await make_client(org, name=name, status=status)
    assert await names(client, owner, org) == ["A", "I", "P"]
    assert await names(client, owner, org, "?status=archived") == ["Z"]
    assert await names(client, owner, org, "?status=active&status=archived") == ["A", "Z"]
    await make_client(org, name="Deleted", deleted_at=date(2026, 1, 1))
    assert "Deleted" not in await names(client, owner, org, "?status=active")


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        ("acme", ["Acme Widgets"]),
        ("WIDG", ["Acme Widgets"]),
        ("bz-9", ["Bravo Zulu"]),
        ("sc0042", ["Charlie"]),
        ("jane@CHARLIE", ["Charlie"]),
        ("office@bravo", ["Bravo Zulu"]),
        ("100%", []),
        ("nothing", []),
    ],
)
async def test_search(client, q, expected):
    owner, org = await practice(client)
    await create_client(client, owner, org, "Acme Widgets", code="AW1")
    await create_client(client, owner, org, "Bravo Zulu", code="BZ-9", email="office@bravo.example")
    await create_client(
        client,
        owner,
        org,
        "Charlie",
        company_number="SC0042",
        contacts=[{"name": "Jane", "email": "jane@charlie.example"}],
    )
    response = await client.get(org_url(org, "/clients"), headers=owner.headers, params={"q": q})
    assert [c["name"] for c in response.json()["data"]] == expected


async def test_filters(client):
    owner, org = await practice(client)
    manager = await staff(org, "manager")
    partner = await staff(org, "partner")
    vat = await service_id(org, "VAT")
    a = await make_client(
        org,
        name="A",
        segment="Gold",
        client_type="sole_trader",
        aml_status="pending",
        accounting_software="xero",
        manager_user_id=manager.id,
    )
    b = await make_client(
        org, name="B", segment="gold", aml_status="clear", partner_user_id=partner.id
    )
    await make_client(org, name="C", segment="Silver", accounting_software="sage")
    await client.post(
        org_url(org, f"/clients/{a.id}/services"),
        headers=owner.headers,
        json={"service_id": str(vat)},
    )
    inactive = await client.post(
        org_url(org, f"/clients/{b.id}/services"),
        headers=owner.headers,
        json={"service_id": str(vat), "is_active": False},
    )
    assert inactive.status_code == 201
    assert await names(client, owner, org, "?segment=GOLD") == ["A", "B"]
    assert await names(client, owner, org, "?segment=gold&segment=silver") == ["A", "B", "C"]
    assert await names(client, owner, org, "?client_type=sole_trader") == ["A"]
    assert await names(client, owner, org, "?aml_status=clear&aml_status=pending") == ["A", "B"]
    assert await names(client, owner, org, "?accounting_software=sage") == ["C"]
    assert await names(client, owner, org, f"?service_id={vat}") == ["A"]
    assert await names(client, owner, org, f"?manager_user_id={manager.id}") == ["A"]
    assert await names(client, owner, org, f"?partner_user_id={partner.id}") == ["B"]
    assert await names(client, owner, org, "?segment=gold&aml_status=clear") == ["B"]


@pytest.mark.parametrize(
    "query",
    ["?status=gone", "?client_type=plc", "?sort=colour", "?order=up", "?limit=0", "?limit=101"],
)
async def test_bad_list_parameters_are_422(client, query):
    owner, org = await practice(client)
    response = await client.get(org_url(org, f"/clients{query}"), headers=owner.headers)
    assert response.status_code == 422


async def collect(client, actor, org, query: str, limit: int = 2) -> list[str]:
    seen: list[str] = []
    cursor = None
    for _ in range(20):
        params = f"{query}&limit={limit}" + (f"&cursor={cursor}" if cursor else "")
        body = await list_clients(client, actor, org, params)
        seen += [c["name"] for c in body["data"]]
        cursor = body["meta"]["next_cursor"]
        if cursor is None:
            return seen
    raise AssertionError("pagination did not end")


async def test_keyset_pagination_for_every_sort(client):
    owner, org = await practice(client)
    rows = [
        ("delta", "D1", "Gold", 50, "clear"),
        ("Alpha", "A1", None, None, "pending"),
        ("charlie", "C1", "Bronze", 90, "not_started"),
        ("Bravo", "B1", "gold", 50, "failed"),
        ("echo", "E1", "Silver", 10, "review"),
    ]
    for name, code, segment, health, aml in rows:
        await create_client(
            client,
            owner,
            org,
            name,
            code=code,
            segment=segment,
            health_score=health,
            aml_status=aml,
        )
    by_name = ["Alpha", "Bravo", "charlie", "delta", "echo"]
    assert await collect(client, owner, org, "?sort=name") == by_name
    assert await collect(client, owner, org, "?sort=name&order=desc") == by_name[::-1]
    assert await collect(client, owner, org, "?sort=code") == by_name
    assert await collect(client, owner, org, "?sort=created_at") == [r[0] for r in rows]
    assert (
        await collect(client, owner, org, "?sort=created_at&order=desc")
        == [r[0] for r in rows][::-1]
    )
    health = await collect(client, owner, org, "?sort=health_score&order=desc", limit=1)
    assert health[0] == "charlie" and health[-1] == "Alpha"  # nulls last when descending
    assert set(health[1:3]) == {"delta", "Bravo"}
    segment = await collect(client, owner, org, "?sort=segment")
    assert segment[0] == "Alpha" and segment[1] == "charlie" and segment[-1] == "echo"
    aml = await collect(client, owner, org, "?sort=aml_status")
    assert aml == ["delta", "Bravo", "charlie", "Alpha", "echo"]
    activity = await collect(client, owner, org, "?sort=last_activity_at&order=desc")
    assert sorted(activity) == sorted(by_name)


async def test_cursor_of_another_sort_is_rejected(client):
    owner, org = await practice(client)
    for name in ("A", "B", "C"):
        await make_client(org, name=name)
    body = await list_clients(client, owner, org, "?sort=name&limit=1")
    cursor = body["meta"]["next_cursor"]
    response = await client.get(
        org_url(org, f"/clients?sort=code&limit=1&cursor={cursor}"), headers=owner.headers
    )
    assert response.status_code == 422
    assert error(response)["details"][0]["field"] == "cursor"
    response = await client.get(org_url(org, "/clients?cursor=not-a-cursor"), headers=owner.headers)
    assert response.status_code == 422


def _forged_cursor(sort: str, kind: str, value: object, row_id: object = None) -> str:
    payload = {
        "s": sort,
        "d": False,
        "k": kind,
        "v": value,
        "id": str(uuid.uuid4()) if row_id is None else row_id,
    }
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    ("sort", "kind", "value", "row_id"),
    [
        ("name", "raw", 5, None),  # text key, number value
        ("code", "dt", "2026-01-01T00:00:00+00:00", None),  # text key, timestamp value
        ("segment", "raw", "nul\x00byte", None),  # Postgres text cannot hold NUL
        ("health_score", "raw", "abc", None),  # integer key, text value
        ("health_score", "raw", 10**30, None),  # out of range
        ("health_score", "raw", True, None),  # bool is not a score
        ("created_at", "raw", "abc", None),  # timestamp key, raw text
        ("created_at", "dt", "2026-01-01T00:00:00", None),  # naive timestamp
        ("last_activity_at", "raw", 7, None),
        ("name", "raw", "x", 123),  # id must be a uuid string
        ("name", "raw", "x", "not-a-uuid"),
    ],
)
async def test_tampered_cursor_is_422_not_a_server_error(client, sort, kind, value, row_id):
    """A cursor of the right sort/order whose key does not have the sort key's type must be
    rejected before it reaches the database as a mistyped parameter (was a 500)."""
    owner, org = await practice(client)
    await make_client(org, name="Solo")
    response = await client.get(
        org_url(org, "/clients"),
        headers=owner.headers,
        params={"sort": sort, "cursor": _forged_cursor(sort, kind, value, row_id)},
    )
    assert response.status_code == 422, response.text
    assert error(response)["details"][0]["field"] == "cursor"


async def test_initials_use_unicode_letters(client):
    owner, org = await practice(client)
    await make_client(org, name="Émile Zola & Fils")
    await make_client(org, name="Øresund")
    rows = {c["name"]: c["initials"] for c in (await list_clients(client, owner, org))["data"]}
    assert rows == {"Émile Zola & Fils": "ÉZ", "Øresund": "ØR"}


async def test_list_is_one_query_plus_batched_lookups(client):
    owner, org = await practice(client)
    member = await staff(org)
    vat = await service_id(org, "VAT")

    async def add(count: int) -> None:
        for _ in range(count):
            row = await make_client(org)
            await make_job(org, row, due_date=date.today())
            await make_time_entry(org, member, client=row)
            await sql(
                "INSERT INTO client_contacts (id, organization_id, client_id, name, is_primary) "
                "VALUES (gen_random_uuid(), :o, :c, 'P', true)",
                o=org["id"],
                c=str(row.id),
            )
            await sql(
                "INSERT INTO client_services (id, organization_id, client_id, service_id) "
                "VALUES (gen_random_uuid(), :o, :c, :s)",
                o=org["id"],
                c=str(row.id),
                s=str(vat),
            )

    await add(2)
    with count_queries() as few:
        await list_clients(client, owner, org)
    await add(8)
    with count_queries() as many:
        body = await list_clients(client, owner, org)
    assert len(body["data"]) == 10
    assert len(many) == len(few), (len(few), len(many))
    assert all(row["deadlines"]["due_today"] == 1 for row in body["data"])


async def test_list_permissions_and_tenancy(client):
    _owner, org = await practice(client)
    other_owner, other = await practice(client, "Other")
    await make_client(org, name="Mine")
    await make_client(other, name="Theirs")
    for role in ("staff", "viewer", "billing", "manager"):
        actor = await staff(org, role)
        assert await names(client, actor, org) == ["Mine"]
    response = await client.get(org_url(org, "/clients"), headers=other_owner.headers)
    assert response.status_code == 404
    response = await client.get(org_url(org, "/clients/summary"), headers=other_owner.headers)
    assert response.status_code == 404


# --- Summary ----------------------------------------------------------------------------


async def test_summary_counts(client):
    _owner, org = await practice(client)
    await staff(org)
    await staff(org, "manager")
    await staff(org, status="suspended")
    a = await make_client(
        org, status="active", segment="Gold", aml_status="clear", accounting_software="xero"
    )
    await make_client(org, status="active", segment="gold ", client_type="llp")
    await make_client(org, status="prospect", segment="Silver")
    await make_client(org, status="inactive")
    await make_client(org, status="archived", segment="Gold")
    await make_client(org, status="active", deleted_at=date(2026, 1, 1))
    await make_job(org, a, due_date=date.today() - timedelta(days=1))
    viewer = await staff(org, "viewer")
    response = await client.get(org_url(org, "/clients/summary"), headers=viewer.headers)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 4
    assert data["by_status"] == {"prospect": 1, "active": 2, "inactive": 1, "archived": 1}
    assert data["by_client_type"] == {"limited_company": 3, "llp": 1}
    assert data["by_aml_status"]["clear"] == 1
    assert data["by_aml_status"]["not_started"] == 3
    assert data["by_accounting_software"] == {"xero": 1}
    segments = {
        s["segment"].lower() if s["segment"] else None: s["count"] for s in data["by_segment"]
    }
    assert segments == {"gold": 2, "silver": 1, None: 1}
    assert data["with_overdue_deadlines"] == 1
    # Owner, staff, manager and viewer are active; the suspended one is not.
    assert data["users"] == 4


# --- Export -----------------------------------------------------------------------------


async def test_export_csv_uses_the_list_filters(client):
    owner, org = await practice(client)
    await create_client(
        client,
        owner,
        org,
        "Acme Widgets Ltd",
        code="ACME",
        segment="Gold",
        contacts=[{"name": '=HYPERLINK("x")', "email": "jane@acme.example"}],
    )
    await create_client(client, owner, org, "Bravo", code="BRAVO", segment="Silver")
    await make_client(org, name="Archived", status="archived")
    member = await staff(org)
    response = await client.get(
        org_url(org, "/clients/export?format=csv&segment=gold"), headers=member.headers
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert response.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert [r["code"] for r in rows] == ["ACME"]
    assert rows[0]["contact_name"].startswith("'=")  # formula neutralised
    assert rows[0]["contact_email"] == "jane@acme.example"
    assert rows[0]["deadline_status"] == "none"

    everything = await client.get(
        org_url(org, "/clients/export?sort=code&order=desc"), headers=member.headers
    )
    codes = [r["code"] for r in csv.DictReader(io.StringIO(everything.content.decode("utf-8-sig")))]
    assert codes == ["BRAVO", "ACME"]
    assert (
        await client.get(org_url(org, "/clients/export?format=xlsx"), headers=member.headers)
    ).status_code == 422


async def test_export_spans_several_batches(client, monkeypatch):
    from app.services import clients as clients_service

    monkeypatch.setattr(clients_service, "EXPORT_BATCH", 2)
    owner, org = await practice(client)
    for i in range(5):
        await make_client(org, name=f"Client {i}")
    response = await client.get(org_url(org, "/clients/export"), headers=owner.headers)
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert [r["name"] for r in rows] == [f"Client {i}" for i in range(5)]
