# Practice settings, rates and clients

Code: `app/api/v1/{settings,rates,clients}.py` → `app/services/{settings,rates,clients,client_import}.py`
→ `app/repositories/{settings,rates,clients}.py`; schemas in `app/schemas/{settings,rates,clients}.py`.
Tests: `tests/test_settings.py`, `tests/test_rates.py`, `tests/test_clients*.py` (helpers in
`tests/clients_helpers.py`). No schema change in the original release (migration `007` is still a
no-op); the Clients 360 release adds migrations `016`–`018` (central clients, the billing settings
and invoice templates, the activity indexes): see "Clients 360" at the end of this document,
`docs/modules/central-clients.md` and `docs/modules/invoice-templates.md`. Leads (migration
`063`, ticket APT49) are described in "Leads" at the end of this document.

All paths are under `/api/v1/organizations/{organization_id}`. Responses use the standard
envelope; money is integer pence, rates are pence per hour, VAT rates basis points. Another
practice's ids answer 404.

> Services, activity codes, rates (dated rate rules, client and staff rates, re-rating), grades and
> staff are extended by the Team & Rates module: **`docs/modules/team-rates.md` is the reference
> for those endpoints** and wins where it differs from the tables below (services and activity
> codes gained fields and unique names, `PUT /services/{id}/activity-codes`, the staff sequence;
> the V1 rate endpoints now sit on `rate_rules` and a used rate answers 409 `RATE_IN_USE`).

## Endpoints

### Settings

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/vat-rates?include_archived=` | any member | Live VAT rates, highest first (archived last when included). Bounded. |
| GET | `/vat-rates/{rate_id}` | any member | One rate. |
| POST | `/vat-rates` | `settings.manage` | Create (`name`, `code`, `rate_bp` 0–10000, `is_default`). |
| PATCH | `/vat-rates/{rate_id}` | `settings.manage` | Change name/code/rate, `is_default`, `is_archived` (archive/restore). |
| DELETE | `/vat-rates/{rate_id}` | `settings.manage` | Delete if unused, else archive. `data.outcome` = `deleted`/`archived`. |
| GET | `/services?include_inactive=` | any member | Services by `sort_order`, name. Bounded. |
| GET | `/services/{service_id}` | any member | One service. |
| POST | `/services` | `settings.manage` | Create (`code` upper-cased `^[A-Z0-9_-]{1,12}$`, `name`, `billing_basis`, `default_fee`, `vat_rate_id`, `sort_order`, `is_active`). |
| PATCH | `/services/{service_id}` | `settings.manage` | Change any field. |
| POST | `/services/{service_id}/deactivate` · `/activate` | `settings.manage` | Toggle `is_active`. |
| DELETE | `/services/{service_id}` | `settings.manage` | Delete if unused, else deactivate (`outcome` = `deactivated`). |
| GET | `/services/{service_id}/task-templates` | any member | The service's default tasks in order: `id`, `title`, `description`, `budget_minutes`, `due_offset_days`, `sort_order`. |
| PUT | `/services/{service_id}/task-templates` | `settings.manage` | Replace the whole list: `{"templates": [{title, description?, budget_minutes?, due_offset_days?}]}` (0-100, in order, all or nothing; `title` 1-200, `description` <= 2000, `budget_minutes` >= 0, `due_offset_days` 0-3650). The same list changes nothing and writes no audit entry; a change is audited as `service.task_templates_changed`. Tasks already on jobs are not touched. `ServiceRead.task_template_count` says how many a service has. |
| GET | `/activity-codes?include_inactive=` | any member | Chargeable first, then by code. Bounded. |
| GET | `/activity-codes/{activity_id}` | any member | One code. |
| POST | `/activity-codes` | `settings.manage` | Create (`code`, `name`, `is_chargeable`, `is_active`). |
| PATCH | `/activity-codes/{activity_id}` | `settings.manage` | Change any field. |
| POST | `/activity-codes/{activity_id}/deactivate` · `/activate` | `settings.manage` | Toggle `is_active`. |
| DELETE | `/activity-codes/{activity_id}` | `settings.manage` | Delete if unused, else deactivate. |
| GET | `/sequences` | any member | The seven sequences (`client`, `credit_note`, `estimate`, `invoice`, `job`, `staff`, `ticket`) with `prefix`, `padding`, `next_number`, `preview`. |
| PATCH | `/sequences/{kind}` | `organization.update` | Change `prefix`, `padding`, `next_number` of `client`/`job`/`invoice`/`credit_note`/`staff`/`ticket`/`estimate`. |

### Rates

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/members/{user_id}/rates` | `rate.read` | Rate history, newest `effective_from` first. |
| POST | `/members/{user_id}/rates` | `rate.manage` | Add `{effective_from, charge_rate, cost_rate=0}`. The response's `cost_rate` is null without `rate.read` (a custom role may hold `rate.manage` alone). |
| DELETE | `/staff-rates/{rate_id}` | `rate.manage` | Remove one history row (409 `RATE_IN_USE` once time falls under it). |
| GET | `/staff-rates/current?on_date=` | `rate.read` | Every active member (by name) with the rate in force on `on_date` (default today in the practice zone); null rate fields when none. |
| GET | `/clients/{client_id}/rates` | `rate.read` | Client rates: everyone-rate first, then per person. |
| POST | `/clients/{client_id}/rates` | `rate.manage` | `{user_id: uuid or null, hourly_rate, effective_from?, effective_to?}` (null = everyone; the period defaults to every day). |
| PATCH | `/clients/{client_id}/rates/{rate_id}` | `rate.manage` | `{hourly_rate}`. |
| DELETE | `/clients/{client_id}/rates/{rate_id}` | `rate.manage` | Remove (409 `RATE_IN_USE` once time falls under it). |

