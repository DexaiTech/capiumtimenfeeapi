"""Notification settings: a person's own choices, the practice's switches and defaults, and the
matrix that decides what a notification reaches."""

import pytest

from app.core.notification_types import CATALOGUE
from tests.billing_helpers import count_rows
from tests.clients_helpers import scalar
from tests.domain_factories import practice, staff
from tests.factories import add_member, audit_entries
from tests.notification_helpers import (
    inbox,
    notification_jobs,
    notify,
    own_settings,
    practice_settings,
    put_own,
    put_practice,
)
from tests.time_helpers import err, org_url

TYPES = [t.code for t in CATALOGUE]


def channel(type_: str, in_app: bool, email: bool, sms: bool) -> dict:
    return {"type": type_, "in_app": in_app, "email": email, "sms": sms}


def switch(type_: str, enabled: bool, in_app: bool, email: bool, sms: bool) -> dict:
    return {"type": type_, "enabled": enabled, "in_app": in_app, "email": email, "sms": sms}


def by_type(rows: list[dict]) -> dict[str, dict]:
    return {row["type"]: row for row in rows}


# --- The person's own settings -----------------------------------------------------------------


async def test_a_new_person_starts_with_in_app_and_email_on_and_sms_off(client):
    owner, org = await practice(client)
    data = await own_settings(client, owner, org)
    assert data["sms_number"] is None
    rows = by_type(data["preferences"])
    assert list(rows) == TYPES
    for code, row in rows.items():
        assert (row["in_app"], row["sms"], row["enabled"], row["customised"]) == (
            True,
            False,
            True,
            False,
        ), code
        assert row["label"] and row["description"]
    # Emailing the sender about their own invoice email would only repeat it in-app.
    assert [code for code, row in rows.items() if not row["email"]] == ["invoice_delivery"]
    defaults = by_type((await practice_settings(client, owner, org))["types"])
    assert {code: (r["in_app"], r["email"], r["sms"]) for code, r in defaults.items()} == {
        code: (rows[code]["in_app"], rows[code]["email"], rows[code]["sms"]) for code in TYPES
    }


async def test_own_choices_are_kept_per_type_and_the_rest_follow_the_practice(client):
    owner, org = await practice(client)
    person = await staff(org, "staff")
    await put_practice(
        client, owner, org, {"types": [switch("budget_alert", True, True, False, False)]}
    )
    before = by_type((await own_settings(client, person, org))["preferences"])
    assert (before["budget_alert"]["email"], before["budget_alert"]["customised"]) == (False, False)

    saved = await put_own(
        client, person, org, {"preferences": [channel("approval_pending", False, True, False)]}
    )
    rows = by_type(saved["preferences"])
    assert (rows["approval_pending"]["in_app"], rows["approval_pending"]["email"]) == (False, True)
    assert rows["approval_pending"]["customised"] is True
    assert rows["budget_alert"]["customised"] is False

    # Those who have not chosen follow later changes of the practice; a choice made stays.
    await put_practice(
        client,
        owner,
        org,
        {
            "types": [
                switch("budget_alert", True, True, True, False),
                switch("approval_pending", True, True, True, False),
            ]
        },
    )
    after = by_type((await own_settings(client, person, org))["preferences"])
    assert after["budget_alert"]["email"] is True
    assert (after["approval_pending"]["in_app"], after["approval_pending"]["customised"]) == (
        False,
        True,
    )

    # Saving again replaces the choice of that type only.
    again = await put_own(
        client, person, org, {"preferences": [channel("approval_pending", True, True, False)]}
    )
    assert by_type(again["preferences"])["approval_pending"]["in_app"] is True
    assert await count_rows("SELECT count(*) FROM notification_preferences") == 1
    # Everyone else's settings are their own.
    other = by_type((await own_settings(client, owner, org))["preferences"])
    assert other["approval_pending"]["customised"] is False


