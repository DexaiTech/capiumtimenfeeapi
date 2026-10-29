"""Email templates: the catalogue, a practice's own versions, validation (a template can never run
code), preview, reset, audit, tenant isolation and what the worker actually sends."""

from email.message import Message

import pytest

from app.core.notification_types import CATALOGUE
from app.services import template_engine
from app.services.email_templates import default_content, validate_content
from app.services.template_catalogue import MERGE_FIELDS, TEMPLATES, sample_values
from app.services.template_render import TEMPLATE_DIR, render_email
from tests.billing_helpers import count_rows, draft, issued, new_practice
from tests.conftest import Mailbox, run_jobs
from tests.domain_factories import make_client, make_time_entry, practice, staff
from tests.factories import audit_entries
from tests.notification_helpers import notify
from tests.team_helpers import new_staff
from tests.time_helpers import err, ok_data, org_url

KEYS = [t.code for t in CATALOGUE] + [
    "invoice",
    "staff_invitation",
    "account_created",
    "client_payment_reminder",
    "client_payment_upcoming",
    "client_payment_final_notice",
    "client_statement",
]
GOOD = {
    "subject": "Alert: {{ job_name }} at {{ budget_percent }}%",
    "html_body": "<p>Hi {{ recipient_name }}, {{ job_name }} used {{ budget_percent }}%.</p>",
    "text_body": "Hi {{ recipient_name }}, {{ job_name }} used {{ budget_percent }}%.",
}


def url(org: dict, key: str = "", suffix: str = "") -> str:
    return org_url(org, "/email-templates" + (f"/{key}" if key else "") + suffix)


async def put(client, actor, org, key: str = "budget_alert", status: int = 200, **changes) -> dict:
    response = await client.put(url(org, key), headers=actor.headers, json={**GOOD, **changes})
    assert response.status_code == status, response.text
    return response.json()["data"] if status == 200 else response.json()["error"]


async def preview(client, actor, org, key: str = "budget_alert", body=None, status: int = 200):
    response = await client.post(url(org, key, "/preview"), headers=actor.headers, json=body)
    assert response.status_code == status, response.text
    return response.json()["data"] if status == 200 else response.json()["error"]


def html_of(message: Message) -> str:
    for part in message.walk():
        if part.get_content_type() == "text/html":
            return part.get_payload(decode=True).decode()
    raise AssertionError("no HTML part")


# --- The catalogue -----------------------------------------------------------------------------


async def test_the_list_has_every_template_with_its_merge_fields(client):
    owner, org = await practice(client)
    data = ok_data(await client.get(url(org), headers=owner.headers))
    assert [t["key"] for t in data] == KEYS
    for template in data:
        assert template["customised"] is False and template["updated_at"] is None
        assert template["updated_by"] is None and template["updated_by_name"] is None
        assert template["name"] and template["description"]
        assert template["subject"] and template["html_body"] and template["text_body"]
        for field in template["merge_fields"]:
            assert set(field) == {"name", "label", "description", "sample"}
    cat = {t["key"]: t for t in data}
    assert (cat["budget_alert"]["category"], cat["budget_alert"]["notification_type"]) == (
        "notification",
        "budget_alert",
    )
    assert (cat["invoice"]["category"], cat["invoice"]["notification_type"]) == ("system", None)
    assert [f["name"] for f in cat["budget_alert"]["merge_fields"]] == [
        "recipient_name",
        "practice_name",
        "app_name",
        "client_name",
        "job_name",
        "budget_percent",
        "link",
    ]
    assert cat["staff_invitation"]["required_fields"] == ["link"]
    assert cat["budget_alert"]["required_fields"] == []
    one = ok_data(await client.get(url(org, "budget_alert"), headers=owner.headers))
    assert one == cat["budget_alert"]


EXTERNAL_KEYS = [
    "invoice",
    "client_payment_reminder",
    "client_payment_upcoming",
    "client_payment_final_notice",
    "client_statement",
]