### Clients

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/clients` | `client.read` | The client list (see below). Keyset-paginated. |
| GET | `/clients/summary` | `client.read` | Header counts. |
| GET | `/clients/export?format=csv` | `client.read` | Same filters/sort as the list, all rows, CSV (UTF-8 BOM). |
| POST | `/imports/clients?dry_run=` | `client.create` | CSV upload (see Import). |
| POST | `/clients` | `client.create` | Create (optionally with `contacts`). Returns the overview. |
| GET | `/clients/{client_id}` | `client.read` | Client overview (detail). |
| GET | `/clients/{client_id}/overview` | `client.read` | The Client 360 figures behind the tabs (see "Clients 360"). |
| POST / DELETE | `/clients/{client_id}/central-link` | `client.create` | Link to / unlink from a central client (`docs/modules/central-clients.md`). |
| POST | `/clients/{client_id}/central-sync` · `/clients/central-sync` | `client.update` | Synchronise the centrally managed name and address of one / every linked client. |
| PATCH | `/clients/{client_id}` | `client.update` | Change any field (archive transitions also need `client.delete`). |
| POST | `/clients/{client_id}/archive` | `client.delete` | Status → `archived`. |
| POST | `/clients/bulk` | `client.update` (`client.delete` for `delete` and for archiving) | One action on 1-200 clients, all or nothing (see "Bulk action"). Declared before `/clients/{client_id}`. |
| DELETE | `/clients/{client_id}` | `client.delete` | Soft delete when unused, else 409 `CLIENT_IN_USE`. |
| GET / POST | `/clients/{client_id}/contacts` | `client.read` / `client.update` | List / add contact. |
| PATCH / DELETE | `/clients/{client_id}/contacts/{contact_id}` | `client.update` | Change / delete contact. |
| GET / POST | `/clients/{client_id}/services` | `client.read` / `client.update` | List / add engagement (client service). |
| PATCH / DELETE | `/clients/{client_id}/services/{engagement_id}` | `client.update` | Change (`fee`, `frequency`, `start_date`, `end_date`, `is_active`, `auto_create_jobs`, `job_lead_days`) / delete. |
| POST | `/clients/{client_id}/services/{engagement_id}/create-job` | `client.update` + `job.create` | Create the job of the current (else upcoming) period now. 201 `{job: JobRead, period_start, period_end}`; 409 `ENGAGEMENT_JOB_EXISTS` / `ENGAGEMENT_NOT_SCHEDULABLE`. |
| GET | `/clients/{client_id}/services/{engagement_id}/job-runs` | `client.read` | Jobs created for the engagement, newest first, up to 50: `[{period_start, period_end, status (created|failed), error, job: {id, number, name}|null, created_at}]` (`job` null once the job was deleted). |

## Shapes

**ClientRead** (every client response): all `clients` columns (`id, code, name, client_type,
status, email, phone, address_line1/2, city, postcode, country, company_number, utr,
vat_number, year_end_month/day, partner_user_id, manager_user_id, payment_terms_days,
default_vat_rate_id, billing_notes, notes, segment, aml_status, aml_checked_on, health_score,
accounting_software, created_at, updated_at`) plus `initials` (avatar: first letters of the
first two words, or the first two letters of a one-word name) and, from the Clients 360
release, the billing settings `invoice_email`, `default_service_id`, `invoice_template_id` and the
central link `central_client_id`, `central_sync_state`, `central_synced_at`,
`merged_into_client_id` (see "Clients 360").

**ClientListItem** (rows of `GET /clients`) = ClientRead plus:

```json
{
  "primary_contact": {"id": "…", "name": "Jane Doe", "email": "jane@acme.example", "phone": null},
  "contact_name": "Jane Doe",          // null → UI shows "+ Add"
  "contact_email": "jane@acme.example", // primary contact's email, else the client's email
  "deadlines": {"open_jobs": 5, "overdue": 2, "overdue_days": 3, "due_today": 1,
                "due_soon": 1, "due_soon_days": 14, "next_due_date": "2026-10-17",
                "status": "overdue"},   // overdue | due_today | due_soon | on_track | none
  "last_activity_at": "2026-10-17T09:12:00Z",
  "services": [{"service_id": "…", "code": "VAT", "name": "VAT return"}]
}
```

**ClientDetail** (`GET/POST/PATCH /clients/{id}`, archive) = ClientListItem plus
`contacts: ContactRead[]`, `engagements: ClientServiceRead[]`, `rates: ClientRateRead[] | null`,
`financials {wip_time_value, wip_disbursement_value, wip_total, unbilled_minutes,
outstanding_balance, overdue_balance, on_account}`,
`recurring_fees {annual_net, annual_vat, annual_gross, one_off_net}` and, from the Clients 360
release, `default_service: {service_id, code, name} | null`,
`invoice_template: {id, name} | null` and `merged_into: {id, code, name} | null`.

**ClientSummary**: `total` (live, not archived), `by_status` (all four, archived included),
`by_client_type`, `by_aml_status` (all five keys, `by_central` (`{linked, local}`: clients
enabled from the central client service and clients created here; archived ones left out)), `by_accounting_software`,
`by_segment: [{segment, count}]` (case-insensitive groups, null last),
`with_overdue_deadlines`, `users` (active staff).

**ImportResult**: `dry_run, total_rows, valid_rows, created, columns (mapped fields),
ignored_columns (headers as given), errors: [{row, field, code, message}],
clients: [{row, code, name, id}]` (`code` null in a dry run when it will be generated).

**DeleteOutcome** (settings deletes): `{outcome: deleted|archived|deactivated, id}`.

## List parameters

`q` (case-insensitive substring of name, code, company number, client email or any contact
email), `status` (repeatable; default every status except `archived`), `segment`
(repeatable, case-insensitive), `client_type`, `aml_status`, `accounting_software`
(repeatable), `se, `central` (`linked`: enabled from the central client service; `local`: created
in this practice)rvice_id` (active engagement of that service), `manager_user_id`,
`partner_user_id`; `sort` = `name` (default, case-insensitive) | `code` | `segment` |
`health_score` | `aml_status` | `created_at` | `last_activity_at`; `order` = `asc` | `desc`;
`limit` (1–100, default 25), `cursor`.

The cursor encodes the sort, the direction, the sort key and the id; a cursor used with a
different sort/order, or whose key does not have the sort key's type (text, a 0-100 /
-1 score, an aware timestamp), answers 422 (`field: cursor`). Null `segment` sorts as `""` and null
`health_score` as `-1` (first ascending, last descending). One page = one query for the
rows (with `last_activity_at` computed in SQL) + three batched lookups (primary contacts,
active engagements, deadline counts) regardless of page size (tested).

## Error codes

| Code | Status | When |
|---|---|---|
| `VAT_RATE_NAME_TAKEN` | 409 | Duplicate live VAT-rate name (case-insensitive). |
| `SERVICE_CODE_TAKEN` / `ACTIVITY_CODE_TAKEN` | 409 | Duplicate code. |
| `SERVICE_NAME_TAKEN` / `ACTIVITY_NAME_TAKEN` | 409 | Duplicate name (case-insensitive). |
| `RATE_IN_USE` | 409 | Deleting a used client or staff rate. |
| `SEQUENCE_BACKWARDS` | 409 | `next_number` lowered for `invoice` / `credit_note` / `ticket` / `estimate`; `details.current_next_number` is the live counter. |
| `SEQUENCE_NUMBER_TAKEN` | 409 | The resulting next invoice/credit-note/job number already exists (estimate numbers are not checked until the estimates are stored). |
| `SEQUENCE_INVALID` | 409 | Client numbering would produce an invalid client code (generation time). |
| `STAFF_RATE_EXISTS` | 409 | The person already has a rate starting that day. |
| `MEMBER_REMOVED` | 409 | Adding a rate for a removed member. |
| `CLIENT_RATE_EXISTS` | 409 | Second everyone-rate, or second rate for the same person, on a client. |
| `CLIENT_CODE_TAKEN` | 409 | Another live client uses the code. |
| `CLIENT_IN_USE` | 409 | Delete of a client with jobs/time/disbursements/invoices/credit notes/payments (`details` has counts; for a bulk delete `details` is a list with one entry per such client: `client_id`, `code`, `name`, `reason`, `usage` counts). |
| `CLIENT_INACTIVE` | 409 | Raised by `ensure_client_accepts_work` for inactive/archived clients. |
| `CLIENT_SERVICE_EXISTS` | 409 | The client already takes that service. |
| `CLIENT_FIELD_MANAGED_CENTRALLY` | 409 | Changing the name or address of a client linked to the central client service (`details`: one entry per field). |
| `CENTRAL_CLIENT_ALREADY_ENABLED` | 422 | An import row whose company number belongs to an enabled central client (a row error). |
| `CENTRAL_*`, `CLIENT_ALREADY_LINKED`, `CLIENT_NOT_LINKED`, `CLIENT_MERGED` | 409 / 503 | The central link endpoints: `docs/modules/central-clients.md`. |
| `NO_RECIPIENT` | 422 | Sending an invoice when no address is given and the client has none (`invoice_email`, billing contacts, primary contact, client email). |
| `IMPORT_INVALID` | 422 | Real import with row errors (`details` = the row errors); nothing written. |
| `IMPORT_MISSING_COLUMNS` / `IMPORT_DUPLICATE_COLUMNS` / `IMPORT_TOO_MANY_ROWS` | 422 | Header without `name`; two headers mapping to one field; > 5000 rows. |
| `FILE_INVALID` | 400 | Empty file, no data rows, undecodable text, unreadable CSV, no `file` part. |
| `FILE_TOO_LARGE` | 413 | Upload over 5 MB. |
| `FILE_TYPE_NOT_ALLOWED` | 415 | Body neither multipart nor CSV/text. |
| `VALIDATION_ERROR` | 422 | Field validation (incl. member/VAT-rate/service references that are not ours or not live). |

## For other modules

* `app.services.clients.ensure_client_accepts_work(session, organization_id, client_id) -> Client`
  — call inside your unit of work before recording a job, time entry or disbursement.
  `NotFoundError` for missing/deleted/other practice; `ConflictError(code="CLIENT_INACTIVE")`
  for `inactive`/`archived`. Takes `FOR KEY SHARE` on the client row, so a concurrent
  archive (status change) or delete waits for your commit.
* `app.services.rates.rate_in_force(session, organization_id, user_id, on_date)
  -> tuple[charge_rate, cost_rate] | None` — the person's own staff rate in force (latest
  `effective_from ≤ on_date`); None when the person has none. The full charge-out ladder is
  `app.services.charge_rates.resolve_many` (see `docs/modules/team-rates.md`).
* `app.services.rates.current_rates(session, organization_id, on_date)
  -> dict[user_id, (charge_rate, cost_rate)]` — batched version.
* `app.services.clients.generate_client_codes(session, organization_id, count, avoid)` —
  codes from the `client` sequence under its lock.

## Rule decisions beyond docs/domain.md

* **Settings deletes** never fail for "in use": VAT rates are archived, services and activity
  codes deactivated, and `data.outcome` says what happened. Archiving a rate also clears its
  default flag; an archived rate cannot be made default (422) unless restored in the same
  PATCH. `is_default=false` may leave the practice with no default (domain allows 0 or 1).
  Default changes lock all the practice's VAT rate rows in id order, then clear the old
  default with a fresh `UPDATE`, so concurrent changes always end with exactly one default.
* VAT-rate "in use" = referenced by a service, an activity code, a client default, a
  disbursement, an expense, an expense type's default rate, an invoice line or a credit-note
  line (the seeded expense types use the zero, exempt, outside-scope and standard rates, so those
  are archived, not deleted, when removed). Service "in use" = a job, an
  engagement, a client's default service, an invoice line or a rate rule. Activity code "in use" = a time entry, a timer or
  a service's activity list.
* **Sequences**: `client` and `job` may move backwards; generated client codes skip codes
  already in use (gaps are fine for clients). `invoice`, `credit_note`, `ticket` and `estimate`
  only move forward (409 `SEQUENCE_BACKWARDS`); the floor is the live counter, so numbers
  already taken raise it, and skipping ahead is allowed. Estimates default to prefix `EST`,
  padding 5 (`EST00001`) and, like the other kinds, are taken gaplessly under the sequence row
  lock (a rolled-back estimate gives its number back). The `client` prefix may only contain
  letters, digits, `_`, `-` and the formatted code must fit 20 characters (422); codes are
  upper-cased. A kind whose row is missing is shown with defaults and created on PATCH.
* **Archiving is `client.delete`'s job everywhere**: moving a client into or out of
  `archived` by PATCH, create or import needs `client.delete` in addition. Other
  transitions are free (`client.update`).
* **Delete** checks live jobs (soft-deleted jobs do not block), time entries, disbursements,
  invoices of any status (drafts included), credit notes and payments. Contacts, rates and
  engagements stay attached to the soft-deleted row; the code becomes reusable.
* **Contacts**: when `is_primary` is omitted, the client's first contact becomes primary.
  Deleting or un-flagging the primary leaves the client with none (no automatic promotion).
  Primary changes are serialised by a `FOR NO KEY UPDATE` lock on the client row.
* **Engagements**: only active services can be added; the service cannot be changed (delete
  and re-add). `effective_fee` = agreed fee, else the service's `default_fee`.
* **Recurring jobs**: an engagement with `auto_create_jobs: true` (default false; needs a
  `start_date`, 422 on `start_date` otherwise, also when a PATCH would clear it) has the daily
  worker `create_engagement_jobs` create the job of each period, `job_lead_days` (0-365,
  default 0) days before it starts. `ClientServiceRead` adds `auto_create_jobs`,
  `job_lead_days` and `next_job_period: {period_start, period_end, create_on} | null` (the
  period due now or the upcoming one; null when off, inactive, ended, or a one-off job was
  created). Periods, naming and failure handling are in `time.md` ("Recurring jobs from
  engagements"). Each created job is audited as `engagement.job_created` (target: the
  engagement; meta job id and number, period, `source` `engagement_schedule` or
  `engagement_manual`). Tables: `engagement_job_runs` (one row per engagement and period start).
* **Deadlines**: open jobs = `planned`, `in_progress`, `on_hold` and not deleted. "Due soon"
  = due after today and within 14 days. `overdue_days` is for the most overdue job.
* **last_activity_at** = greatest of the client's `updated_at` and the latest `updated_at` of
  its time entries, jobs and invoices.
* **Financial snapshot** (detail): WIP uses the domain WIP definition (billable, `unbilled`;
  time only on approved timesheets when the practice requires approval);
  `outstanding_balance` = Σ(gross − paid − credited) of issued invoices; `overdue_balance`
  the part with `due_date` < today (practice zone); `on_account` = Σ unallocated amounts of
  non-voided payments. WIP fields need `wip.read`, balances `invoice.read`, `on_account`
  `payment.read`; otherwise null.
* **Recurring fees** (detail): active engagements not ended before today, annualised
  (monthly ×12, quarterly ×4, annually ×1); VAT per engagement with `money.vat_on` at the
  service's rate, else the client's default, else the practice default; 0 when the practice
  is not VAT registered. One-off fees are reported separately, without VAT.
* **Client rates** of a soft-deleted client are unreachable (404); a removed member cannot
  get a new client rate (422).
* **Export**: text cells starting with `=`, `+`, `-`, `@`, tab or CR are prefixed with `'`
  (CSV injection). Columns: code, name, client_type, status, company_number, segment,
  health_score, contact_name, contact_email, email, phone, deadline_status, overdue_jobs,
  overdue_days, next_due_date, aml_status, accounting_software, services, last_activity_at,
  created_at.

