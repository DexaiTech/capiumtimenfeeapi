# Notifications, email templates and the messaging gateway

Code: `app/api/v1/{notifications,email_templates}.py` → `app/services/{notifications,notification_settings,notification_events,notification_delivery,email_templates,template_catalogue,template_engine,template_render,email}.py`
and `app/services/messaging/{base,smtp,mock}.py` → `app/repositories/{notifications,email_templates}.py`; schemas in
`app/schemas/{notifications,email_templates}.py`; models `app/db/models/notifications.py`; the type catalogue
`app/core/notification_types.py`; the worker handlers in `app/workers/handlers.py`.
Tests: `tests/test_notifications.py`, `test_notification_settings.py`, `test_email_templates.py`,
`test_messaging_gateway.py` (helpers in `tests/notification_helpers.py`).
Migrations: `013` (`notifications`, `notification_preferences`, `notification_settings`,
`organization_members.mobile_number`), `014` (`email_templates`, `message_log`).
**No permission was added**: the practice-level endpoints use `organization.update` **or** `settings.manage`.

All paths are under `/api/v1/organizations/{organization_id}`. Another practice's ids answer 404. The interactive
schema is `/openapi.json`. Spec: requirements 2.21 (and the Settings pages "Notifications" and "Email Templates").

## The idea

* A module that wants to tell someone something calls **`NotificationService.notify(...)`** (below) inside its own
  transaction. For each recipient it adds an **in-app** row (the bell) and queues an **email** and/or **SMS** job in
  the same transaction, according to the practice's switches and the person's preferences.
* Email and SMS leave only through the **messaging gateway** (`MESSAGING_BACKEND=smtp|mock`). The mock records every
  message in `message_log`; SMS is always the mock.
* The text of the emails is a **template** per key: built in (code), overridable per practice, edited with plain
  `{{ field }}` merge fields only (nothing is ever evaluated).

## Endpoint index

| Method | Path | Who |
|---|---|---|
| GET | `/notifications` | any member (own notifications) |
| GET | `/notifications/unread-count` | any member |
| POST | `/notifications/read-all` | any member |
| POST | `/notifications/{notification_id}/read` | any member |
| GET, PUT | `/notification-settings` | any member (own settings) |
| GET, PUT | `/notification-settings/practice` | `organization.update` or `settings.manage` |
| GET | `/email-templates` | `organization.update` or `settings.manage` |
| GET, PUT, DELETE | `/email-templates/{key}` | `organization.update` or `settings.manage` |
| POST | `/email-templates/{key}/preview` | `organization.update` or `settings.manage` |

Owner, Admin and Partner hold both permissions; Manager, Staff, Billing and Viewer neither (403). A custom role with
either one is enough. Reading templates needs the same permission as writing them.

## Notifications (the bell)

### `GET /notifications`

The caller's own notifications, **newest first**, cursor-paginated (`limit` 1–100, default 25; `cursor` from
`meta.next_cursor`; `meta.has_more`). `unread=true` lists only unread ones. Nobody sees anyone else's.

```json
{
  "id": "…", "type": "approval_pending",
  "title": "Timesheet waiting for approval",
  "body": "Pat Person submitted their timesheet for the week starting 07/09/2026 (1:30 hours).",
  "link": "/time/approvals",
  "entity_type": "timesheet", "entity_id": "…",
  "payload": {"week_start": "2026-09-07", "total_minutes": 90},
  "is_read": false, "read_at": null, "created_at": "2026-09-10T08:30:00Z"
}
```

* `type` is one of the catalogue codes (table below). The UI picks the icon from it.
* `link` is a **path inside the practice's app, relative to `/{practice slug}`** (the UI opens `/{slug}{link}`), or
  `null`. It always starts with a single `/`; it is never an absolute URL (enforced in code and by a CHECK).
* `entity_type` / `entity_id` name the record concerned (both or neither); `payload` is free JSON.

### `GET /notifications/unread-count`

`data: {"count": 3}` — the number on the bell. Cheap (a partial index); the UI polls it.

### `POST /notifications/{notification_id}/read`