async def test_sms_needs_a_mobile_number(client):
    owner, org = await practice(client)
    person = await staff(org, "staff")
    sms_on = {"preferences": [channel("budget_alert", True, True, True)]}
    refused = await put_own(client, person, org, sms_on, status=422)
    assert refused["details"][0]["field"] == "sms_number"
    assert await count_rows("SELECT count(*) FROM notification_preferences") == 0

    saved = await put_own(client, person, org, {**sms_on, "sms_number": "+44 (0)7700 900-123"})
    assert saved["sms_number"] == "+4407700900123"
    assert by_type(saved["preferences"])["budget_alert"]["sms"] is True
    stored = await scalar(
        "SELECT mobile_number FROM organization_members WHERE user_id = :u", u=person.id
    )
    assert stored == "+4407700900123"

    # Omitted: kept. Null or blank: cleared (and SMS cannot be ticked again without one).
    assert (await put_own(client, person, org, {"preferences": []}))[
        "sms_number"
    ] == "+4407700900123"
    assert (await put_own(client, person, org, {"sms_number": "  "}))["sms_number"] is None
    assert (await put_own(client, person, org, {"sms_number": "07700 900123"}))[
        "sms_number"
    ] == "07700900123"
    assert (await put_own(client, person, org, {"sms_number": None}))["sms_number"] is None
    again = await put_own(client, person, org, sms_on, status=422)
    assert again["details"][0]["field"] == "sms_number"
    for bad in ("abc", "12345", "+1234567890123456789", "0770-abc-123"):
        refused = await put_own(client, person, org, {"sms_number": bad}, status=422)
        assert refused["details"][0]["field"] == "sms_number", bad
    # The number belongs to the person in that practice only.
    assert (await own_settings(client, owner, org))["sms_number"] is None


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"preferences": [channel("no_such_type", True, True, False)]}, "preferences.0.type"),
        (
            {"preferences": [channel("budget_alert", True, True, False)] * 2},
            "preferences",
        ),
        (
            {"preferences": [{"type": "budget_alert", "in_app": True, "email": True}]},
            "preferences.0.sms",
        ),
        (
            {"preferences": [{**channel("budget_alert", True, True, False), "push": True}]},
            "preferences.0.push",
        ),
        (
            {"preferences": [{**channel("budget_alert", True, True, False), "in_app": "maybe"}]},
            "preferences.0.in_app",
        ),
        ({"preferences": "budget_alert"}, "preferences"),
        ({"unknown": 1}, "unknown"),
    ],
)
async def test_own_settings_are_validated(client, body, field):
    owner, org = await practice(client)
    refused = await put_own(client, owner, org, body, status=422)
    assert field in [d["field"] for d in refused["details"]]
    assert await count_rows("SELECT count(*) FROM notification_preferences") == 0


# --- The practice's settings -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        ("owner", True),
        ("admin", True),
        ("partner", True),
        ("manager", False),
        ("staff", False),
        ("billing", False),
        ("viewer", False),
    ],
)
async def test_the_practice_settings_need_organization_update_or_settings_manage(
    client, role, allowed
):
    owner, org = await practice(client)
    actor = owner if role == "owner" else await staff(org, role)
    url = org_url(org, "/notification-settings/practice")
    body = {"types": [switch("budget_alert", True, True, True, False)]}
    got = await client.get(url, headers=actor.headers)
    put = await client.put(url, headers=actor.headers, json=body)
    assert (got.status_code, put.status_code) == ((200, 200) if allowed else (403, 403))
    if not allowed:
        assert err(got, 403)["code"] == "PERMISSION_DENIED"
    # Every member can read and change their own, whatever their role.
    assert (
        await client.get(org_url(org, "/notification-settings"), headers=actor.headers)
    ).status_code == 200