async def test_templates_are_split_into_client_and_team_emails(client):
    owner, org = await practice(client)
    everything = ok_data(await client.get(url(org), headers=owner.headers))
    assert {t["key"]: t["audience"] for t in everything} == {
        key: "external" if key in EXTERNAL_KEYS else "internal" for key in KEYS
    }
    external = ok_data(
        await client.get(url(org), params={"audience": "external"}, headers=owner.headers)
    )
    internal = ok_data(
        await client.get(url(org), params={"audience": "internal"}, headers=owner.headers)
    )
    assert [t["key"] for t in external] == EXTERNAL_KEYS
    assert [t["key"] for t in internal] == [k for k in KEYS if k not in EXTERNAL_KEYS]
    one = ok_data(await client.get(url(org, "budget_alert"), headers=owner.headers))
    assert one["audience"] == "internal"
    bad = await client.get(url(org), params={"audience": "public"}, headers=owner.headers)
    assert bad.status_code == 422


def test_the_pickers_of_client_emails_only_offer_external_templates():
    from typing import get_args

    from app.schemas.debtors import ManualReminderTemplate, ReminderTemplate
    from app.services.statement_sends import TEMPLATE as STATEMENT_TEMPLATE

    for key in (*get_args(ManualReminderTemplate), *get_args(ReminderTemplate), STATEMENT_TEMPLATE):
        assert TEMPLATES[key].audience == "external", key


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
async def test_reading_and_writing_need_organization_update_or_settings_manage(
    client, role, allowed
):
    owner, org = await practice(client)
    actor = owner if role == "owner" else await staff(org, role)
    h = actor.headers
    answers = [
        await client.get(url(org), headers=h),
        await client.get(url(org, "budget_alert"), headers=h),
        await client.put(url(org, "budget_alert"), headers=h, json=GOOD),
        await client.post(url(org, "budget_alert", "/preview"), headers=h, json={}),
        await client.delete(url(org, "budget_alert"), headers=h),
    ]
    assert [r.status_code for r in answers] == ([200] * 5 if allowed else [403] * 5)
    if not allowed:
        # The permission is checked before the key: no hint whether a template exists.
        assert (await client.get(url(org, "no_such_template"), headers=h)).status_code == 403
        assert await count_rows("SELECT count(*) FROM email_templates") == 0


async def test_an_unknown_key_is_404(client):
    owner, org = await practice(client)
    for response in (
        await client.get(url(org, "verification"), headers=owner.headers),
        await client.put(url(org, "no_such"), headers=owner.headers, json=GOOD),
        await client.delete(url(org, "no_such"), headers=owner.headers),
        await client.post(url(org, "password_reset", "/preview"), headers=owner.headers, json={}),
    ):
        assert err(response, 404)["code"] == "RESOURCE_NOT_FOUND"


# --- Customising and resetting -----------------------------------------------------------------


