# Audit log

The audit log is the practice's permanent record of who did what: every create, update and delete
of a record that matters, every approval, invoice issue, payment, write-off, rate change, period
lock and status change, and the sign-ins of the people who use the practice's data. An entry
captures the **actor**, the **action**, the **record** it is about, the **time**, the caller's
**IP address** (and user agent and request id) and, where the action changed something, the
**before and after** of each field. Entries are written by the service that does the work, inside
the same transaction, so there is never a change without its entry (or an entry without its
change).

The trail is **append-only**: a database trigger refuses every `UPDATE` and `DELETE`, and no
endpoint can change or remove an entry (see [Append-only](#append-only)).

All paths are under `/api/v1/organizations/{organization_id}/audit-logs`.

## Endpoints

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `` (the collection) | `audit_log.read` | The practice's entries, newest first, with filters and text search |
| GET | `/history` | `audit_log.read` **or** the record type's own read permission | The entries about one record |
| GET | `/export` | `audit_log.read` **and** `audit_log.export` | The entries as CSV or Excel |

Permission checks come before anything is looked up, so a caller without the permission gets 403
whatever they send. A practice the caller is not an active member of answers 404 on every route.
Entries of another practice are never visible: every query is scoped by the practice, and an id of
another practice's record simply has no entries here.

| Role | `audit_log.read` | `audit_log.export` |
|---|---|---|
| Owner, Admin, Partner | yes | yes |
| Manager, Staff, Billing, Viewer | no | no |

A custom role may hold either permission; the export needs both (`docs/modules/permissions.md`).
Staff, Manager, Billing and Viewer can still read the **history of a record** they may read (below).

## The entries

An entry is one row of `audit_logs` (monthly partitions, never deleted by the application):

| Column | Meaning |
|---|---|
| `id`, `created_at` | The entry and when it was written (UTC) |
| `organization_id` | The practice; null for account-level entries (sign-ins), which belong to no practice and are never listed |
| `actor_user_id` | Who did it; null for what the system did |
| `action` | `<area>.<what happened>`, e.g. `invoice.issued`, `client.updated`, `job.status_changed` |
| `target_type`, `target_id` | The record the action is about (`invoice`, `client`, `job`, ... and its id); `audit_log` with no id for an export |
| `ip_address`, `user_agent`, `request_id` | Where the request came from; the request id ties the entry to the logs and to the other entries of the same request |
| `metadata` | What the service recorded: the record's name or number, the amounts and, for an update, `changes` |

Several entries can share a request (a payment that allocates writes `payment.recorded` and
`payment.allocated`). An update that changes nothing writes nothing.

### What changed: `changes`

Services record what changed in a few shapes. The API turns all of them into the same list,
`changes: [{field, before, after}]` (empty when the entry records a creation, a deletion or an
action without a before and after), so no client has to know them:

| Stored in `metadata` | Example | Becomes |
|---|---|---|
| `changes: {field: [old, new]}` (the usual one) | `{"changes": {"name": ["Acme", "Acme Group"]}}` | `name: Acme -> Acme Group` |
| `old` and `new` plain values | `job.status_changed` `{"old": "planned", "new": "on_hold"}` | `status: planned -> on_hold` (the field is the action's name without `_changed`) |
| `old` and `new` objects | `member.role_changed` `{"old": {"role_id", "role_name"}, "new": {...}}` | one change per key whose value differs; an `x_id` gives way to `x_name` |
| `old_x` / `new_x` pairs | `timesheet.approved` `{"old_status": "submitted", "new_status": "approved"}` | `status: submitted -> approved` |
| `from` and `to` | `{"from": "open", "to": "closed"}` | `status: open -> closed` |

Values are JSON as the service stored them: money in pence, dates ISO 8601, ids as strings. A
field that was empty has `before: null`; one that was cleared has `after: null`.

### `label` and `summary`

`label` is the action in words (`Invoice issued`, `Client details updated`, `Signed in`; an action
that is not in the catalogue gets one made from its name). `summary` is the label with the record
it is about, built from the common name keys of `metadata`: `"Invoice issued: INV-00012 (Babbage
Ltd)"` (number or code, then the name, then the client), at most 200 characters.

## `GET /audit-logs`

Newest first, cursor-paginated (`limit` 1-100, default 25; `cursor` from `meta.next_cursor`;
`meta.has_more`).

| Query parameter | Rule |
|---|---|
| `action` | 1-100 characters: an exact action (`invoice.issued`), or a prefix when it ends with a dot (`invoice.`) |
| `actor_user_id` | uuid: only that person's entries |
| `from`, `to` | ISO 8601 timestamps, inclusive; one without an offset is read as UTC |
| `target_type` | 1-60 characters, `^[a-z][a-z0-9_]*$`: only entries about records of this type |
| `target_id` | uuid: only entries about this record |
| `q` | 1-200 characters, trimmed: case-insensitive text search over the action, the record type, the IP address, the person's name and email and the recorded details. `%` and `_` are ordinary characters. A substring search over the details: combine it with a date range on a large log |

`AuditLogEntryRead` (an element of `data`; the first nine fields are the original ones, the
rest were added without changing them):

```json
{
  "id": "…", "created_at": "2026-10-05T09:14:07.123456Z", "action": "client.updated",
  "label": "Client details updated",
  "summary": "Client details updated: C00012 · Acme Group",
  "actor": {"id": "…", "full_name": "Ada Lovelace", "email": "ada@example.com"},
  "target_type": "client", "target_id": "…",
  "ip_address": "203.0.113.7", "user_agent": "Mozilla/5.0 …",
  "changes": [{"field": "name", "before": "Acme", "after": "Acme Group"}],
  "metadata": {"code": "C00012", "name": "Acme Group", "changes": {"name": ["Acme", "Acme Group"]}}
}
```

`actor` is null for what the system did. `metadata` is exactly what the service recorded, except
that **cost figures are `null` for a caller without `rate.read`** (see [Cost figures](#cost-figures)).

## `GET /audit-logs/history`

The entries about **one record**, newest first, same pagination and the same `action`,
`actor_user_id`, `from`, `to` and `q` filters. This is what a record's History tab shows.

| Query parameter | Rule |
|---|---|
| `target_type` | **required**, 1-60 characters, `^[a-z][a-z0-9_]*$` |
| `target_id` | **required** uuid |
| `include_related` | boolean, default false. For a **client** it adds the entries that name the client in their details (`client_id`): its contacts, services, rates, jobs, time, invoices, credit notes and payments. Ignored for every other record type |

`AuditHistoryEntryRead` (an element of `data`):

```json
{
  "id": "…", "action": "invoice.issued", "label": "Invoice issued",
  "actor": {"id": "…", "name": "Ada Lovelace"},
  "at": "2026-10-05T09:14:07.123456Z",
  "ip_address": "203.0.113.7",
  "target_type": "invoice", "target_id": "…",
  "changes": [],
  "summary": "Invoice issued: INV-00012 (Babbage Ltd)"
}
```

`actor` is null for what the system did. `ip_address` is `null` for a caller who does not hold
`audit_log.read`.

### Who may read a record's history

A caller with `audit_log.read` reads the history of **every** record type. Anyone else needs the
**read permission of the record's own type**; otherwise the answer is 403, whatever the id:

| `target_type` | Record | Any of |
|---|---|---|
| `client`, `client_contact`, `client_service` | A client, its contacts and engagements | `client.read` |
| `job`, `task` | A job and its tasks | `job.read` |
| `invoice`, `credit_note` | An invoice, a credit note | `invoice.read` |
| `payment` | A payment | `payment.read` |
| `disbursement`, `wip_write_off` | A disbursement, a WIP write-off | `wip.read` |
| `time_entry`, `timesheet` | A time entry, a timesheet | `time.read_all` |
| `expense_claim`, `expense`, `expense_receipt` | A claim, its lines, its receipts | `expense.read_all` |
| `rate_rule`, `staff_rate`, `client_rate` | Rates | `rate.read` |
| `ticket`, `ticket_comment`, `ticket_attachment` | A ticket and what hangs from it | `ticket.read` |
| `period_lock` | The practice's period lock | `period_lock.read` |
| anything else (`member`, `role`, `organization`, `vat_rate`, `sequence`, ...) | Staff, roles and settings | `audit_log.read` only |

With `include_related`, a caller without `audit_log.read` only gets the related entries about
record types they may read (a Viewer sees a client's invoices but not its rates).

The history of a record that was deleted is still there (`client.deleted` ends it). A record of
another practice has no entries in this practice: the answer is `200` with an empty list, never
someone else's entries.

### Cost figures

A person without `rate.read` never sees cost rates, cost values or margins, in the trail either.
For such a caller every `metadata` key named `cost`, `margin` or containing them as a word
(`cost_rate`, `cost_value_delta`, `margin_bp`) is `null`, and so is the `rate` of a rate rule of
kind `cost`; the corresponding `changes` disappear. The same holds for the export.

## `GET /audit-logs/export`

The entries that match the list filters (`action`, `actor_user_id`, `from`, `to`, `target_type`,
`target_id`, `q`), newest first, as a file. Needs **both** `audit_log.read` and
`audit_log.export`.

| Query parameter | Rule |
|---|---|
| `format` | `csv` (default) or `xlsx` (anything else is 422) |

* **At most 50,000 entries** are exported, the newest. A larger result is cut; the response header
  `X-Export-Truncated` is `true` (`false` otherwise), `X-Export-Row-Count` is the number of rows,
  and the workbook says so in a note. Narrow the filters (a date range) to export the rest.
* The columns, in order: `Time (UTC)` (ISO 8601, `2026-10-05T09:14:07Z`), `User`, `Email`,
  `Action`, `Event` (the label), `Summary`, `Record type`, `Record id`, `Changes`
  (`name: Acme → Acme Group; fee: 100 → 120`), `IP address`, `Device`, `Request id` and
  `Details (JSON)` (the recorded details, compact, at most 30,000 characters).
* CSV is UTF-8 with a byte-order mark and CRLF line ends. Text that starts with `=`, `+`, `-` or
  `@` is prefixed with `'` in both formats, so a spreadsheet never runs it as a formula.
* The workbook (`Audit log` sheet) carries a title and notes above the table: the practice, who
  exported and when, the filters and the number of entries.
* The response is an attachment `audit-log-YYYY-MM-DD.<csv|xlsx>` (the date is the practice's
  today) with `Cache-Control: no-store`.
* **The export is itself audited**: one `audit_log.exported` entry (`target_type` `audit_log`, no
  `target_id`) with `{format, row_count, truncated, filters}`, written after the rows were read:
  an export never contains its own entry, the next one does. A refused request (403, 422) writes
  nothing.

## Append-only

Migration `043` adds a `BEFORE UPDATE OR DELETE` row trigger to `audit_logs`
(`trg_audit_logs_append_only`, function `reject_audit_log_change`). It exists on the table, on
every monthly partition and on the default partition, and PostgreSQL clones it onto every
partition created later (`create_monthly_partition`). Any `UPDATE` or `DELETE` fails with
`audit_logs is append-only: UPDATE is not allowed` (SQLSTATE `23001`), whoever runs it.

* The table has no foreign key, so deleting a practice (a soft delete) or even removing its row
  never touches its entries. There is no cascade to allow for.
* No endpoint modifies or removes an entry (the audit routes are `GET` only; `PUT`, `PATCH`,
  `DELETE` and `POST` answer 404 or 405), and a test scans the application source for any
  `UPDATE`/`DELETE` of `audit_logs`.
* `TRUNCATE` and dropping a partition are administrative operations, not row changes, and are not
  blocked by the trigger (the test suite empties the table between tests). In production the
  application's database role should not be granted `TRUNCATE` or ownership of the table; keep
  partitions for at least six years (`docs/plan.md`).

The migration also adds the index `ix_audit_logs_organization_id_target_created_at`
(`organization_id`, `target_type`, `target_id`, `created_at`) that serves the record history and
the `target_type`/`target_id` filters.

## Error codes

| Status | Code | When |
|---|---|---|
| 401 | `AUTH_REQUIRED`, `AUTH_TOKEN_INVALID` | No or invalid token |
| 403 | `PERMISSION_DENIED` | The list or export without the permission(s); a history of a record type the caller may not read |
| 404 | `RESOURCE_NOT_FOUND` | Not an active member of the practice |
| 422 | `VALIDATION_ERROR` | A bad filter, `limit`, `cursor`, `format`, `target_type` or id (`details[0].field` names it) |

## What is recorded

The trail holds what each module writes; the full list of actions by module:

| Module | Actions (`target_type`) |
|---|---|
| Sign-in (`user`, `session`; no practice) | `auth.registered`, `auth.email_verified`, `auth.login`, `auth.login_failed`, `auth.logout`, `auth.logout_all`, `auth.refresh_reuse_detected`, `auth.password_reset_requested`, `auth.password_reset`, `auth.password_changed`, `auth.session_revoked` |
| Practice (`organization`) | `organization.created`, `.updated` (with `changes`), `.deleted` |
| Staff and roles (`member`, `role`) | `member.created`, `.updated`, `.role_changed`, `.suspended`, `.reactivated`, `.removed`, `.password_reset`; `staff.created`, `.updated`, `.deactivated`, `.left`, `.reactivated`, `.deleted`, `.invited`, `.login_linked`, `.login_unlinked`; `role.created`, `.updated`, `.permissions_changed`, `.deleted` |
| Clients (`client`, `client_contact`, `client_service`) | `client.created`, `.updated`, `.archived`, `.deleted`, `.imported`, `.central_*`; `client_contact.*`, `client_service.*` |
| Jobs (`job`, `task`) | `job.created`, `.updated`, `.status_changed`, `.assignees_changed`, `.copied`, `.deleted`, `.budget_alert`, `.tasks_reordered`; `task.created`, `.updated`, `.status_changed`, `.assignees_changed`, `.deleted` |
| Time (`time_entry`, `timesheet`) | `time_entry.created`, `.updated`, `.deleted`, `.copied`, `.week_copied`, `.corrected`; `timesheet.submitted`, `.recalled`, `.approved`, `.rejected`, `.reopened`, `.reminders_sent` |
| Rates (`rate_rule`, `staff_rate`, `client_rate`, `rate_rerun`) | `rate.created`, `.updated`, `.deleted`, `.rerun`; `staff_rate.*`, `client_rate.*`; `time_entries.revalued` |
| Expenses (`disbursement`, `expense_claim`, `expense`, `expense_receipt`, `expense_type`) | `disbursement.*`; `expense_claim.created`, `.updated`, `.deleted`, `.submitted`, `.recalled`, `.returned`, `.approved`; `expense.created`, `.updated`, `.corrected`, `.deleted`; `expense_receipt.added`, `.deleted`; `expense_type.*` |
| Billing (`invoice`, `credit_note`, `payment`, `wip_write_off`) | `invoice.draft_created`, `.wip_added`, `.updated`, `.line_added`, `.line_updated`, `.line_removed`, `.draft_deleted`, `.issued`, `.voided`, `.send_queued`; `credit_note.issued`; `payment.recorded`, `.allocated`, `.unallocated`, `.voided`; `wip.written_off`, `.write_off_reversed` |
| Period lock (`period_lock`) | `period_lock.locked`, `.reopened` (with `reason` and `changes.locked_through`) |
| Settings | `vat_rate.*`, `service.*`, `activity_code.*`, `grade.*`, `sequence.updated`, `invoice_template.*`, `email_template.updated`/`.reset`, `notification_settings.updated` |
| Tickets (`ticket`, `ticket_comment`, `ticket_attachment`, `ticket_tag`) | `ticket.created`, `.updated`, `.status_changed`, `.deleted`; `ticket_comment.*`; `ticket_attachment.added`/`.deleted`; `ticket_tag.*` |
| The audit log (`audit_log`) | `audit_log.exported` |

`*` stands for `created`, `updated` and `deleted` (or `deactivated`). Sign-ins belong to the
account, not to a practice: they are recorded (actor, session, IP address, user agent) but have no
`organization_id`, so the practice's list never shows them.

A coverage test (`tests/test_audit_coverage.py`) drives the representative operations through the
API (create, update, archive and delete a client, sign in, fail a sign-in, approve a timesheet,
issue an invoice, record and void a payment, write off and reverse, credit note, rate change,
period lock and reopen, job status change, role change, staff, roles and VAT rates ...) and
asserts exactly one entry each, with the actor, the record, the IP address, the request id and the
before and after where the action has them.