Marks one read and returns it. **Idempotent**: an already read notification keeps its `read_at`. Another person's
notification (and another practice's) is **404** `RESOURCE_NOT_FOUND`.

### `POST /notifications/read-all`

Marks every unread notification of the caller read. `data: {"updated": 2}` (0 when there was nothing to do).

## Notification settings

### Catalogue (`app/core/notification_types.py`)

| `type` | Raised when | Recipients | Default channels |
|---|---|---|---|
| `timesheet_reminder` | A module reminds people to submit (later module) | The people concerned | in-app, email |
| `timesheet_returned` | A timesheet is rejected (**implemented**) | The timesheet's owner | in-app, email |
| `approval_pending` | A timesheet is submitted (**implemented**) | The person's manager if they are active and hold `timesheet.approve`, else every active member with `timesheet.approve`; never the submitter | in-app, email |
| `budget_alert` | A job reaches a budget threshold (later module) | The people concerned | in-app, email |
| `invoice_delivery` | The invoice email was delivered (**implemented**) | The person who sent it | **in-app only** |
| `payment_reminder` | An invoice is overdue (later module) | The people concerned | in-app, email |
| `failed_job` | A background job failed for good (**implemented**) | Practice administrators: active members with `organization.update` or `settings.manage` | in-app, email |

SMS is **off** by default for every type. `invoice_delivery` does not email the sender by default because the sender
just asked for that email to be sent. Adding a type = one entry in `CATALOGUE` and a template of the same key in
`template_catalogue.py` (a test refuses a type without a template); no migration (the database holds the code as text).

### What reaches a person

Per type and channel (`in_app`, `email`, `sms`):

1. The practice has the type **switched off** → nothing at all, in-app included, whatever the person prefers.
2. Else the person's **own choice** for the type, if they made one.
3. Else the **practice's default** for the type, if it set one.
4. Else the catalogue default (above).

Only **active members who can sign in** are notified (suspended, left and no-login staff are skipped). SMS is queued
only for people who have saved a mobile number.

### `GET /notification-settings` / `PUT /notification-settings` (own)

```json
{
  "sms_number": "+4407700900123",
  "preferences": [
    {"type": "budget_alert", "label": "Budget alert", "description": "…",
     "enabled": true, "in_app": true, "email": true, "sms": false, "customised": false}
  ]
}
```

`preferences` always lists **every** catalogue type in catalogue order. `enabled` is the practice's switch (when false the
UI shows the row disabled: nothing is sent whatever the choice). `customised` is true when the person made the choice,
false when they inherit the practice default. `sms_number` is the mobile number SMS goes to, per practice membership.

`PUT` body (extra fields are 422):

```json
{
  "preferences": [{"type": "budget_alert", "in_app": true, "email": false, "sms": true}],
  "sms_number": "07700 900123"
}
```

* Listed types replace the person's choice for that type (all three channels required); **unlisted types keep theirs**.
* `sms_number` omitted = unchanged; `null` or blank clears it; otherwise spaces, dashes, dots and brackets are dropped and
  7–15 digits with an optional leading `+` are stored (`"+44 (0)7700 900-123"` → `"+4407700900123"`).
* Turning `sms` on for any listed type needs a number (the one sent or the one on file): else 422 on `sms_number`
  (`code: required`).
* Returns the full settings as `GET`. Personal choices are not audited.

### `GET /notification-settings/practice` / `PUT …/practice`

Needs `organization.update` or `settings.manage` (403 otherwise, for `GET` too).

```json
{"types": [{"type": "budget_alert", "label": "Budget alert", "description": "…",
            "enabled": true, "in_app": true, "email": true, "sms": false}]}
```

`enabled` switches the type on or off for the whole practice; `in_app`/`email`/`sms` are the channels people get **until
they choose their own**. `PUT` takes `{"types": [{"type", "enabled", "in_app", "email", "sms"}, …]}` (1–100 items, each
type once, all fields required); unlisted types keep their settings. Audited as `notification_settings.updated` with
`changes: {type: {field: [old, new]}}`; saving what is already set writes nothing.

## Email templates

### Catalogue (`app/services/template_catalogue.py`)