async def test_either_permission_is_enough_and_unrelated_ones_are_not(client):
    owner, org = await practice(client)
    url = org_url(org, "/notification-settings/practice")

    async def custom(name: str, permissions: list[str]):
        made = await client.post(
            org_url(org, "/roles"),
            headers=owner.headers,
            json={"name": name, "permissions": permissions},
        )
        assert made.status_code == 201, made.text
        return await add_member(org["id"], name)

    only_settings = await custom("Config", ["organization.read", "settings.manage"])
    only_org = await custom("Practice editor", ["organization.read", "organization.update"])
    neither = await custom("Rates", ["organization.read", "rate.manage"])
    for actor in (only_settings, only_org):
        assert (await client.get(url, headers=actor.headers)).status_code == 200
    assert (await client.get(url, headers=neither.headers)).status_code == 403
    outsider, _ = await practice(client, "Other Practice")
    assert (await client.get(url, headers=outsider.headers)).status_code == 404
    assert (await client.get(url)).status_code == 401


async def test_the_practice_switches_roundtrip_and_are_audited(client):
    owner, org = await practice(client)
    first = await practice_settings(client, owner, org)
    assert [row["type"] for row in first["types"]] == TYPES
    assert all(row["enabled"] for row in first["types"])

    saved = await put_practice(
        client,
        owner,
        org,
        {
            "types": [
                switch("budget_alert", False, True, True, False),
                switch("payment_reminder", True, False, True, True),
            ]
        },
    )
    rows = by_type(saved["types"])
    assert rows["budget_alert"]["enabled"] is False
    assert (rows["payment_reminder"]["in_app"], rows["payment_reminder"]["sms"]) == (False, True)
    assert rows["approval_pending"]["enabled"] is True  # not listed: untouched
    assert await practice_settings(client, owner, org) == saved

    [entry] = await audit_entries(org["id"], "notification_settings.updated")
    assert entry["actor_user_id"] == str(owner.id)
    assert entry["metadata"]["changes"] == {
        "budget_alert": {"enabled": [True, False]},
        "payment_reminder": {"in_app": [True, False], "sms": [False, True]},
    }

    # Saving what is already set writes nothing; a real change is audited again.
    await put_practice(
        client, owner, org, {"types": [switch("budget_alert", False, True, True, False)]}
    )
    assert len(await audit_entries(org["id"], "notification_settings.updated")) == 1
    await put_practice(
        client, owner, org, {"types": [switch("budget_alert", True, True, True, False)]}
    )
    assert len(await audit_entries(org["id"], "notification_settings.updated")) == 2
    assert await count_rows("SELECT count(*) FROM notification_settings") == 2


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"types": []}, "types"),
        ({"types": [switch("no_such_type", True, True, True, False)]}, "types.0.type"),
        ({"types": [switch("budget_alert", True, True, True, False)] * 2}, "types"),
        (
            {"types": [{"type": "budget_alert", "enabled": True, "in_app": True, "email": True}]},
            "types.0.sms",
        ),
        (
            {"types": [{**switch("budget_alert", True, True, True, False), "extra": 1}]},
            "types.0.extra",
        ),
        ({}, "types"),
    ],
)
async def test_practice_settings_are_validated(client, body, field):
    owner, org = await practice(client)
    refused = await put_practice(client, owner, org, body, status=422)
    assert field in [d["field"] for d in refused["details"]]
    assert await audit_entries(org["id"], "notification_settings.updated") == []


async def test_each_practice_has_its_own_settings(client):
    owner, org = await practice(client)
    other_owner, other = await practice(client, "Other Practice")
    await put_practice(
        client, owner, org, {"types": [switch("budget_alert", False, True, True, False)]}
    )
    theirs = by_type((await practice_settings(client, other_owner, other))["types"])
    assert theirs["budget_alert"]["enabled"] is True
    # The owner of one practice cannot reach the other's settings.
    url = org_url(other, "/notification-settings/practice")
    assert (await client.get(url, headers=owner.headers)).status_code == 404
    assert (
        await client.put(
            url,
            headers=owner.headers,
            json={"types": [switch("budget_alert", False, True, True, False)]},
        )
    ).status_code == 404
    # A person in both practices has a settings page in each.
    await add_member(other["id"], "staff", user=owner)
    await put_own(
        client, owner, org, {"preferences": [channel("budget_alert", True, False, False)]}
    )
    assert (
        by_type((await own_settings(client, owner, other))["preferences"])["budget_alert"][
            "customised"
        ]
        is False
    )