async def test_customise_then_reset_with_audit(client):
    owner, org = await practice(client)
    admin = await staff(org, "admin", full_name="Ada Admin")
    default = ok_data(await client.get(url(org, "budget_alert"), headers=owner.headers))

    saved = await put(client, admin, org)
    assert saved["customised"] is True and saved["subject"] == GOOD["subject"]
    assert saved["html_body"] == GOOD["html_body"] and saved["text_body"] == GOOD["text_body"]
    assert (saved["updated_by"], saved["updated_by_name"]) == (str(admin.id), "Ada Admin")
    assert saved["updated_at"] is not None
    listed = {t["key"]: t for t in ok_data(await client.get(url(org), headers=owner.headers))}
    assert listed["budget_alert"]["customised"] is True
    assert [k for k, t in listed.items() if t["customised"]] == ["budget_alert"]

    [created] = await audit_entries(org["id"], "email_template.updated")
    assert created["actor_user_id"] == str(admin.id) and created["target_type"] == "email_template"
    assert (
        created["metadata"]["created"] is True
        and created["metadata"]["template_key"] == "budget_alert"
    )
    assert created["metadata"]["changed_fields"] == ["subject", "html_body", "text_body"]
    assert created["metadata"]["changes"] == {"subject": [default["subject"], GOOD["subject"]]}

    # The same content again changes and audits nothing; a real change is an update.
    await put(client, admin, org)
    assert len(await audit_entries(org["id"], "email_template.updated")) == 1
    await put(client, owner, org, text_body="Plain {{ job_name }}")
    updated = (await audit_entries(org["id"], "email_template.updated"))[1]
    assert updated["metadata"]["created"] is False and updated["metadata"]["changed_fields"] == [
        "text_body"
    ]
    assert "changes" not in updated["metadata"]
    assert await count_rows("SELECT count(*) FROM email_templates") == 1

    reset = ok_data(await client.delete(url(org, "budget_alert"), headers=owner.headers))
    assert reset == default
    [entry] = await audit_entries(org["id"], "email_template.reset")
    assert entry["metadata"] == {"template_key": "budget_alert", "template_name": "Budget alert"}
    assert await count_rows("SELECT count(*) FROM email_templates") == 0
    # Resetting what is not customised is fine and writes nothing.
    again = await client.delete(url(org, "budget_alert"), headers=owner.headers)
    assert (
        ok_data(again) == default
        and len(await audit_entries(org["id"], "email_template.reset")) == 1
    )


async def test_each_practice_has_its_own_versions(client):
    owner, org = await practice(client)
    other_owner, other = await practice(client, "Other Practice")
    await put(client, owner, org)
    theirs = {
        t["key"]: t for t in ok_data(await client.get(url(other), headers=other_owner.headers))
    }
    assert theirs["budget_alert"]["customised"] is False
    assert theirs["budget_alert"]["subject"] != GOOD["subject"]
    # Another practice's owner is not a member here: 404, not 403.
    for response in (
        await client.get(url(org), headers=other_owner.headers),
        await client.put(url(org, "budget_alert"), headers=other_owner.headers, json=GOOD),
        await client.delete(url(org, "budget_alert"), headers=other_owner.headers),
        await client.post(
            url(org, "budget_alert", "/preview"), headers=other_owner.headers, json={}
        ),
    ):
        assert response.status_code == 404
    assert (await client.get(url(org))).status_code == 401
    # Resetting in one practice leaves the other's alone.
    await put(client, other_owner, other)
    await client.delete(url(org, "budget_alert"), headers=owner.headers)
    assert ok_data(await client.get(url(other, "budget_alert"), headers=other_owner.headers))[
        "customised"
    ]


# --- Validation: a template is plain text with merge fields, nothing more ---------------------