## Import

`POST /imports/clients` takes `multipart/form-data` with a `file` part, or a raw `text/csv`
body (5 MB cap enforced by the route; the global body-size middleware exempts this path).
The multipart body is parsed with the standard library because `python-multipart` is not a
dependency. Text is UTF-8 (BOM allowed), falling back to Windows-1252.

Headers are normalised (case, spaces, punctuation) and mapped to the `POST /clients` fields
plus `year_end` (`DD/MM` or `DD/MM/YYYY`), `contact_name`, `contact_email`, `contact_phone`,
`contact_position` (creates the primary contact), `services` (codes separated by `;`, `,`
or `|`), `manager_email`, `partner_email`. Aliases include `client`/`client name`/
`company name` → name, `reg no`/`company number`/`crn` → company_number, `type` →
client_type, `contact` → contact_name, `aml` → aml_status, `health` → health_score,
`software` → accounting_software. Enum values are normalised (`Limited Company` →
`limited_company`, `Xero` → `xero`). Blank rows are skipped; row numbers are 1-based data rows.

Row checks: everything `POST /clients` validates, plus duplicate codes within the file,
codes already used (`CLIENT_CODE_TAKEN`), unknown/inactive service codes, emails that are
not active members, contact fields without a contact name, `status=archived` without
`client.delete`, and a company number (ignoring case, spaces and leading zeros) that belongs
to a client enabled from the central client service (its own number or its central
client's): row error `CENTRAL_CLIENT_ALREADY_ENABLED`, so an import never duplicates an
enabled central client (the same way codes are checked; a local client with the same number is
not an error: company numbers are not unique). A real run inserts all clients, contacts and engagements in one
transaction (codes for blank-code rows come from the client sequence, skipping codes in the
file) and writes one `client.imported` audit row (count + first 100 codes).

The CSV **export** and **import** are unchanged by the Clients 360 release: the export is not
column-complete (no address, tax references or billing settings), so the new fields were not
added to either.

## Bulk action

`POST /clients/bulk`, body `{client_ids, action, value?}`: one action on 1-200 clients, all or nothing in one transaction (the
clients are locked in id order; a rule that any client breaks stops the whole action). Permission `client.update`, or
`client.delete` for `delete`.

| Field | Type | Notes |
|---|---|---|
| `client_ids` | uuid[] 1-200 | Duplicates count once. Any id that is not a live client of the practice: 404 `RESOURCE_NOT_FOUND`, `details: {"client_ids": [...]}`, nothing changes. |
| `action` | `set_status`, `set_manager`, `set_partner`, `set_segment`, `delete` | |
| `value` | string or null | `set_status`: `prospect`, `active`, `inactive` or `archived` (required; `archived` is how clients are archived in bulk, so there is no separate `archive` action; moving a client into or out of `archived` also needs `client.delete`, 403 otherwise). `set_manager` / `set_partner`: a staff user id (an active member, 422 otherwise), null clears. `set_segment`: up to 40 characters; null or blank clears. `delete`: no value (a value is 422). |

`delete` follows `DELETE /clients/{id}`: a client with jobs, time, disbursements, expenses, invoices, credit notes or payments makes
the whole call answer 409 `CLIENT_IN_USE` (nothing is deleted; archive those instead). The name and address of a centrally
managed client are not touched by any of these actions. Response `{"action", "affected", "changed"}` (a client already holding the
wanted value counts in `affected` only). Each client that changed gets the audit row of the single change (`client.updated`,
`client.archived` when the new status is `archived`, `client.deleted`) with `bulk: true`.

## Audit actions

`vat_rate.created|updated|archived|deleted`, `service.created|updated|deactivated|deleted`,
`activity_code.created|updated|deactivated|deleted`, `sequence.updated`,
`staff_rate.created|deleted`, `client_rate.created|updated|deleted`,
`client.created|updated|archived|deleted|imported`,
`client_contact.created|updated|deleted`, `client_service.created|updated|deleted`, `engagement.job_created` — each
with a `meta` naming the record (code/name, amounts, `changes: {field: [old, new]}`).
From the Clients 360 release also `client.central_enabled`, `client.central_linked`,
`client.central_unlinked`, `client.central_synced`, `client.central_renamed`,
`client.central_archived`, `client.central_restored`, `client.central_merged`
(`docs/modules/central-clients.md`) and
`invoice_template.created|updated|deactivated|activated|made_default|deleted`
(`docs/modules/invoice-templates.md`). The audit entries of payments, credit notes,
disbursements and WIP write-offs now carry `client_id` in their metadata, like the other billing
entries, so they show in the client's activity.
Leads write `lead.created|updated|archived|unarchived|converted` (see "Leads").

## Clients 360

### Billing settings

Three optional fields on `POST /clients`, `PATCH /clients/{id}` and every client response:

| Field | Meaning |
|---|---|
| `invoice_email` | Where invoices are emailed: a valid address, up to 254 characters (422 on `invoice_email` otherwise; null or blank clears it). It comes **first**: the invoice recipients are the request's `to`, else `invoice_email`, else the billing contacts, else the primary contact, else the client's email. |
| `default_service_id` | The service new jobs and invoices of the client suggest: an active service of the practice. |
| `invoice_template_id` | The client's invoice template (`docs/modules/invoice-templates.md`): an active template of the practice; null means the practice default. |

Another practice's service or template (or an unknown id) answers **404** with
`details: {field}`; an inactive one **422** on the field. A value that is not changing is not
checked again. Changes are in the `client.updated` audit entry's `changes`. `ClientDetail` also
returns `default_service` and `invoice_template` (id and name). A service that is a client's
default service counts as in use for `DELETE /services/{id}` (it is deactivated instead).

### Linked clients

See `docs/modules/central-clients.md`. In short: `central_client_id`, `central_sync_state`
(`local`, `synced`, `stale`, `failed`), `central_synced_at` and `merged_into_client_id` on every
client; the name and address of a linked client cannot be edited (409
`CLIENT_FIELD_MANAGED_CENTRALLY`); the list filters by `central=linked|local`.

### Overview: `GET /clients/{client_id}/overview`

`client.read`. The aggregated figures behind the Client 360 tabs in **one call** (a fixed
number of queries, however much data the client has); the tab lists themselves reuse the
existing endpoints (`/jobs`, `/time-entries`, `/disbursements`, `/clients/{id}/wip`, `/invoices`,
`/payments`, `/clients/{id}/statement`, filtered by `client_id`). Money is pence, minutes whole
minutes, dates ISO in the practice time zone. **A section the caller may not see is `null`**
(never a 403 for the whole call); another practice's client is 404.

```json
{
  "client_id": "…", "as_of": "2026-10-18", "ytd_from": "2026-01-01", "last_30d_from": "2026-09-19",
  "jobs": {"total": 6, "by_status": {"not_started": 1, "in_progress": 2, "on_hold": 1, "completed": 1, "closed": 1}},
  "time": {"scope": "all", "minutes_30d": 135, "billable_minutes_30d": 90,
           "minutes_ytd": 400, "billable_minutes_ytd": 355, "last_entry_on": "2026-10-15"},
  "expenses": {"scope": "all", "count": 4, "net": 5500, "gross": 6400, "awaiting_approval": 0},
  "wip": {"time": 16000, "fixed_fee": 6000, "expense": 4800, "total": 26800,
          "ageing": {"0_30": 14300, "31_60": 6000, "61_90": 2500, "90_plus": 4000}, "item_count": 6},
  "invoices": {"count": 7,
               "by_status": {"draft": 1, "issued": 1, "overdue": 2, "part_paid": 1, "paid": 1, "void": 1},
               "billed_ytd": 25000, "outstanding": 28600, "overdue": 15600, "last_invoice_on": "2026-10-13"},
  "payments": {"received_ytd": 7400, "count_ytd": 2, "last_payment_on": "2026-10-10"},
  "recent_activity": [{"id": "…", "action": "payment.recorded", "label": "Payment recorded",
                       "at": "2026-10-17T09:12:00Z", "actor_user_id": "…", "actor_name": "Ada Lovelace",
                       "target_type": "payment", "target_id": "…"}],
  "tab_counts": {"jobs": 6, "time_entries": 5, "expenses": 4, "wip": 6, "invoices": 7, "payments": 4, "contacts": 2}
}
```

| Section | Needs | Rules |
|---|---|---|
| `jobs` | `job.read` | Jobs not deleted, by status; `by_status` always has all five keys: a `planned` job counts as `not_started`, a `cancelled` one as `closed`. |
| `time` | `time.read_all` (every person's time, `scope: "all"`) or `time.log` (the caller's own, `scope: "own"`) | `minutes_30d`: the 30 days up to and including today; `minutes_ytd`: since 1 January; the billable figures count billable entries; `last_entry_on`: the latest entry of any date. |
| `expenses` | `wip.read` (all), else `disbursement.log` / `disbursement.manage` (the caller's own, `scope: "own"`) | Every disbursement of the client, billed or not; `gross` adds VAT at the disbursement's own rate, else the client's default, else the practice's (none when the practice is not VAT registered), half-up per disbursement. `awaiting_approval` is always 0: disbursements have no approval step yet. |
| `wip` | `wip.read` | The WIP module's own query, so the figures equal the WIP screens (`GET /wip`, `GET /clients/{id}/wip`): `time` (unbilled billable time) + `fixed_fee` (the part on fixed-fee jobs) + `expense` = `total`; `ageing` by item date; `item_count` = time entries + disbursements in WIP. |
| `invoices` | `invoice.read` | `by_status` = the display statuses of the invoice list (all six keys); `billed_ytd` = net of the invoices issued since 1 January (void excluded); `outstanding` and `overdue` = the client detail's balances (`overdue`: past `due_date`); `last_invoice_on` = latest issue date. |
| `payments` | `payment.read` | `received_ytd` / `count_ytd`: non-void payments received since 1 January; `last_payment_on`: latest `received_on` of a non-void payment. |
| `recent_activity` | `client.read` | The last 10 audit entries about the client (and clients merged into it): entries that target the client or name it (`client_id`) in their metadata, newest first, limited to the kinds the caller may see (`client.*`, contacts and engagements always; `job.*` needs `job.read`; `time_entry.*` `time.read_all`; `disbursement.*` and `wip.*` `wip.read`; `invoice.*` and `credit_note.*` `invoice.read`; `payment.*` `payment.read`; rates `rate.read`). `actor_user_id` is null for central events. |
| `tab_counts` | per section | `jobs`, `time_entries` (those the caller may see), `expenses`, `wip` (items), `invoices`, `payments` (all, void included) are null exactly when their section is; `contacts` needs only `client.read`. |

Cost values and margins are **never** part of the overview, so `rate.read` changes nothing here.
`ytd_from` is 1 January of the current year in the practice time zone; `last_30d_from` is
today minus 29 days.

## Leads

Ticket APT49, migration `063`. Code: `app/api/v1/leads.py` → `app/services/leads.py` →
`app/repositories/leads.py`; schemas in `app/schemas/leads.py`; model `Lead` in
`app/db/models/clients.py` (table `leads`). Tests: `tests/test_leads.py`.

A lead is a prospective client and what winning it cost: `acquisition_cost` and
`onboarding_cost` (integer pence, 0 and up). One click converts it into a client; a lead the
practice no longer pursues is archived (and can be unarchived).

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/leads?status=&q=` | `client.read` | One status (`open` default, `converted`, `archived`), newest first; `q` searches name, contact name and email. Keyset-paginated (`limit`, `cursor`). |
| GET | `/leads/summary` | `client.read` | Per status: `{count, acquisition_cost, onboarding_cost, total_cost}`. Declared before `/leads/{lead_id}`. |
| POST | `/leads` | `client.create` | Add (`name` 1-200; `client_type` as clients, default `limited_company`; `contact_name` ≤ 120, `email`, `phone` ≤ 40, `source` ≤ 100, `notes` ≤ 5000; the two costs). 201. |
| GET | `/leads/{lead_id}` | `client.read` | One lead. |
| PATCH | `/leads/{lead_id}` | `client.update` | Change any field; text fields accept null. The costs stay editable after conversion. |
| POST | `/leads/{lead_id}/archive` · `/unarchive` | `client.delete` | Set / clear `archived_at`. Doing it twice changes nothing. |
| POST | `/leads/{lead_id}/convert` | `client.create` | Body `{code?}` (optional; may be empty). Creates the client and links it. |

**LeadRead**: the columns (`id, name, client_type, contact_name, email, phone, source, notes,
acquisition_cost, onboarding_cost, archived_at, converted_client_id, converted_at, created_by,
created_at, updated_at`) plus `total_cost` (the sum of the two costs), `status` (derived:
`archived` if archived, else `converted` if converted, else `open`) and
`converted_client: {id, code, name} | null` (null until converted, and once that client is
deleted; `converted_client_id` stays).

Rules:

* **Converting** runs `ClientsService.create` inside the same transaction, so the client gets the
  same validation, code generation and `client.created` entry as `POST /clients`. The client is
  `active` and takes the lead's `name`, `client_type`, `email`, `phone` and `notes`; with a
  `contact_name` it also gets a primary contact (`name`, `email`, `phone`). `code` defaults to the
  client sequence; a taken code is 409 `CLIENT_CODE_TAKEN` and the lead stays open.
* A lead converts once: 409 `LEAD_ALREADY_CONVERTED` (`details.client_id`). The lead row is
  locked (`FOR UPDATE`) while converting, so two clicks at once make one client; a partial unique
  index on `(organization_id, converted_client_id)` also keeps one lead per client.
* An archived lead does not convert: 409 `LEAD_ARCHIVED` (unarchive it first). A converted lead
  can be archived; archiving never touches the client.
* Deleting the client later leaves the lead `converted` with `converted_client: null`.
* Audit (`target_type: "lead"`, readable in `GET /audit-logs/history` with `client.read`):
  `lead.created` (`name` and the costs), `lead.updated` (`changes`, only when something
  changed), `lead.archived`, `lead.unarchived`, `lead.converted` (`name`, `client_id`,
  `client_code`; the `client_id` puts it in the client's activity too). As for every entry,
  keys ending in `cost` are shown as null in the trail to people without `rate.read`.
