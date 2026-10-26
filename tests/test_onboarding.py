"""Onboarding checklist: the steps and what the data says, the decisions people make by hand,
permissions, isolation, audit and the races between two people marking the same step."""

import asyncio

import pytest
from sqlalchemy.exc import IntegrityError

from tests.clients_helpers import scalar, sql
from tests.domain_factories import make_client, make_job, make_time_entry, practice, staff
from tests.factories import audit_entries, create_user_with_org, error
from tests.foundation_helpers import add_custom_role
from tests.onboarding_helpers import STEP_KEYS, checklist, mark, steps
from tests.period_lock_helpers import lock_period, today
from tests.team_helpers import new_staff, set_grade_rates
from tests.time_helpers import org_url

PREFIX = "/api/v1/organizations/{organization_id}"


async def _address(client, owner, org):
    response = await client.patch(
        org_url(org, ""),
        headers=owner.headers,
        json={"address_line1": "1 High Street", "postcode": "EC1A 1AA", "phone": "020 7946 0000"},
    )
    assert response.status_code == 200, response.text


async def _vat_number(client, owner, org):
    response = await client.patch(
        org_url(org, ""), headers=owner.headers, json={"vat_number": "GB123456789"}
    )
    assert response.status_code == 200, response.text


async def _not_registered(client, owner, org):
    response = await client.patch(
        org_url(org, ""), headers=owner.headers, json={"vat_registered": False}
    )
    assert response.status_code == 200, response.text


async def _second_person(client, owner, org):
    await new_staff(client, owner, org, "Sam Staff")


async def _grade_rate(client, owner, org):
    await set_grade_rates(org, "SNR", charge=12_000)


async def _practice_default_rate(client, owner, org):
    response = await client.patch(
        org_url(org, ""), headers=owner.headers, json={"default_charge_rate": 9_500}
    )
    assert response.status_code == 200, response.text


async def _prefix(client, owner, org):
    await sql(
        "UPDATE document_sequences SET prefix = 'FEE-' WHERE organization_id = :o "
        "AND kind = 'invoice'",
        o=org["id"],
    )


async def _used_number(client, owner, org):
    await sql(
        "UPDATE document_sequences SET next_number = 12 WHERE organization_id = :o "
        "AND kind = 'credit_note'",
        o=org["id"],
    )


async def _footer(client, owner, org):
    await sql(
        "UPDATE invoice_templates SET footer_text = 'Thank you for your business' "
        "WHERE organization_id = :o",
        o=org["id"],
    )


async def _second_template(client, owner, org):
    await sql(
        "INSERT INTO invoice_templates (id, organization_id, name) "
        "VALUES (gen_random_uuid(), :o, 'Detailed')",
        o=org["id"],
    )


async def _a_client(client, owner, org):
    await make_client(org)


async def _a_job(client, owner, org):
    await make_job(org, await make_client(org))


async def _a_time_entry(client, owner, org):
    await make_time_entry(org, owner)


async def _a_lock(client, owner, org):
    response = await lock_period(client, owner, org, "year", today(org).year - 1)
    assert response.status_code == 200, response.text


async def _a_linked_client(client, owner, org):
    await make_client(org, central_client_id="CC-1", central_sync_state="synced")


# step key -> something that makes the practice's data say the step is finished
SETUPS = [
    ("practice_details", _address),
    ("vat_registration", _vat_number),
    ("vat_registration", _not_registered),
    ("staff_added", _second_person),
    ("grades_rates", _grade_rate),
    ("grades_rates", _practice_default_rate),
    ("numbering_configured", _prefix),
    ("numbering_configured", _used_number),
    ("invoice_template", _footer),
    ("invoice_template", _second_template),
    ("clients_added", _a_client),
    ("jobs_created", _a_job),
    ("first_time_entry", _a_time_entry),
    ("period_lock", _a_lock),
    ("integrations", _a_linked_client),
]


# --- The checklist ----------------------------------------------------------------------------