# --- What reaches the person -------------------------------------------------------------------

SMS_NUMBER = "07700900123"
OFF = {"enabled": False, "in_app": True, "email": True, "sms": False}
EVERYTHING = {"in_app": True, "email": True, "sms": True}

# (practice switch for the type or None, the person's own choice or None, mobile number,
#  expected: row in the inbox, email queued, SMS queued)
MATRIX = {
    "catalogue defaults": (None, None, None, (True, True, False)),
    "practice switched the type off": (OFF, None, None, (False, False, False)),
    "practice off beats a person who wants everything": (
        OFF,
        EVERYTHING,
        SMS_NUMBER,
        (False, False, False),
    ),
    "practice default email off": (
        {"enabled": True, "in_app": True, "email": False, "sms": False},
        None,
        None,
        (True, False, False),
    ),
    "own email on over a practice default off": (
        {"enabled": True, "in_app": True, "email": False, "sms": False},
        {"in_app": True, "email": True, "sms": False},
        None,
        (True, True, False),
    ),
    "own in-app off keeps email": (
        None,
        {"in_app": False, "email": True, "sms": False},
        None,
        (False, True, False),
    ),
    "own email off keeps in-app": (
        None,
        {"in_app": True, "email": False, "sms": False},
        None,
        (True, False, False),
    ),
    "own everything off": (
        None,
        {"in_app": False, "email": False, "sms": False},
        None,
        (False, False, False),
    ),
    "own sms on with a number": (None, EVERYTHING, SMS_NUMBER, (True, True, True)),
    "practice sms default on, person has a number": (
        {"enabled": True, "in_app": True, "email": True, "sms": True},
        None,
        SMS_NUMBER,
        (True, True, True),
    ),
}


@pytest.mark.parametrize("case", list(MATRIX), ids=list(MATRIX))
async def test_the_preference_matrix(client, case):
    practice_switch, own, number, (in_app, email, sms) = MATRIX[case]
    owner, org = await practice(client)
    person = await staff(org, "staff")
    if practice_switch is not None:
        await put_practice(
            client, owner, org, {"types": [{"type": "budget_alert", **practice_switch}]}
        )
    body: dict = {}
    if own is not None:
        body["preferences"] = [{"type": "budget_alert", **own}]
    if number is not None:
        body["sms_number"] = number
    if body:
        await put_own(client, person, org, body)

    result = await notify(org, "budget_alert", [person], title="Alert")
    assert (len(await inbox(client, person, org)) == 1) is in_app, case
    assert (len(await notification_jobs("email")) == 1) is email, case
    assert (len(await notification_jobs("sms")) == 1) is sms, case
    assert (result.created, result.emails_queued, result.sms_queued, result.suppressed) == (
        int(in_app),
        int(email),
        int(sms),
        practice_switch is not None and not practice_switch["enabled"],
    )


async def test_a_switched_off_type_is_off_for_everyone_of_that_practice_only(client):
    owner, org = await practice(client)
    other_owner, other = await practice(client, "Other Practice")
    await put_practice(
        client, owner, org, {"types": [switch("budget_alert", False, True, True, False)]}
    )
    await put_own(client, owner, org, {"preferences": [channel("budget_alert", True, True, False)]})

    await notify(org, "budget_alert", [owner])
    await notify(other, "budget_alert", [other_owner])
    assert await inbox(client, owner, org) == []
    assert len(await inbox(client, other_owner, other)) == 1
    # The type is not one of the practice's switches elsewhere in the catalogue.
    await notify(org, "payment_reminder", [owner])
    assert len(await inbox(client, owner, org)) == 1