REFUSED = [
    # Fields the template does not have (including names that would be secrets).
    ("html_body", "<p>{{ nope }}</p>", "unknown_merge_field"),
    ("subject", "Hello {{ jwt_secret_key }}", "unknown_merge_field"),
    ("text_body", "{{ week_start }}", "unknown_merge_field"),
    ("html_body", "{{ config }}", "unknown_merge_field"),
    ("html_body", "{{ self }}", "unknown_merge_field"),
    # Anything but a plain field: attribute access, calls, filters, arithmetic.
    ("html_body", "{{ ''.__class__ }}", "invalid_placeholder"),
    ("subject", "{{ ''.__class__ }}", "invalid_placeholder"),
    ("text_body", "{{ ''.__class__.__mro__[1].__subclasses__() }}", "invalid_placeholder"),
    ("html_body", "{{ job_name.__class__ }}", "invalid_placeholder"),
    ("html_body", "{{ lipsum.__globals__ }}", "invalid_placeholder"),
    ("html_body", "{{ cycler.__init__.__globals__.os.popen('id').read() }}", "invalid_placeholder"),
    ("html_body", "{{ job_name() }}", "invalid_placeholder"),
    ("html_body", "{{ job_name|upper }}", "invalid_placeholder"),
    ("html_body", "{{ 7*7 }}", "invalid_placeholder"),
    ("html_body", "{{ job_name['x'] }}", "invalid_placeholder"),
    ("html_body", "{{ {{ job_name }} }}", "invalid_placeholder"),
    ("html_body", "{{ JOB_NAME }}", "invalid_placeholder"),
    ("html_body", "{{ }}", "invalid_placeholder"),
    # Statements and comments.
    ("html_body", "{% if job_name %}x{% endif %}", "template_logic"),
    ("text_body", "{% for i in range(9) %}x{% endfor %}", "template_logic"),
    ("subject", "{% set x = 1 %}{{ job_name }}", "template_logic"),
    ("html_body", "{# comment #}", "template_logic"),
    ("html_body", "{%- import 'os' as os -%}", "template_logic"),
    # Stray braces.
    ("html_body", "Hello {{ job_name", "unbalanced_placeholder"),
    ("text_body", "job_name }}", "unbalanced_placeholder"),
    # Markup that runs script or leaves the message.
    ("html_body", "<script>alert(1)</script>", "unsafe_html"),
    ("html_body", '<a href="javascript:alert(1)">x</a>', "unsafe_html"),
    ("html_body", "<img src=x onerror=alert(1)>", "unsafe_html"),
    ("html_body", "<iframe src=x></iframe>", "unsafe_html"),
    ("html_body", "<form action=x><input></form>", "unsafe_html"),
    ("html_body", "<META http-equiv=refresh content=0>", "unsafe_html"),
    ("subject", "Line one\nLine two", "multiline_subject"),
]


@pytest.mark.parametrize(
    ("field", "value", "code"), REFUSED, ids=[f"{r[0]}:{r[1][:28]}" for r in REFUSED]
)
async def test_a_template_with_logic_or_unknown_fields_is_refused(client, field, value, code):
    owner, org = await practice(client)
    refused = await put(client, owner, org, status=422, **{field: value})
    assert refused["code"] == "VALIDATION_ERROR"
    assert code in [d["code"] for d in refused["details"] if d["field"] == field]
    assert all({"field", "code", "message"} <= set(d) for d in refused["details"])
    # Nothing was stored or audited, and the same draft cannot be previewed either.
    assert await count_rows("SELECT count(*) FROM email_templates") == 0
    assert await audit_entries(org["id"], "email_template.updated") == []
    shown = await preview(client, owner, org, body={field: value}, status=422)
    assert code in [d["code"] for d in shown["details"] if d["field"] == field]


async def test_every_problem_is_listed_at_once(client):
    owner, org = await practice(client)
    refused = await put(
        client,
        owner,
        org,
        status=422,
        subject="{{ nope }}",
        html_body="<script></script>{{ ''.__class__ }}",
        text_body="{% if x %}{% endif %}",
    )
    found = {(d["field"], d["code"]) for d in refused["details"]}
    assert found == {
        ("subject", "unknown_merge_field"),
        ("html_body", "invalid_placeholder"),
        ("html_body", "unsafe_html"),
        ("text_body", "template_logic"),
    }


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"subject": ""}, "subject"),
        ({"subject": "   "}, "subject"),
        ({"html_body": ""}, "html_body"),
        ({"text_body": " \n "}, "text_body"),
        ({"subject": "x" * 201}, "subject"),
        ({"html_body": "x" * 100_001}, "html_body"),
        ({"text_body": "x" * 50_001}, "text_body"),
        ({"subject": None}, "subject"),
        ({"extra": "x"}, "extra"),
    ],
)
async def test_empty_missing_and_oversized_parts_are_refused(client, changes, field):
    owner, org = await practice(client)
    refused = await put(client, owner, org, status=422, **changes)
    assert field in [d["field"] for d in refused["details"]]
    assert await count_rows("SELECT count(*) FROM email_templates") == 0
    for missing in ("subject", "html_body", "text_body"):
        body = {k: v for k, v in GOOD.items() if k != missing}
        response = await client.put(url(org, "budget_alert"), headers=owner.headers, json=body)
        assert err(response, 422)["details"][0]["field"] == missing