async def test_a_new_practice_starts_with_only_the_seeded_steps_done(client):
    owner, org = await practice(client)
    response = await checklist(client, owner, org)
    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert [s["key"] for s in body["steps"]] == STEP_KEYS
    by_key = steps(response)

    # Only the services and activity types every practice is seeded with are finished.
    assert [k for k, s in by_key.items() if s["state"] == "done"] == ["services_activity_types"]
    assert by_key["services_activity_types"]["source"] == "auto"
    assert (
        by_key["services_activity_types"]["detail"] == "8 active services, 11 active activity types"
    )
    assert by_key["practice_details"]["detail"] == "Missing: address, postcode, email or phone"
    assert by_key["vat_registration"]["detail"] == "VAT registered, VAT number missing"
    assert by_key["staff_added"]["detail"] == "1 active staff member"
    assert by_key["grades_rates"]["detail"] == "0 grades with a default charge-out rate"
    assert by_key["clients_added"]["detail"] == "0 clients"
    assert body["summary"] == {
        "total": 13,
        "done": 1,
        "skipped": 0,
        "todo": 12,
        "percent": 8,
        "complete": False,
    }
    for step in by_key.values():
        assert step["title"] and step["description"]
        assert step["link"].startswith("/")
        assert step["updated_by_name"] is None and step["updated_at"] is None


async def test_a_step_only_a_person_can_finish_is_manual_and_has_no_detection(client):
    owner, org = await practice(client)
    step = steps(await checklist(client, owner, org))["vat_codes_reviewed"]
    assert (step["state"], step["source"], step["detected"], step["detail"]) == (
        "todo",
        "manual",
        None,
        None,
    )
    assert step["link"] == "/settings/vat-codes"


@pytest.mark.parametrize(("key", "setup"), SETUPS, ids=[f"{k}-{s.__name__}" for k, s in SETUPS])
async def test_an_automatic_step_follows_the_data(client, key, setup):
    owner, org = await practice(client)
    before = steps(await checklist(client, owner, org))[key]
    assert (before["state"], before["source"], before["detected"]) == ("todo", "auto", False)

    await setup(client, owner, org)

    after = steps(await checklist(client, owner, org))[key]
    assert (after["state"], after["source"], after["detected"]) == ("done", "auto", True)
    assert after["updated_by_name"] is None and after["updated_at"] is None


async def test_the_details_count_what_was_found(client):
    owner, org = await practice(client)
    await _second_person(client, owner, org)
    await _grade_rate(client, owner, org)
    await _a_client(client, owner, org)
    await _a_linked_client(client, owner, org)
    by_key = steps(await checklist(client, owner, org))
    assert by_key["staff_added"]["detail"] == "2 active staff members"
    assert by_key["grades_rates"]["detail"] == "1 grade with a default charge-out rate"
    assert by_key["clients_added"]["detail"] == "2 clients"
    assert by_key["integrations"]["detail"] == "1 client linked to the central service"


async def test_a_step_that_the_data_no_longer_supports_goes_back_to_todo(client):
    owner, org = await practice(client)
    assert steps(await checklist(client, owner, org))["services_activity_types"]["state"] == "done"
    await sql("UPDATE services SET is_active = false WHERE organization_id = :o", o=org["id"])
    step = steps(await checklist(client, owner, org))["services_activity_types"]
    assert (step["state"], step["detected"]) == ("todo", False)
    assert step["detail"] == "0 active services, 11 active activity types"


async def test_staff_who_left_do_not_count_as_a_team(client):
    owner, org = await practice(client)
    person = await staff(org)
    assert steps(await checklist(client, owner, org))["staff_added"]["state"] == "done"
    await sql(
        "UPDATE organization_members SET status = 'left' WHERE organization_id = :o "
        "AND user_id = :u",
        o=org["id"],
        u=str(person.id),
    )
    assert steps(await checklist(client, owner, org))["staff_added"]["state"] == "todo"


