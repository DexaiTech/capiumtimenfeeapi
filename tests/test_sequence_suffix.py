"""The number suffix of invoices, credit notes and estimates (``INV-00042/26``)."""

import pytest

from app.services.sequences import format_number
from tests.clients_helpers import org_url
from tests.domain_factories import practice
from tests.factories import error


def test_format_number_puts_the_suffix_last():
    assert format_number("INV-", 42, 5) == "INV-00042"
    assert format_number("INV-", 42, 5, "/26") == "INV-00042/26"


async def update(client, actor, org, body, kind="invoice"):
    return await client.patch(org_url(org, f"/sequences/{kind}"), headers=actor.headers, json=body)


async def test_every_sequence_starts_without_a_suffix(client):
    owner, org = await practice(client)
    rows = (await client.get(org_url(org, "/sequences"), headers=owner.headers)).json()["data"]
    assert rows and all(row["suffix"] == "" for row in rows)


@pytest.mark.parametrize("kind", ["invoice", "credit_note", "estimate"])
async def test_a_document_number_can_have_a_suffix(client, kind):
    owner, org = await practice(client)
    response = await update(client, owner, org, {"suffix": "/26"}, kind)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["suffix"] == "/26" and data["preview"].endswith("00001/26")


async def test_the_suffix_can_be_cleared_again(client):
    owner, org = await practice(client)
    await update(client, owner, org, {"suffix": "-A"})
    response = await update(client, owner, org, {"suffix": ""})
    assert response.json()["data"]["preview"] == "INV-00001"


@pytest.mark.parametrize("kind", ["client", "staff", "job", "ticket"])
async def test_codes_do_not_take_a_suffix(client, kind):
    owner, org = await practice(client)
    response = await update(client, owner, org, {"suffix": "X"}, kind)
    assert response.status_code == 422
    assert error(response)["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("bad", ["a b", "x" * 13, "é", "#1"])
async def test_a_suffix_is_letters_digits_and_a_few_signs(client, bad):
    owner, org = await practice(client)
    assert (await update(client, owner, org, {"suffix": bad})).status_code == 422