async def test_an_invitation_must_keep_its_link(client):
    owner, org = await practice(client)
    body = {
        "subject": "Join {{ practice_name }}",
        "html_body": "<p>Hi {{ recipient_name }}</p>",
        "text_body": "Hi {{ recipient_name }}",
    }
    for key in ("staff_invitation", "account_created"):
        refused = await client.put(url(org, key), headers=owner.headers, json=body)
        found = {(d["field"], d["code"]) for d in err(refused, 422)["details"]}
        assert found == {("html_body", "missing_merge_field"), ("text_body", "missing_merge_field")}
        ok = {
            **body,
            "html_body": body["html_body"] + "{{ link }}",
            "text_body": body["text_body"] + "{{ link }}",
        }
        assert (await client.put(url(org, key), headers=owner.headers, json=ok)).status_code == 200


# --- Preview -----------------------------------------------------------------------------------


async def test_preview_renders_the_saved_template_with_sample_data(client):
    owner, org = await practice(client, "Acme Accountants")
    shown = await preview(client, owner, org)  # no body at all
    assert shown["subject"] == "Budget alert: Year-end accounts 2026 has used 80% of its budget"
    assert shown["html"].startswith("<!doctype html>") and "Acme Accountants" in shown["html"]
    assert "Year-end accounts 2026" in shown["html"] and "{{" not in shown["html"]
    assert owner.full_name in shown["text"] and "Widgets Ltd" in shown["text"]
    assert shown["values"]["link"] == f"http://localhost:5173/{org['slug']}"
    assert shown["values"]["practice_name"] == "Acme Accountants"
    assert shown["values"]["recipient_name"] == owner.full_name
    assert shown["values"]["budget_percent"] == "80"
    assert await preview(client, owner, org, body={}) == shown
    # Previewing saves nothing and sends nothing.
    assert await count_rows("SELECT count(*) FROM email_templates") == 0
    assert await count_rows("SELECT count(*) FROM message_log") == 0

    await put(client, owner, org)
    saved = await preview(client, owner, org, body={})
    assert saved["subject"] == "Alert: Year-end accounts 2026 at 80%"


async def test_preview_renders_a_draft_with_given_values_and_escapes_them(client):
    owner, org = await practice(client)
    draft_body = {
        "subject": "Hi {{ recipient_name }}: {{ job_name }}",
        "html_body": "<p>{{ client_name }} {{ budget_percent }}%</p>",
        "text_body": "T {{ job_name }}",
        "values": {"job_name": "Q3 <Tax> & VAT", "client_name": "A&B <b>"},
    }
    shown = await preview(client, owner, org, body=draft_body)
    assert shown["subject"] == f"Hi {owner.full_name}: Q3 <Tax> & VAT"
    assert "A&amp;B &lt;b&gt; 80%" in shown["html"] and "<b>" not in shown["html"]
    assert shown["text"] == "T Q3 <Tax> & VAT"
    assert (
        shown["values"]["job_name"] == "Q3 <Tax> & VAT"
        and shown["values"]["budget_percent"] == "80"
    )
    # A value is text, never more template; a subject never gains a line break.
    sneaky = await preview(
        client,
        owner,
        org,
        body={
            "values": {
                "job_name": "{{ link }} and x\r\nBcc: evil@example.com",
                "client_name": "{{ recipient_name }}",
            }
        },
    )
    assert "\n" not in sneaky["subject"] and "\r" not in sneaky["subject"]
    assert "{{ link }} and x Bcc: evil@example.com" in sneaky["subject"]
    assert "{{ recipient_name }}" in sneaky["text"]
    assert await count_rows("SELECT count(*) FROM email_templates") == 0