async def test_deleted_clients_and_jobs_do_not_count(client):
    owner, org = await practice(client)
    row = await make_client(org)
    await make_job(org, row)
    await sql("UPDATE jobs SET deleted_at = now() WHERE organization_id = :o", o=org["id"])
    await sql("UPDATE clients SET deleted_at = now() WHERE organization_id = :o", o=org["id"])
    by_key = steps(await checklist(client, owner, org))
    assert by_key["clients_added"]["state"] == "todo"
    assert by_key["jobs_created"]["state"] == "todo"


# --- Marking steps ----------------------------------------------------------------------------


async def test_marking_a_step_done_records_who_and_when(client):
    owner, org = await practice(client)
    response = await mark(client, owner, org, "vat_codes_reviewed", "done")
    assert response.status_code == 200, response.text
    assert response.json()["message"] == "Step updated"
    step = steps(response)["vat_codes_reviewed"]
    assert (step["state"], step["source"]) == ("done", "manual")
    assert step["updated_by_name"] == owner.full_name and step["updated_at"] is not None
    summary = response.json()["data"]["summary"]
    assert (summary["done"], summary["todo"], summary["percent"]) == (2, 11, 15)
    # The next read agrees.
    assert steps(await checklist(client, owner, org))["vat_codes_reviewed"] == step


async def test_a_step_can_be_skipped_and_the_checklist_completes(client):
    owner, org = await practice(client)
    skipped = steps(await mark(client, owner, org, "integrations", "skipped"))["integrations"]
    assert (skipped["state"], skipped["source"], skipped["detected"]) == (
        "skipped",
        "manual",
        False,
    )

    for key in STEP_KEYS:
        current = steps(await checklist(client, owner, org))[key]
        if current["state"] == "todo":
            response = await mark(client, owner, org, key, "done")
    summary = response.json()["data"]["summary"]
    assert summary == {
        "total": 13,
        "done": 12,
        "skipped": 1,
        "todo": 0,
        "percent": 100,
        "complete": True,
    }


async def test_a_decision_wins_over_the_data_and_todo_takes_it_back(client):
    owner, org = await practice(client)
    # The data says nothing yet: a person says done.
    done = steps(await mark(client, owner, org, "clients_added", "done"))["clients_added"]
    assert (done["state"], done["source"], done["detected"]) == ("done", "manual", False)
    # Taking the decision back lets the data speak: todo again.
    back = steps(await mark(client, owner, org, "clients_added", "todo"))["clients_added"]
    assert (back["state"], back["source"], back["updated_by_name"]) == ("todo", "auto", None)

    await _a_client(client, owner, org)
    # The data says done; a person skips it anyway.
    skipped = steps(await mark(client, owner, org, "clients_added", "skipped"))["clients_added"]
    assert (skipped["state"], skipped["source"], skipped["detected"]) == ("skipped", "manual", True)
    # And back to the data.
    again = steps(await mark(client, owner, org, "clients_added", "todo"))["clients_added"]
    assert (again["state"], again["source"]) == ("done", "auto")


async def test_a_manual_step_taken_back_stays_manual(client):
    owner, org = await practice(client)
    await mark(client, owner, org, "vat_codes_reviewed", "done")
    step = steps(await mark(client, owner, org, "vat_codes_reviewed", "todo"))["vat_codes_reviewed"]
    assert (step["state"], step["source"]) == ("todo", "manual")