| `key` | `category` | Sent when | Merge fields |
|---|---|---|---|
| `timesheet_reminder` | notification | a timesheet reminder is delivered | `recipient_name practice_name app_name week_start link` |
| `timesheet_returned` | notification | a timesheet is returned | `recipient_name practice_name app_name week_start approver_name decision_note link` |
| `approval_pending` | notification | a timesheet waits for approval | `recipient_name practice_name app_name submitter_name week_start total_hours link` |
| `budget_alert` | notification | a budget alert is delivered | `recipient_name practice_name app_name client_name job_name budget_percent link` |
| `invoice_delivery` | notification | an invoice was sent (email is off by default) | `recipient_name practice_name app_name client_name invoice_number amount_due sent_to link` |
| `payment_reminder` | notification | an invoice is overdue | `recipient_name practice_name app_name client_name invoice_number amount_due due_date days_overdue link` |
| `failed_job` | notification | a background job failed | `recipient_name practice_name app_name job_type attempts` |
| `invoice` | system | an invoice is emailed to the client (with the PDF) | `recipient_name practice_name app_name client_name invoice_number issue_date due_date amount_due bank_details` |
| `staff_invitation` | system | a staff member is invited | `recipient_name practice_name app_name invited_by link login_url` (**`link` required**) |
| `account_created` | system | an administrator creates an account | `recipient_name practice_name app_name added_by link login_url` (**`link` required**) |

The five account emails of the central login (verification, password reset, welcome, password changed, account exists)
belong to no practice and are **not listed** or customisable. For the three `system` templates the built-in design is the
existing Jinja template in `app/templates/email/` (still what a practice that has not customised them sends); the
default text listed by the API is its plain `{{ field }}` equivalent, the starting point for customising. The seven
`notification` templates are built in code.

A **merge field** has `name`, `label`, `description` and a `sample` value (used by previews). `link` in a notification email
is `FRONTEND_URL/{practice slug}{notification link}`. Values are always strings.

### Shape

```json
{
  "key": "budget_alert", "name": "Budget alert", "description": "Warns when a job has used a set share of its budget.",
  "category": "notification", "notification_type": "budget_alert",
  "merge_fields": [{"name": "job_name", "label": "Job name", "description": "The job concerned.", "sample": "Year-end accounts 2026"}],
  "required_fields": [],
  "customised": true,
  "subject": "Alert: {{ job_name }}", "html_body": "<p>…</p>", "text_body": "…",
  "updated_at": "2026-10-18T09:00:00Z", "updated_by": "…", "updated_by_name": "Ada Admin"
}
```

`subject`, `html_body` and `text_body` are the **effective** template: the practice's version when `customised`, else the
built-in one (`updated_*` are null then). The HTML body is the message itself; it is placed in the shared branded layout
(`app/templates/email/_message.html`, header = practice name) when sent.

### `GET /email-templates` · `GET /email-templates/{key}`

All ten templates in catalogue order, or one. Unknown key: 404 (after the permission check, so a caller without
permission gets 403 for any key).

### `PUT /email-templates/{key}`

`{"subject": "…", "html_body": "…", "text_body": "…"}`, all required, trimmed: subject 1–200 characters, HTML 1–100 000,
text 1–50 000. Creates or replaces the practice's version; returns it. **Validation (422, `details: [{field, code, message}]`
with an entry per problem, all at once):**

| `code` | Meaning |
|---|---|
| `unknown_merge_field` | `{{ name }}` is not a field of this template (the message lists the available ones) |
| `invalid_placeholder` | Anything between `{{ }}` that is not a plain lower-case field name: attribute access (`{{ ''.__class__ }}`), calls, filters, indexing, arithmetic |
| `template_logic` | Statements or comments: `{% … %}`, `{# … #}` |
| `unbalanced_placeholder` | A `{{` or `}}` that is not part of a complete placeholder |
| `unsafe_html` | In `html_body`: `<script>`, `<iframe>`, `<object>`, `<embed>`, `<form>`, `<base>`, `<meta>`, `<link>`, `javascript:` links or `on…=` event handlers |
| `multiline_subject` | A line break in the subject |
| `missing_merge_field` | A required field (`link` of the two invitations) is not in the HTML and the text body |
| `string_too_short` / `string_too_long` / `missing` / `extra_forbidden` | Empty (also blank) subject or bodies, over the limits, missing or unknown request fields |

Nothing is evaluated: rendering is a regular-expression substitution of `{{ field }}` by the field's value (no Jinja, no
attribute access), so a template can never execute code or read a secret that is not among the values it is given.
Values are HTML-escaped in the HTML body, left as they are in the text body, and the subject is one line. A value is text
and is never expanded again (`{{ link }}` inside a value stays literally `{{ link }}`). Saving what is already saved writes
and audits nothing. Audited as `email_template.updated` (`meta`: `template_key`, `template_name`, `created`,
`changed_fields`, and `changes: {subject: [old, new]}` when the subject changed).

