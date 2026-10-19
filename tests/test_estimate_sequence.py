"""The estimate number sequence (kind ``estimate``): seeded for every practice with the other six,
formatted and gapless like them, forward-only like invoice numbers, and the only kinds the
database accepts are the seven documented ones."""

import asyncio
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models.practice import SEQUENCE_KINDS
from app.db.session import get_sessionmaker
from app.maintenance.bootstrap import bootstrap
from app.services.sequences import DEFAULT_SEQUENCES, next_number, take_number
from tests.clients_helpers import org_url, scalar, sql
from tests.domain_factories import practice, staff
from tests.factories import audit_entries, create_user_with_org, error

ESTIMATE_ROW = "SELECT prefix, padding, next_number FROM document_sequences WHERE organization_id = :o AND kind = 'estimate'"


async def update(client, actor, org: dict, body: dict, kind: str = "estimate"):
    return await client.patch(org_url(org, f"/sequences/{kind}"), headers=actor.headers, json=body)


async def listed(client, actor, org: dict) -> dict:
    response = await client.get(org_url(org, "/sequences"), headers=actor.headers)
    return {row["kind"]: row for row in response.json()["data"]}


def test_the_kinds_and_the_defaults_agree():
    assert len(SEQUENCE_KINDS) == 7 and "estimate" in SEQUENCE_KINDS
    assert set(SEQUENCE_KINDS) == set(DEFAULT_SEQUENCES)
    assert DEFAULT_SEQUENCES["estimate"] == ("EST", 5)


# --- Seeded for every practice -----------------------------------------------------------------


async def test_a_new_practice_gets_the_estimate_sequence(client):
    owner, org = await practice(client)
    assert await sql(ESTIMATE_ROW, o=org["id"]) == [
        {"prefix": "EST", "padding": 5, "next_number": 1}
    ]
    kinds = await sql("SELECT kind FROM document_sequences WHERE organization_id = :o", o=org["id"])
    assert {row["kind"] for row in kinds} == set(SEQUENCE_KINDS)
    estimate = (await listed(client, owner, org))["estimate"]
    assert (estimate["prefix"], estimate["padding"], estimate["next_number"]) == ("EST", 5, 1)
    assert estimate["preview"] == "EST00001"


async def test_the_bootstrap_command_seeds_it_too(db):
    result = await bootstrap(
        db, org_name="Acme", admin_name="Ada Admin", admin_email="ada@acme.example"
    )
    org_id = str(result.organization.id)
    assert await sql(ESTIMATE_ROW, o=org_id) == [{"prefix": "EST", "padding": 5, "next_number": 1}]
    assert (
        await scalar("SELECT count(*) FROM document_sequences WHERE organization_id = :o", o=org_id)
        == 7
    )


async def test_every_practice_numbers_its_estimates_on_its_own(client):
    owner, org = await practice(client)
    other_owner, other = await practice(client, "Other & Co")
    assert (
        await update(client, owner, org, {"prefix": "QUOTE-", "next_number": 40})
    ).status_code == 200
    assert (await listed(client, other_owner, other))["estimate"]["preview"] == "EST00001"
    assert await sql(ESTIMATE_ROW, o=other["id"]) == [
        {"prefix": "EST", "padding": 5, "next_number": 1}
    ]


# --- Taking numbers ------------------------------------------------------------------------------


async def test_estimate_numbers_are_formatted_and_gapless(client):
    _, org = await practice(client)
    org_id = uuid.UUID(org["id"])
    others_sql = (
        "SELECT kind, next_number FROM document_sequences "
        "WHERE organization_id = :o AND kind <> 'estimate' ORDER BY kind"
    )
    others_before = await sql(others_sql, o=org["id"])
    async with get_sessionmaker()() as session:
        assert await take_number(session, org_id, "estimate") == (1, "EST00001")
        await session.rollback()  # a rolled-back estimate frees its number
        assert await take_number(session, org_id, "estimate") == (1, "EST00001")
        assert await next_number(session, org_id, "estimate") == "EST00002"
        await session.commit()
    assert (await sql(ESTIMATE_ROW, o=org["id"]))[0]["next_number"] == 3
    # The other sequences are not moved by it.
    assert await sql(others_sql, o=org["id"]) == others_before