async def test_marking_is_audited_once_per_change(client):
    owner, org = await practice(client)
    await mark(client, owner, org, "vat_codes_reviewed", "done")
    await mark(client, owner, org, "vat_codes_reviewed", "done")  # nothing changes
    await mark(client, owner, org, "vat_codes_reviewed", "todo")
    entries = await audit_entries(org["id"], "onboarding.step_updated")
    assert [e["metadata"] for e in entries] == [
        {
            "step_key": "vat_codes_reviewed",
            "step_title": "VAT codes",
            "state": "done",
            "changes": {"state": ["todo", "done"]},
        },
        {
            "step_key": "vat_codes_reviewed",
            "step_title": "VAT codes",
            "state": "todo",
            "changes": {"state": ["done", "todo"]},
        },
    ]
    row_id = str(
        await scalar("SELECT id FROM onboarding_progress WHERE organization_id = :o", o=org["id"])
    )
    assert {(e["target_type"], e["target_id"], e["actor_user_id"]) for e in entries} == {
        ("onboarding_step", row_id, str(owner.id))
    }


async def test_one_row_is_kept_per_practice_and_step(client):
    owner, org = await practice(client)
    for state in ("done", "skipped", "todo", "done"):
        await mark(client, owner, org, "period_lock", state)
    await mark(client, owner, org, "jobs_created", "skipped")
    rows = await sql(
        "SELECT step_key, state FROM onboarding_progress WHERE organization_id = :o "
        "ORDER BY step_key",
        o=org["id"],
    )
    assert rows == [
        {"step_key": "jobs_created", "state": "skipped"},
        {"step_key": "period_lock", "state": "done"},
    ]


async def test_two_people_marking_the_same_step_at_once_leave_one_row(client):
    owner, org = await practice(client)
    admin = await staff(org, "admin")
    results = await asyncio.gather(
        mark(client, owner, org, "period_lock", "done"),
        mark(client, admin, org, "period_lock", "skipped"),
        mark(client, owner, org, "period_lock", "done"),
    )
    assert [r.status_code for r in results] == [200, 200, 200]
    rows = await sql(
        "SELECT state FROM onboarding_progress WHERE organization_id = :o", o=org["id"]
    )
    assert len(rows) == 1 and rows[0]["state"] in {"done", "skipped"}


# --- Validation -------------------------------------------------------------------------------


async def test_an_unknown_step_is_404(client):
    owner, org = await practice(client)
    response = await mark(client, owner, org, "make_coffee", "done")
    assert response.status_code == 404
    assert error(response)["code"] == "RESOURCE_NOT_FOUND"
    assert await scalar("SELECT count(*) FROM onboarding_progress") == 0


@pytest.mark.parametrize(
    "body", [{"state": "finished"}, {}, {"state": None}, {"state": "done", "x": 1}]
)
async def test_a_bad_body_is_422(client, body):
    owner, org = await practice(client)
    response = await client.put(
        org_url(org, "/onboarding/steps/period_lock"), headers=owner.headers, json=body
    )
    assert response.status_code == 422, response.text
    assert error(response)["code"] == "VALIDATION_ERROR"
    assert await scalar("SELECT count(*) FROM onboarding_progress") == 0


# --- Permissions and isolation ----------------------------------------------------------------


@pytest.mark.parametrize("role", ["owner", "admin", "partner"])
async def test_who_manages_the_checklist(client, role):
    _owner, org = await practice(client)
    person = await staff(org, role)
    assert (await mark(client, person, org, "period_lock", "skipped")).status_code == 200


@pytest.mark.parametrize("role", ["manager", "staff", "billing", "viewer"])
async def test_everybody_else_reads_but_cannot_mark(client, role):
    _owner, org = await practice(client)
    person = await staff(org, role)
    assert (await checklist(client, person, org)).status_code == 200
    response = await mark(client, person, org, "period_lock", "skipped")
    assert response.status_code == 403
    assert error(response)["code"] == "PERMISSION_DENIED"
    assert await scalar("SELECT count(*) FROM onboarding_progress") == 0


async def test_a_role_without_organization_read_sees_nothing(client):
    _owner, org = await practice(client)
    await add_custom_role(org["id"], "Nothing")
    person = await staff(org, "Nothing")
    response = await checklist(client, person, org)
    assert response.status_code == 403
    assert error(response)["code"] == "PERMISSION_DENIED"
    # The permission is checked before the step is looked up.
    assert (await mark(client, person, org, "make_coffee", "done")).status_code == 403