### `DELETE /email-templates/{key}`

Resets to the built-in template and returns it (`customised: false`). Idempotent. Audited as `email_template.reset` when a
version was deleted.

### `POST /email-templates/{key}/preview`

Renders without saving or sending. Body (optional, every part optional): a **draft** (`subject`, `html_body`, `text_body`;
omitted parts are the saved template's) and `values` (`{field: text}`, up to 50, each ≤ 500 characters, only the template's
fields — else 422 `values.<name>`). The draft is validated exactly like a save (422 with the same codes). Returns:

```json
{"subject": "Hi Ada: Q3", "html": "<!doctype html>…", "text": "…", "values": {"job_name": "Q3", "…": "…"}}
```

`html` is the complete email as it would be sent; show it in a sandboxed frame. Fields without a given value use their
sample, except `recipient_name` (the caller), `practice_name`, `app_name`, `link` and `login_url`, which are real. `values`
lists what was used for every field.

## Delivery

`notify` queues one **`send_notification`** job per person and channel (`background_jobs`, same transaction). The job carries
ids and text, never an address or a number: the worker looks the recipient up when it runs and skips (job succeeds with
`skipped`) a person who is no longer active (`recipient_unavailable`) or whose SMS number was removed (`no_mobile_number`).
Email renders the practice's template for the type (else the built-in one) with the merge values the caller gave plus the
recipient's name, the practice name and the link, and sends it through the gateway. SMS is
`{practice}: {title} - {body} {absolute link}` (320 characters at most). A refused recipient fails the job (`failed`, no
retry); any other error is retried by the job machinery (backoff 30 s, 60 s, …, five attempts). The three practice-facing
`system` emails look for the practice's version in the `send_email` / `send_invoice_email` jobs (the payload carries
`organization_id`).

## The messaging gateway (`app/services/messaging/`)

```python
class MessageGateway(Protocol):
    name: str

    async def send_email(self, message: OutboundEmail) -> DeliveryResult: ...
    async def send_sms(self, message: OutboundSms) -> DeliveryResult: ...
```

| Backend | Email | SMS |
|---|---|---|
| `smtp` (`SmtpGateway`) | SMTP as before (`aiosmtplib`) | delegated to the mock |
| `mock` (`MockGateway`) | recorded in `message_log`, nothing leaves | recorded in `message_log` |

`MESSAGING_BACKEND=smtp|mock`; unset = `smtp` when `SMTP_HOST` is set, else `mock`. `smtp` without `SMTP_HOST` is refused at
start-up. `EmailService` (verification, password reset, invitations, invoice emails, notifications) and every SMS go through
`build_gateway(settings)`.

* **`message_log`** (mock only): `created_at`, `organization_id` (null for account emails), `user_id`, `channel`
  (`email`/`sms`), `recipient`, `subject`, `body` (text or SMS text), `html_body`, `template_key`, `status`
  (`sent`/`failed`), `provider_ref` (`mock-…`), `error`. Rows are written in a transaction of their own: **a failed attempt
  stays on record even though the job's transaction rolls back**. **One-time links are removed** (`[removed]`) from the
  stored subject and bodies. No endpoint reads the log; it is for development, support and tests.
* **Failures**: a gateway raises on a transient failure (the job is retried with backoff) and `PermanentSendError` when the
  message can never be delivered (the job fails at once). The SMTP gateway logs `email_failed`/`email_rejected` without the
  body; the mock logs the failed row with `error`.
* Housekeeping (`purge_retention`): notifications older than 180 days and `message_log` rows older than 90 days are deleted.

## The `notify` API (for the modules that raise notifications)

```python
from app.services.notifications import NotificationService

result = await NotificationService(session).notify(
    organization_id=ctx.organization_id,
    type="budget_alert",                  # a catalogue code (or NotificationType.BUDGET_ALERT)
    recipients=[user_id, …],              # user ids of members of the practice
    title="Budget alert: Year-end accounts",          # ≤ 200 chars, required (clipped)
    body="Year-end accounts for Widgets Ltd has used 80% of its budget.",   # ≤ 2000 chars, also the SMS text
    link="/jobs/…",                       # path inside the app, relative to /{slug}; optional
    entity_type="job", entity_id=job.id,  # both or neither; optional
    payload={"percent": 80},              # extra JSON kept on the row; optional
    dedupe_key="budget_alert:<job id>:80",# optional: makes the call idempotent per recipient
    merge={"client_name": "Widgets Ltd", "job_name": "Year-end accounts", "budget_percent": 80},  # email merge fields
)
# result: NotifyResult(created, duplicates, emails_queued, sms_queued, unavailable, suppressed)
```