async def test_concurrent_estimates_get_distinct_consecutive_numbers(client):
    _, org = await practice(client)
    org_id = uuid.UUID(org["id"])

    async def take() -> tuple[int, str]:
        async with get_sessionmaker()() as session:
            taken = await take_number(session, org_id, "estimate")
            await session.commit()
            return taken

    results = await asyncio.gather(*(take() for _ in range(6)))
    assert sorted(number for number, _ in results) == [1, 2, 3, 4, 5, 6]
    assert sorted(text for _, text in results) == [f"EST{n:05d}" for n in range(1, 7)]
    assert (await sql(ESTIMATE_ROW, o=org["id"]))[0]["next_number"] == 7


async def test_a_practice_without_the_row_gets_it_on_first_use(client):
    _, org = await practice(client)
    await sql("DELETE FROM document_sequences WHERE organization_id = :o", o=org["id"])
    async with get_sessionmaker()() as session:
        assert await take_number(session, uuid.UUID(org["id"]), "estimate") == (1, "EST00001")
        await session.commit()
    assert await sql(ESTIMATE_ROW, o=org["id"]) == [
        {"prefix": "EST", "padding": 5, "next_number": 2}
    ]


# --- Changing it ---------------------------------------------------------------------------------


async def test_prefix_padding_and_next_number_can_be_set_and_are_audited(client):
    owner, org = await practice(client)
    response = await update(
        client, owner, org, {"prefix": "QUOTE-", "padding": 4, "next_number": 12}
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["kind"], data["prefix"], data["padding"], data["next_number"]) == (
        "estimate",
        "QUOTE-",
        4,
        12,
    )
    assert data["preview"] == "QUOTE-0012"
    [entry] = await audit_entries(org["id"], "sequence.updated")
    assert entry["metadata"] == {
        "kind": "estimate",
        "changes": {"prefix": ["EST", "QUOTE-"], "padding": [5, 4], "next_number": [1, 12]},
    }
    async with get_sessionmaker()() as session:
        assert await take_number(session, uuid.UUID(org["id"]), "estimate") == (12, "QUOTE-0012")
        await session.commit()
    assert (await listed(client, owner, org))["estimate"]["preview"] == "QUOTE-0013"
    # Prefix and padding do not need a next number, and the same values change nothing.
    again = await update(client, owner, org, {"prefix": "QUOTE-", "padding": 4})
    assert again.status_code == 200
    assert len(await audit_entries(org["id"], "sequence.updated")) == 1


async def test_estimate_numbers_only_move_forward(client):
    owner, org = await practice(client)
    assert (await update(client, owner, org, {"next_number": 20})).status_code == 200
    response = await update(client, owner, org, {"next_number": 19})
    assert response.status_code == 409
    assert error(response)["code"] == "SEQUENCE_BACKWARDS"
    assert error(response)["details"] == {"current_next_number": 20}
    assert (await sql(ESTIMATE_ROW, o=org["id"]))[0]["next_number"] == 20
    # The floor is the live counter: numbers taken since move it up.
    async with get_sessionmaker()() as session:
        for _ in range(3):
            await take_number(session, uuid.UUID(org["id"]), "estimate")
        await session.commit()
    assert (await update(client, owner, org, {"next_number": 22})).status_code == 409
    assert (await update(client, owner, org, {"next_number": 23})).status_code == 200
    # Skipping ahead is allowed: the gap is the practice's choice.
    assert (await update(client, owner, org, {"next_number": 500})).status_code == 200
    assert (await listed(client, owner, org))["estimate"]["preview"] == "EST00500"
    # A prefix change does not have to restate the number.
    assert (await update(client, owner, org, {"prefix": "E"})).status_code == 200
    assert (await listed(client, owner, org))["estimate"]["preview"] == "E00500"


