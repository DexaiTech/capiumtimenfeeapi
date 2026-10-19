"""Paging the items behind the historic WIP report (`GET /reports/wip/items`)."""

from datetime import date, timedelta

from tests.billing_helpers import new_practice
from tests.domain_factories import make_disbursement
from tests.factories import error

TODAY = date.today()


async def test_the_items_are_paged_with_limit_and_cursor(client):
    p = await new_practice(client)
    for days, amount in ((30, 1_000), (20, 2_000), (10, 3_000)):
        await make_disbursement(p.org, p.client, amount, incurred_on=TODAY - timedelta(days=days))
    url = p.url("/reports/wip/items")

    first = await client.get(url, headers=p.owner.headers, params={"limit": 2})
    assert first.status_code == 200, first.text
    body = first.json()
    assert [item["value"] for item in body["data"]] == [1_000, 2_000]
    assert body["meta"]["has_more"] is True

    second = await client.get(
        url,
        headers=p.owner.headers,
        params={"limit": 2, "cursor": body["meta"]["next_cursor"]},
    )
    assert second.status_code == 200, second.text
    assert [item["value"] for item in second.json()["data"]] == [3_000]
    assert second.json()["meta"]["has_more"] is False


async def test_the_page_size_is_checked_and_unknown_parameters_are_still_refused(client):
    p = await new_practice(client)
    url = p.url("/reports/wip/items")

    too_small = await client.get(url, headers=p.owner.headers, params={"limit": 0})
    assert too_small.status_code == 422
    assert error(too_small)["code"] == "VALIDATION_ERROR"

    unknown = await client.get(url, headers=p.owner.headers, params={"offset": 5})
    assert unknown.status_code == 422