* Call it **inside your own `unit_of_work`**; it does not commit. The inbox rows and the jobs are part of your transaction.
* `link` must be a path starting with a single `/` (no `//`, no backslash, no spaces): `ValueError` otherwise. `type` must be
  in the catalogue and `title` non-empty: `ValueError` (programming errors, not user input).
* **Idempotent per `dedupe_key`**: a person who already has a notification with the key gets nothing again (no second row, no
  second email or SMS). The key is unique per person across all types — start it with the type (`dedupe_key(type, *parts)`
  does). For a person with the in-app channel off the guard is the queued job's key, which also remembers finished jobs for
  the 14 days they are kept. Without a key every call notifies.
* Recipients who are not active members with a login, and every recipient when the practice switched the type off, are skipped
  (`unavailable`, `suppressed`).
* A trigger that must never fail its own action wraps the call in a savepoint: use the helpers below, which do.

### Ready-made helpers (`app/services/notification_events.py`)

Each builds the title, body, link, dedupe key and merge values and returns `NotifyResult | None` (None when the call
failed and was logged). All take `session` and keyword arguments.

| Helper | Arguments | Dedupe key (once per …) |
|---|---|---|
| `timesheet_reminder` | `organization_id, recipients, week_start: date` | person and week |
| `timesheet_returned` | `organization_id, timesheet_id, owner_id, approver_name, week_start, note, decided_at` | rejection |
| `approval_pending` | `organization_id, timesheet_id, submitter_id, submitter_name, week_start, total_minutes, submitted_at` | submission |
| `budget_alert` | `organization_id, recipients, job_id, job_name, client_name, percent: int` | job and threshold |
| `invoice_delivery` | `organization_id, sender_id, invoice_id, invoice_number, client_name, amount_due: str, sent_to, request_key` | send request |
| `payment_reminder` | `organization_id, recipients, invoice_id, invoice_number, client_name, amount_due: str, due_date, days_overdue, stage: str` | invoice and `stage` |
| `failed_job` | `job: FailedJob(job_id, organization_id, job_type, attempts, status)` | job |

Constants: `LINK_TIMESHEET` (`/time`), `LINK_APPROVALS` (`/time/approvals`), `link_job(id)` (`/jobs/{id}`),
`link_invoice(id)` (`/billing/invoices/{id}`), `uk_date(d)` (`dd/MM/yyyy`), `hours(minutes)` (`h:mm`),
`approvers_of(session, organization_id, submitter_id)`.

### The triggers that exist today

| Event | Where | Notifies |
|---|---|---|
| Timesheet submitted | `TimesheetService.submit` | `approval_pending` to the approvers |
| Timesheet rejected | `TimesheetService.reject` | `timesheet_returned` to the owner (not when they reject their own) |
| Invoice email delivered | `deliver_invoice_email` (worker; the send request carries `requested_by`) | `invoice_delivery` to the person who sent it |
| Job `failed` or `dead` with a practice | `Worker._run_job` and the lease reaper | `failed_job` to the administrators; jobs of no practice and `send_notification` jobs themselves are skipped |

## Error codes

| Status | Code | When |
|---|---|---|
| 401 | `AUTH_REQUIRED`, `AUTH_TOKEN_INVALID` | Not signed in |
| 403 | `PERMISSION_DENIED` | Practice notification settings or email templates without `organization.update`/`settings.manage` |
| 404 | `RESOURCE_NOT_FOUND` | Another person's or practice's notification, unknown template key, not a member of the practice |
| 422 | `VALIDATION_ERROR` | `details: [{field, code, message}]`: unknown notification type (`preferences.0.type`), a type listed twice, SMS without a number (`sms_number`, `required`), a malformed number, an invalid template (codes above), a bad cursor |

## Audit

`notification_settings.updated` (practice switches, with `changes`), `email_template.updated`, `email_template.reset`
(`target_type: email_template`, `target_id` = the template row). Reading is not audited.