async def test_changing_the_estimates_leaves_the_other_sequences_alone(client):
    owner, org = await practice(client)
    assert (
        await update(client, owner, org, {"prefix": "QUO", "next_number": 9})
    ).status_code == 200
    seen = await listed(client, owner, org)
    assert seen["invoice"]["preview"] == "INV-00001"
    assert seen["credit_note"]["preview"] == "CN-00001"
    assert seen["estimate"]["preview"] == "QUO00009"


@pytest.mark.parametrize(
    "body",
    [
        {"next_number": 0},
        {"next_number": 2_000_000_001},
        {"next_number": "abc"},
        {"padding": 0},
        {"padding": 11},
        {"prefix": None},
        {"prefix": "bad prefix"},
        {"prefix": "THIRTEENCHARS"},
    ],
)
async def test_invalid_values_are_422_and_change_nothing(client, body):
    owner, org = await practice(client)
    response = await update(client, owner, org, body)
    assert response.status_code == 422, response.text
    assert error(response)["details"][0]["field"] == next(iter(body))
    assert await sql(ESTIMATE_ROW, o=org["id"]) == [
        {"prefix": "EST", "padding": 5, "next_number": 1}
    ]
    assert await audit_entries(org["id"], "sequence.updated") == []


async def test_a_slash_is_allowed_in_the_prefix_because_estimates_are_not_codes(client):
    owner, org = await practice(client)
    response = await update(client, owner, org, {"prefix": "EST/26/"})
    assert response.status_code == 200 and response.json()["data"]["preview"] == "EST/26/00001"


# --- Who may and where ---------------------------------------------------------------------------


async def test_only_organization_update_changes_it_and_every_member_reads_it(client):
    _, org = await practice(client)
    expected = {
        "viewer": 403,
        "staff": 403,
        "billing": 403,
        "manager": 403,
        "admin": 200,
        "partner": 200,
    }
    for index, (role, status) in enumerate(expected.items(), start=2):
        actor = await staff(org, role)
        response = await update(client, actor, org, {"next_number": index})
        assert response.status_code == status, role
        assert (await listed(client, actor, org))["estimate"]["kind"] == "estimate", role
    assert len(await audit_entries(org["id"], "sequence.updated")) == 2


async def test_another_practice_cannot_touch_it(client):
    owner, org = await practice(client)
    stranger, _ = await create_user_with_org(client, "Stranger LLP")
    response = await update(client, stranger, org, {"next_number": 99})
    assert response.status_code == 404 and error(response)["code"] == "RESOURCE_NOT_FOUND"
    assert (await listed(client, owner, org))["estimate"]["next_number"] == 1
    assert (
        await client.get(org_url(org, "/sequences"), headers=stranger.headers)
    ).status_code == 404


# --- The database ---------------------------------------------------------------------------------


async def test_the_database_only_accepts_the_seven_kinds(client):
    _, org = await practice(client)
    with pytest.raises(IntegrityError, match="ck_document_sequences_kind"):
        await sql(
            "INSERT INTO document_sequences (organization_id, kind, prefix, padding, next_number) "
            "VALUES (:o, 'quote', 'Q', 5, 1)",
            o=org["id"],
        )
    # One row per practice and kind.
    with pytest.raises(IntegrityError, match="document_sequences"):
        await sql(
            "INSERT INTO document_sequences (organization_id, kind, prefix, padding, next_number) "
            "VALUES (:o, 'estimate', 'X', 5, 1)",
            o=org["id"],
        )
    # The existing bounds still apply to the new kind.
    for column, bad, constraint in (
        ("next_number", 0, "ck_document_sequences_next_number"),
        ("padding", 11, "ck_document_sequences_padding"),
        ("prefix", "no spaces", "ck_document_sequences_prefix_format"),
    ):
        values = {"next_number": 5, "padding": 5, "prefix": "EST", column: bad}
        with pytest.raises(IntegrityError, match=constraint):
            await sql(
                "UPDATE document_sequences SET next_number = :next_number, padding = :padding, "
                "prefix = :prefix WHERE organization_id = :o AND kind = 'estimate'",
                o=org["id"],
                **values,
            )
