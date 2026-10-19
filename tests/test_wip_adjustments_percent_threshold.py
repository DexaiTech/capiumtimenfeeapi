"""The percent-of-WIP write-off approval rule, alongside the existing flat-pence threshold.

`WipAdjustmentService.create` (app/services/wip_adjustments.py): a write-off above the flat
`wip_adjustment_approval_threshold` OR above `wip_write_off_approval_threshold_percent` of the
client's (or job's) current WIP needs approval; either rule alone is enough to require it. The
percent rule only applies to `write_off` (write-ons add WIP, there is nothing to measure a
percentage of removing), and is off entirely when the organization's percent threshold is null.
"""

from tests.clients_helpers import org_url, scalar
from tests.domain_factories import make_client, make_time_entry, practice


async def set_thresholds(client, owner, org, *, flat: int, percent: int | None) -> None:
    body = {"wip_adjustment_approval_threshold": flat}
    response = await client.patch(
        org_url(org),
        headers=owner.headers,
        json={**body, "wip_write_off_approval_threshold_percent": percent},
    )
    assert response.status_code == 200, response.text


async def adjust(client, owner, org, acme, *, kind: str, amount: int):
    return await client.post(
        org_url(org, "/wip/adjustments"),
        headers=owner.headers,
        json={
            "client_id": str(acme.id),
            "kind": kind,
            "amount": amount,
            "reason": "Test adjustment",
        },
    )


async def seed_wip(org, owner, acme, *, minutes: int = 600, charge_rate: int = 6000) -> None:
    """£600 of unbilled, approved time WIP (600 minutes at £60.00/hour)."""
    await make_time_entry(
        org,
        owner,
        client=acme,
        minutes=minutes,
        charge_rate=charge_rate,
        timesheet_status="approved",
        billing_status="unbilled",
    )


async def test_a_write_off_above_the_percentage_of_wip_needs_approval_even_under_the_flat_cap(
    client,
):
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd", code="ACME")
    await seed_wip(org, owner, acme)
    # Flat threshold is generous (£1000); the percent threshold (10% of £600 = £60) is not.
    await set_thresholds(client, owner, org, flat=100_000, percent=1000)

    response = await adjust(client, owner, org, acme, kind="write_off", amount=7_000)  # £70
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["status"] == "pending"
    assert data["approval_required"] is True


async def test_a_write_off_at_or_below_the_percentage_is_approved_at_once(client):
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd", code="ACME")
    await seed_wip(org, owner, acme)
    await set_thresholds(client, owner, org, flat=100_000, percent=1000)

    response = await adjust(client, owner, org, acme, kind="write_off", amount=3_000)  # £30
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["status"] == "approved"
    assert data["approval_required"] is False


async def test_the_flat_threshold_still_applies_on_its_own(client):
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd", code="ACME")
    await seed_wip(org, owner, acme)
    # Percent threshold off; flat threshold is small enough to catch this amount by itself.
    await set_thresholds(client, owner, org, flat=5_000, percent=None)

    response = await adjust(client, owner, org, acme, kind="write_off", amount=7_000)  # £70
    assert response.status_code == 201, response.text
    assert response.json()["data"]["status"] == "pending"


async def test_the_percentage_rule_is_off_when_the_threshold_is_null(client):
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd", code="ACME")
    await seed_wip(org, owner, acme)
    await set_thresholds(client, owner, org, flat=100_000, percent=None)

    # £70 is well above 10% of £600 (£60), but the percent rule is off and the flat
    # threshold (£1000) is not reached, so this still goes through at once.
    response = await adjust(client, owner, org, acme, kind="write_off", amount=7_000)
    assert response.status_code == 201, response.text
    assert response.json()["data"]["status"] == "approved"


async def test_the_percentage_rule_does_not_apply_to_a_write_on(client):
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd", code="ACME")
    await seed_wip(org, owner, acme)
    await set_thresholds(client, owner, org, flat=100_000, percent=1000)

    # £70 write-on: well above 10% of the existing £600 WIP, but the percent rule only
    # measures write-offs, so only the (generous) flat threshold applies here.
    response = await adjust(client, owner, org, acme, kind="write_on", amount=7_000)
    assert response.status_code == 201, response.text
    assert response.json()["data"]["status"] == "approved"


async def test_the_default_percent_threshold_is_ten_percent(client):
    """New practices default to the 10% documented in the client's assumptions."""
    _owner, org = await practice(client)
    percent = await scalar(
        "SELECT wip_write_off_approval_threshold_percent FROM organizations WHERE id = :o",
        o=org["id"],
    )
    assert percent == 1000