async def test_a_custom_role_can_hold_only_the_manage_permission(client):
    _owner, org = await practice(client)
    await add_custom_role(org["id"], "Setup", ["organization.read", "onboarding.manage"])
    person = await staff(org, "Setup")
    assert (await mark(client, person, org, "period_lock", "done")).status_code == 200


async def test_another_practices_checklist_is_404_and_progress_is_per_practice(client):
    owner, org = await practice(client)
    other_owner, other = await create_user_with_org(client, "Other Practice")
    await mark(client, owner, org, "vat_codes_reviewed", "done")

    assert (
        steps(await checklist(client, other_owner, other))["vat_codes_reviewed"]["state"] == "todo"
    )
    for response in (
        await checklist(client, other_owner, org),
        await mark(client, other_owner, org, "vat_codes_reviewed", "todo"),
    ):
        assert response.status_code == 404
        assert error(response)["code"] == "RESOURCE_NOT_FOUND"
    assert steps(await checklist(client, owner, org))["vat_codes_reviewed"]["state"] == "done"


async def test_the_data_counted_belongs_to_the_practice(client):
    owner, org = await practice(client)
    other_owner, other = await create_user_with_org(client, "Other Practice")
    await _a_job(client, other_owner, other)
    await _a_time_entry(client, other_owner, other)
    by_key = steps(await checklist(client, owner, org))
    assert by_key["jobs_created"]["state"] == "todo"
    assert by_key["first_time_entry"]["state"] == "todo"
    assert by_key["clients_added"]["state"] == "todo"


async def test_sign_in_is_required(client):
    _owner, org = await practice(client)
    assert (await client.get(org_url(org, "/onboarding"))).status_code == 401
    response = await client.put(
        org_url(org, "/onboarding/steps/period_lock"), json={"state": "done"}
    )
    assert response.status_code == 401


# --- The table --------------------------------------------------------------------------------


async def test_the_table_refuses_a_bad_state_a_bad_key_and_a_second_row(client):
    _owner, org = await practice(client)
    insert = (
        "INSERT INTO onboarding_progress (id, organization_id, step_key, state) "
        "VALUES (gen_random_uuid(), :o, :k, :s)"
    )
    await sql(insert, o=org["id"], k="period_lock", s="done")
    for key, state in (("period_lock", "skipped"), ("other", "finished"), ("Bad-Key", "done")):
        with pytest.raises(IntegrityError):
            await sql(insert, o=org["id"], k=key, s=state)


async def test_progress_goes_with_the_practice(client):
    owner, org = await practice(client)
    await mark(client, owner, org, "period_lock", "done")
    assert await scalar("SELECT count(*) FROM onboarding_progress") == 1
    await sql("DELETE FROM organizations WHERE id = :o", o=org["id"])
    assert await scalar("SELECT count(*) FROM onboarding_progress") == 0


# --- The schema -------------------------------------------------------------------------------


async def test_the_routes_are_documented(client):
    spec = (await client.get("/openapi.json")).json()
    paths = spec["paths"]
    get = paths[f"{PREFIX}/onboarding"]["get"]
    put = paths[f"{PREFIX}/onboarding/steps/{{step_key}}"]["put"]
    for operation, words in ((get, ["organization.read"]), (put, ["onboarding.manage", "404"])):
        assert operation["summary"]
        for word in words:
            assert word in operation["description"]
        assert {"401", "403", "404", "422"} <= set(operation["responses"])
    schemas = spec["components"]["schemas"]
    for name in (
        "OnboardingRead",
        "OnboardingStepRead",
        "OnboardingSummary",
        "OnboardingStepUpdate",
    ):
        assert name in schemas
    for field, prop in schemas["OnboardingStepRead"]["properties"].items():
        assert prop.get("description") or any(
            o.get("description") for o in prop.get("anyOf", [])
        ), field