async def test_preview_refuses_unknown_and_malformed_values(client):
    owner, org = await practice(client)
    unknown = await preview(client, owner, org, body={"values": {"week_start": "x"}}, status=422)
    assert unknown["details"][0] == {
        "field": "values.week_start",
        "code": "unknown_merge_field",
        "message": "week_start is not a merge field of this template.",
    }
    for values in ({"Bad Name": "x"}, {"job_name": "x" * 501}):
        refused = await preview(client, owner, org, body={"values": values}, status=422)
        assert refused["details"][0]["field"] == "values"
    assert (await preview(client, owner, org, body={"extra": 1}, status=422))["details"]


# --- What the worker sends ---------------------------------------------------------------------


async def test_a_customised_notification_template_is_what_gets_emailed(client, worker, mailbox):
    owner, org = await practice(client, "Acme Accountants")
    await put(
        client,
        owner,
        org,
        subject="Heads up: {{ job_name }} at {{ budget_percent }}%",
        html_body="<p>Dear {{ recipient_name }}, <b>{{ job_name }}</b> hit {{ budget_percent }}%.</p>",
        text_body="Dear {{ recipient_name }}, {{ job_name }} hit {{ budget_percent }}%. {{ link }}",
    )
    await notify(
        org,
        "budget_alert",
        [owner],
        link="/jobs/1",
        merge={"job_name": "<i>Q3</i> & more", "budget_percent": 90},
    )
    await run_jobs(worker)
    [message] = mailbox.to(owner.email)
    assert message["Subject"] == "Heads up: <i>Q3</i> & more at 90%"
    html = html_of(message)
    assert "&lt;i&gt;Q3&lt;/i&gt; &amp; more" in html and "<i>Q3</i>" not in html
    assert "Dear " + owner.full_name in html and "Acme Accountants" in html
    assert Mailbox.text(message).strip() == (
        f"Dear {owner.full_name}, <i>Q3</i> & more hit 90%. "
        f"http://localhost:5173/{org['slug']}/jobs/1"
    )

    # Reset: the built-in text is back for the next one.
    await client.delete(url(org, "budget_alert"), headers=owner.headers)
    mailbox.clear()
    await notify(org, "budget_alert", [owner], merge={"job_name": "Q4", "budget_percent": 100})
    await run_jobs(worker)
    [again] = mailbox.to(owner.email)
    assert again["Subject"] == "Budget alert: Q4 has used 100% of its budget"


async def test_a_customised_invoice_email_keeps_its_pdf(client, worker, mailbox):
    p = await new_practice(client)
    emailed = await make_client(p.org, "Emailed Ltd", email="bills@emailed.example")
    entry = await make_time_entry(p.org, p.owner, client=emailed)
    invoice = await draft(client, p.owner, p.org, emailed.id, time=[entry])
    await issued(client, p.owner, p.org, invoice["id"])
    await put(
        client,
        p.owner,
        p.org,
        "invoice",
        subject="Your bill {{ invoice_number }}",
        html_body="<p>Owing: {{ amount_due }} by {{ due_date }}</p>",
        text_body="Owing: {{ amount_due }} by {{ due_date }} to {{ practice_name }}",
    )
    send = org_url(p.org, f"/invoices/{invoice['id']}/send")
    assert (await client.post(send, headers=p.owner.headers)).status_code == 202
    await run_jobs(worker)
    [message] = mailbox.to("bills@emailed.example")
    assert message["Subject"] == "Your bill INV-00001"
    assert "Owing: £144.00 by " in Mailbox.text(message) and "Acme Accountants" in Mailbox.text(
        message
    )
    [pdf] = [part for part in message.walk() if part.get_content_type() == "application/pdf"]
    assert pdf.get_payload(decode=True).startswith(b"%PDF")

    await client.delete(url(p.org, "invoice"), headers=p.owner.headers)
    mailbox.clear()
    assert (await client.post(send, headers=p.owner.headers)).status_code == 202
    await run_jobs(worker)
    [built_in] = mailbox.to("bills@emailed.example")
    assert built_in["Subject"] == "Invoice INV-00001 from Acme Accountants"


async def test_a_customised_invitation_still_carries_the_one_time_link(client, worker, mailbox):
    owner, org = await practice(client, "Acme Accountants")
    person = await new_staff(client, owner, org, "Jane Doe", email="jane@example.com")
    await put(
        client,
        owner,
        org,
        "staff_invitation",
        subject="Welcome to {{ practice_name }}, {{ recipient_name }}",
        html_body='<p>{{ invited_by }} says hi.</p><p><a href="{{ link }}">Join</a></p>',
        text_body="{{ invited_by }} says hi. Join: {{ link }} (sign in at {{ login_url }})",
    )
    mailbox.clear()
    response = await client.post(
        org_url(org, f"/staff/{person['id']}/invite"), headers=owner.headers, json={}
    )
    assert response.status_code == 200, response.text
    await run_jobs(worker)
    [message] = mailbox.to("jane@example.com")
    assert message["Subject"] == "Welcome to Acme Accountants, Jane Doe"
    assert mailbox.token_for("jane@example.com")
    assert "says hi" in Mailbox.text(message)
    assert "http://localhost:5173/login" in Mailbox.text(message)


# --- The catalogue itself ----------------------------------------------------------------------


def test_every_default_passes_its_own_validation_and_renders_completely():
    for spec in TEMPLATES.values():
        content = default_content(spec)
        validate_content(spec, content)
        used = set()
        for source in (content.subject, content.html, content.text):
            used |= template_engine.fields_used(source)
        assert used <= set(spec.fields), spec.key
        assert set(spec.required) <= used, spec.key
        assert all(name in MERGE_FIELDS for name in spec.fields), spec.key
        rendered = render_email(content, sample_values(spec), brand="Acme", app_name="Capium")
        for text in (rendered.subject, rendered.html, rendered.text):
            assert "{{" not in text and "}}" not in text, spec.key
        assert "\n" not in rendered.subject


def test_every_notification_type_has_a_template_and_the_system_emails_keep_their_files():
    for entry in CATALOGUE:
        spec = TEMPLATES[entry.code]
        assert spec.category == "notification"
        assert {"recipient_name", "practice_name", "app_name"} <= set(spec.fields)
    for key in ("invoice", "staff_invitation", "account_created"):
        assert TEMPLATES[key].category == "system"
        assert (TEMPLATE_DIR / f"{key}.html").exists() and (TEMPLATE_DIR / f"{key}.txt").exists()
    # The central login's account emails belong to no practice and are not customisable.
    for key in ("verification", "password_reset", "welcome", "password_changed", "account_exists"):
        assert key not in TEMPLATES


def test_the_engine_substitutes_plain_fields_and_nothing_else():
    render = template_engine.render
    assert render("Hi {{ name }} and {{name}}!", {"name": "A"}, escape=False) == "Hi A and A!"
    assert render("{{ missing }}", {}, escape=False) == ""
    assert render("{{ x }}", {"x": "<b>&\"'"}, escape=True) == "&lt;b&gt;&amp;&quot;&#x27;"
    assert render("{{ x }}", {"x": "{{ y }}", "y": "SECRET"}, escape=False) == "{{ y }}"
    assert render("{{ 1+1 }}{{ x.y }}", {"x": "v"}, escape=False) == ""
    problems = template_engine.check("{{ a }} {{ b.c }} {% x %} {{ d }} {{", {"a"})
    assert {p.code for p in problems} == {
        "invalid_placeholder",
        "unknown_merge_field",
        "template_logic",
        "unbalanced_placeholder",
    }
    assert template_engine.check("{{ a }} and {{a}}", {"a"}) == []
    assert template_engine.one_line("a\r\n b \n\n c") == "a b c"
