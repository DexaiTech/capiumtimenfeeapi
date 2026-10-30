# Domain rules

The business rules of Capium Time & Fees. The schema is in `app/db/models/` (one file per
area) and the migrations `003`–`005` and `010`–`020` (staff, rates, services, notifications, tickets, central clients, invoice templates, practice settings, period locks); this document is
the contract every module follows.
When code and this document disagree, one of them is a bug.

## Conventions

| Thing | Representation |
|---|---|
| Money | `int` minor units (pence) in the practice currency. Never floats. Arithmetic in `app/utils/money.py`, rounding half up. |
| Rates | Minor units per hour (`hourly_rate`, `charge_rate`, `cost_rate`). |
| VAT rate | Basis points: `2000` = 20.00%. Snapshotted onto every invoice/credit-note line (`vat_rate_bp`). |
| Percentages in responses | Basis points (`recovery_bp`, `utilisation_bp`), `null` when the denominator is zero. |
| Durations | Whole minutes. |
| Dates | ISO dates (`YYYY-MM-DD`) in the practice's time zone; timestamps are UTC. |
| Numbers | Clients `C0001`, jobs `J00001`, invoices `INV-00001`, credit notes `CN-00001`, staff `S001`, tickets `APT1`, estimates `EST00001`, from `app/services/sequences.py` (gapless, locked per practice). |
| Tenancy | Every route is under `/api/v1/organizations/{organization_id}/…`; every query is filtered by `organization_id`; composite foreign keys make cross-practice references impossible. Another practice's id answers 404, never 403. |
| Envelope | `{success, data, message, meta}`; lists are cursor-paginated (`limit`, `cursor`, `meta.next_cursor`) unless documented as bounded. |
| Audit | Every create/update/delete of a financial record writes `audit.record(...)` in the same transaction, with `target_type`, `target_id` and a `meta` that names the record (number, client name, amounts). |

## Roles

System roles are copied into each practice (`app/core/permissions.py`): Owner, Admin,
Partner, Manager, Staff, Billing, Viewer. Routes check permissions, never role names. Every
permission code, with its group and description and the system roles that hold it, is listed in
`docs/modules/permissions.md`. The permission codes per module:

| Module | Read | Write |
|---|---|---|
| Settings (VAT rates, services, activity types and their links, numbering) | any member | `settings.manage` (numbering: `organization.update`) |
| Staff | `member.read` | create, invite, link a login: `member.invite`; edit, reactivate: `member.update`; deactivate, delete, unlink a login: `member.remove` |
| Rates, grades, re-rating | `rate.read` (grades: `member.read`) | `rate.manage` |
| Clients, contacts | `client.read` | `client.create`, `client.update`, `client.delete` |
| Central clients (search, enable, link, unlink) | — | `client.create`; synchronise: `client.update`; central events, the change feed, the sync log and the mock's switches: `settings.manage` |
| Client overview (Client 360) | `client.read`; each section needs the permission of its module (`job.read`, `time.read_all` or `time.log`, `wip.read`, `invoice.read`, `payment.read`) and is `null` without it | — |
| Invoice templates | any member (inactive ones: `settings.manage`) | `settings.manage` |
| Jobs | `job.read` | `job.create`, `job.update`, `job.delete` |
| Time | own time always; others' with `time.read_all` | own with `time.log`; others' with `time.manage_all` |
| Timesheets | own; others' with `time.read_all` or as their manager | submit own; approve with `timesheet.approve` |
| Disbursements | `wip.read` or own | `disbursement.log` (own), `disbursement.manage` (any) |
| WIP | `wip.read` | write-off: `wip.write_off` |
| Invoices, credit notes | `invoice.read` | draft: `invoice.create`; issue/void/send: `invoice.issue`; credit note: `credit_note.create` |
| Payments | `payment.read` | `payment.record` |
| Reports | `report.read` (time, jobs, utilisation) | `report.financial` (WIP, debtors, billing, recoverability, profitability) |
| Period lock | `period_lock.read` (Owner, Admin, Partner, Manager, Billing) | lock: `period_lock.manage` (Owner, Admin, Partner); reopen: `period_lock.reopen` (Owner, Admin) |
| Notifications | own notifications and own settings: every member | the practice's switches and defaults, the email templates (read and write): `organization.update` or `settings.manage` |
| Tickets (Application Testing) | `ticket.read` (every role, Viewer included) | raise: `ticket.create`; edit (status, priority, tags, assignee, text, comments, files, tags of the practice): `ticket.update`; delete tags, moderate comments and files: `ticket.delete` (Owner, Admin, Partner, Manager) |

A person without `rate.read` never sees `cost_rate`, `cost_value` or margin figures in any
response (they are `null`); the same goes for a staff member's rates (`GET /staff`) and a
grade's default cost rate.

## Practice settings

* A new practice is seeded (`app/services/practice_defaults.py`) with the UK VAT rates
  (Standard 20% default, Reduced 5%, Zero, Exempt, Outside scope), activity codes (seven
  chargeable, four non-chargeable), eight common services, the staff grades (Partner,
  Manager, Senior, Junior, without default rates), eight expense types, the seven number
  sequences (client, job, invoice, credit note, staff, ticket, estimate), the seven
  default ticket tags (Bug, Enhancement, UI, Reporting, Accounts, Validation, Trial Balance),
  the default invoice template (Standard) and an empty period lock.
* VAT rates are archived, never deleted, once used. Exactly zero or one default.
* Services and activity types have unique names and codes (ignoring case, inactive ones
  included). Once used they are deactivated (`is_active=false`), never deleted: a service is
  used by a job, a client engagement, a client's default service, an invoice line or a rate
  rule; an activity type by
  time, a timer or a service's activity list; a VAT rate by an activity type counts as used.
* A service carries `default_billable` (false: jobs created for it without a billing basis
  start `non_billable`), a VAT rate and an invoice description; an activity type carries
  `is_chargeable` (the default billable status of its time), a VAT rate and an invoice
  description.
* A service may list the activity types its jobs' time can use (`service_activity_codes`);
  an empty list means every active type. Time or a timer outside the list is refused
  (422 `activity_not_allowed`); time already recorded keeps its type.
* `time_increment_minutes` rounds logged minutes **up** to the increment (6 = tenths). A new
  practice starts with 6 (a practice that already existed keeps its own value).
* Numbering: the prefix, padding and next number of each sequence can be changed by
  `organization.update`; `next_number` may only move forward for `invoice`,
  `credit_note`, `ticket` and `estimate` (never re-use a number). Estimates are numbered
  `EST00001` (prefix `EST`, padding 5) from the `estimate` sequence, gaplessly under the
  sequence row lock, like the other kinds.
* Settings the expense, WIP, invoice-approval and rate modules read (`PATCH
  /organizations/{id}`, `organization.update`; every member can read them; bounds and
  defaults in `docs/modules/foundation.md`), money in pence: `receipt_required_threshold`
  (default 2 500: an expense above it, gross, must carry a receipt),
  `wip_adjustment_approval_threshold` (50 000: a WIP write-on or write-off above it needs
  approval; 0: every adjustment does), `invoice_approval_required` (false) with
  `invoice_approval_threshold` (0: with approval on, an invoice above it, gross, must be
  approved before it is issued; 0: every invoice), `mileage_rate` (45 pence a mile, 0–1000)
  and `expense_markup_bp` (0, basis points added when an expense is recharged, 0–100 000).
  The three thresholds run 0–100 000 000.
* `rate_mode`: `staff_first` (default) or `grade_only`. See Rates.
* `fixed_fee_wip_mode` decides **when the unbilled part of a fixed fee counts as WIP** (hourly
  and non-billable jobs are not affected). This is the contract the WIP module implements:
  * `on_completion` (default): the unbilled fixed fee counts as WIP **once the job is
    completed or closed**; until then the fixed fee is not WIP.
  * `by_budget_progress`: the fixed fee counts in step with the budget used:
    **fixed fee × min(1, logged minutes ÷ budget minutes) minus what is already billed** on
    the job (never below 0; a job that has used more than its budget minutes counts the whole
    fee).

## Period lock

(API, shapes, error codes and call sites: `docs/modules/period-locks.md`.)

* A practice has **one lock**: `locked_through`, the last locked day (inclusive; null = nothing
  locked). Every day on or before it is closed for **time** (entries, timer stops, corrections),
  **expenses** (disbursements and expense claim lines) and **WIP** (write-offs and their
  reversal, dated by `written_off_on`). Later days are open. It applies to everybody, the Owner
  included.
* **Locking** (`period_lock.manage`) only moves the lock forward and not past today in the
  practice's time zone (422 `PERIOD_LOCK_NOT_FORWARD`, `PERIOD_LOCK_IN_FUTURE`).
  **Reopening** (`period_lock.reopen`: Owner and Admin) moves it back to an earlier date, or to
  none, and needs a reason (422 `PERIOD_LOCK_NOT_BACKWARD`, `PERIOD_LOCK_REASON_REQUIRED`).
  Every change is a row of the append-only history (`period_lock_events`; a database trigger
  rejects any update or delete) and an audit entry (`period_lock.locked`,
  `period_lock.reopened`).
* **Enforcement** is `app/services/period_locks.py`. A service that creates, edits or deletes
  dated time, expense or WIP data calls it **inside its unit of work, before it writes**:

  ```python
  await period_locks.assert_open(session, organization_id, on_date, area)  # one date
  await period_locks.assert_open_many(session, organization_id, days, area)  # a collection
  ```

  `area` is `"time"`, `"expense"` or `"wip"`. A day is locked when `on_date <= locked_through`;
  the call raises `PeriodLockedError`, which is 409 `PERIOD_LOCKED` with
  `details {date, locked_through, area}` (ISO dates; `date` is the earliest refused day) and a
  message in the UK date format. A change that **moves** a record is checked at its old and at
  its new date. A refused write leaves nothing behind. `locked_dates(session, organization_id,
  days)` returns `{day: "Period locked until 31/03/2026"}` for the days that are locked and is
  how re-rating skips locked time. The functions share-lock the practice's lock row until the
  transaction ends, so a lock that is being set waits for the writes already checked and a write
  that starts meanwhile waits and is refused.
* **Expense-claim and WIP-adjustment code written by other modules calls these same functions**
  (area `expense` with the line's `incurred_on`; area `wip` with the adjustment's date) before
  it writes, and gets the 409 for free. Not blocked: reads, timesheet decisions (submit, recall,
  approve, reject, reopen change the sheet, not the time), timer start, pause, resume and
  discard, invoices, credit notes and payments (billing a closed period is the point of closing
  it), and records without a date of work.

## Staff and grades

(API: `docs/modules/team-rates.md`.)

* A staff member is a practice member with a **code** (unique per practice, from the `staff`
  sequence unless given), a **grade**, an **approver**, working minutes per day and working
  days (weekly capacity = minutes × days), a chargeable target (basis points; the grade's, else
  75%), a start and a leaving date and a **status**. The staff id is the user id.
* A login is optional. A person without one is a placeholder: time, rates and jobs refer to
  them and managers record time for them, but they cannot sign in, approve or submit. A login is
  added by invitation (emailed link, 7 days), by creating an account (temporary password shown
  once) or by merging the record into someone who already signs in; it is removed again by
  unlinking. Everything recorded stays with the person.
* Status: `active`, `inactive` (paused; can return) and `left` (no longer works for the
  practice; the leaving date is set and the people they approved lose their approver). The V1
  names `suspended` and `removed` are the same states. Only `active` staff not past their
  leaving date can have time recorded; nothing can be logged after the leaving date (422
  `after_leaving_date`).
* Codes are unique per practice (409 `STAFF_CODE_TAKEN`); no two **active** staff share an
  email address, whether work or sign-in (409 `STAFF_EMAIL_TAKEN`); initials are unique among
  non-removed members.
* Staff with time, expenses, invoices, jobs, rates or other records cannot be deleted
  (409 `STAFF_IN_USE`): deactivate them. The practice always keeps one active owner who can
  sign in; only an owner changes another owner; nobody deactivates or deletes themselves.
* A **grade** (Partner, Manager, Senior, Junior seeded) names a level with a default cost rate,
  default charge-out rate and default chargeable target. A used grade (staff of any status,
  or a rate naming it) is deactivated, never deleted. Names and codes are unique.

## Rates

Rates are dated **rules** (`rate_rules`): a kind (`charge` or `cost`), a scope, pence per hour
and a period `effective_from`–`effective_to` (inclusive; open-ended when null). The scope
decides the level. The charge-out rate of a time entry is the first that exists of:

1. client + service + staff (the job's service, the person),
2. client + service,
3. client + staff, else the client's rate for everyone,
4. `jobs.hourly_rate` of the entry's job (undated),
5. service + grade (the person's current grade),
6. staff,
7. grade: a rule in force, else the grade's default charge-out rate,

then `organizations.default_charge_rate`. A rule counts only on the days inside its period.
The cost rate is the person's staff cost rule, else the grade's cost rule, else the grade's
default cost rate, else (`staff_first`) the person's first staff cost rule when the day is
before it starts, else 0 (cost rules exist only for staff and grades). The cost never depends
on the client, the job or the job's billing basis: a cost rate added "from today" also costs
the time already recorded for earlier days.

* **Rate mode** (`organizations.rate_mode`): `staff_first` (default) uses the ladder above.
  `grade_only` **skips level 6**: a person's own `staff` rate never prices time and their
  grade's rate (level 7) does; levels 1–5, the practice default and the cost rate are
  unchanged. The `staff` rules are kept and apply again if the mode is switched back. One
  resolver (`app/services/charge_rates.py`) serves saving time, the preview
  (`GET /rates/resolve`, where the level 6 step is `applicable: false` with the reason) and
  re-rating, so a preview always equals what saving or re-rating will do. Switching the mode
  never rewrites recorded time.

* The rate, its source (`rate_source`: the level that priced it) and the value are
  snapshotted on the entry when it is saved. A rate change never rewrites recorded time;
  an edit of an unbilled entry re-takes its rate from the ladder again, and unbilled time is
  moved to new rates only by re-rating. The one exception is a **missing cost**: when a cost
  rate appears for a person (their own cost rule, their grade's cost rule or default cost rate,
  or a move to a grade that has one), their unbilled, unlocked time recorded with a cost of 0
  takes its cost from the ladder; the charge-out side is untouched (audit
  `time_entries.costed`, `app/services/time_costs.py`).
* Rules of one kind and scope never overlap (409 `RATE_OVERLAP`; an exclusion constraint makes
  it hold under concurrency). `close_previous` ends the earlier open-ended rule the day before.
* A rule is **used** when any time entry falls inside its scope and period. A used rule cannot be
  deleted (409 `RATE_IN_USE`), but its amount, start day and end day can change.
* Every change to a rule or to a grade's default rate is appended to the rate history
  (`rate_changes`) and to the audit log.
* **Re-rating** re-takes the rates of **unbilled** time for chosen clients, jobs, staff and/or a
  date range, in two steps: a preview (nothing changes; old and new rate and value per entry,
  totals, what is left alone, a short-lived token) and an apply that must match the preview.
  Billed, in-draft, written-off and period-locked time never changes. Each apply is a recorded
  run with before and after per entry. `POST …/rates/revalue` (V1) re-prices a date range
  directly and is recorded as a run too.
* Reading rates and costs needs `rate.read`, changing them `rate.manage`.
* V1 `staff_rates` and `client_rates` are views of the rules: a staff rate is a `staff` charge
  rule plus the cost rule that starts the same day, a client rate a `client_staff` or `client`
  rule.

## Clients

* `code` is unique per practice among live clients; generated from the `client` sequence
  when not given, upper-cased, `^[A-Z0-9_-]{1,20}$`.
* Status: `prospect`, `active`, `inactive`, `archived` (any transition is allowed).
  New jobs, time and disbursements are accepted for `prospect` and `active` clients only;
  `inactive` and `archived` answer 409 `CLIENT_INACTIVE`. Billing existing WIP, credit
  notes and payments stay possible for every status. Archived clients are hidden from
  pickers by default (`?status=` filter).
* Soft delete (`deleted_at`) only when the client has no jobs, time, disbursements,
  invoices or payments; otherwise archive. 409 `CLIENT_IN_USE`.
* One primary contact per client; any number of billing contacts. Invoices are addressed
  to the client's `invoice_email`, else the billing contacts, else the primary contact,
  else the client email (422 `NO_RECIPIENT` when there is none); addresses given in the
  request always win.
* **Billing settings** (all optional): `invoice_email` (a valid address, up to 254
  characters), `default_service_id` (an active service of the practice) and
  `invoice_template_id` (an active invoice template of the practice; none means the
  practice default). Another practice's id answers 404, an inactive one 422; a value that
  is not changed is not checked again, so a service that is deactivated later does not
  block other edits. A service that is some client's default service counts as used (it is
  deactivated, never deleted).

### Central clients

(API and the gateway interface: `docs/modules/central-clients.md`.)

* The **central client service** is the Capium group's master list of clients. It owns a
  client's identity: name, address and company number, and what happens to it (renamed,
  archived, restored, merged into another). Time & Fees reaches it only through the
  `CentralClientGateway` interface (`CENTRAL_CLIENT_GATEWAY`, `mock` for now).
* A client is **linked** when `central_client_id` is set; `central_sync_state` then says
  how fresh its centrally managed fields are: `synced`, `stale` (the service was unavailable:
  the stored values were kept) or `failed` (the service no longer knows the client). A client
  created here is `local`. A central client is linked to **at most one live client per
  practice** (a unique index), and several practices may each link the same central client.
* **Enabling** a central client creates the practice's client (code from the `client`
  sequence, name, address and company number from the central service, status `active`),
  linked. It never creates a duplicate: a central client that is already enabled answers with
  the existing client, and a client that is not linked and has the same company number
  (compared ignoring case, spaces and leading zeros) is **adopted**: it is linked and takes
  the central name and address, everything else stays. Two requests enabling the same central
  client at once create one client. The client import refuses a row whose company number
  belongs to an enabled central client.
* The **name, address lines, city, postcode and country** of a linked client are managed
  centrally: changing them on the client answers 409 `CLIENT_FIELD_MANAGED_CENTRALLY`
  (saying the same value is allowed); every other field stays editable. Unlinking makes them
  editable again.
* **While the central service is down** searching and enabling work from the practice's
  cache (filled by every live answer), a synchronisation keeps the stored values and marks the
  client `stale`, and every such attempt is logged; none of them answers with a server error.
  With nothing cached the answer is 503 `CENTRAL_UNAVAILABLE`.
* **Events** (`renamed`, `archived`, `restored`, `merged`) arrive one at a time or are pulled
  from the change feed and go through one handler that runs as the system (no actor in the
  audit entry). An event id is processed once: a repeat is logged as skipped and changes
  nothing. An event for a central client that is not enabled here is skipped, never an error.
  `renamed` updates name and address, `archived` / `restored` set the client `archived` /
  `active`.
* **Merge** (`merged`: client A went into client B centrally). If both are enabled here, A's
  work moves to B in one transaction: jobs, time entries, timers, disbursements, WIP
  write-offs, invoices, credit notes, payments and their allocations, contacts, services and
  rate rules. A is archived and keeps `merged_into_client_id`; it is never deleted. Where both
  have the same thing, B's wins: a service B takes already, a rate rule overlapping one of B's
  with the same kind and scope, and a second primary contact (it moves as an ordinary
  contact); the left-behind rows stay on the archived A and are listed in the audit entry. If
  only A is enabled here it is re-linked to B's central id; otherwise the event is skipped.
  A test discovers every table that refers to a client from the model metadata and fails when
  a later module adds one the merge does not handle.
* Every enable, link, unlink, synchronisation, event and feed poll writes an **integration
  sync log** row (`integration_sync_logs`: integration, operation, status `ok`, `failed` or
  `skipped`, attempt count, details) and every change to a client an audit entry
  (`client.central_enabled`, `central_linked`, `central_unlinked`, `central_synced`,
  `central_renamed`, `central_archived`, `central_restored`, `central_merged`).

### Invoice templates

(API: `docs/modules/invoice-templates.md`.)

* A template names a `layout` (`standard`, `detailed`, `summary`), whether the logo and the bank
  details are printed and a footer text. Every practice has exactly one **default** template
  (`Standard` to begin with: created with every practice, and by migration `017` for existing
  ones); a client uses its own template or, when it has none, the default.
* Names are unique among live templates, ignoring case. The default cannot be deleted or
  deactivated (409 `TEMPLATE_IS_DEFAULT`): make another one the default first. A template a
  client uses cannot be deleted (409 `TEMPLATE_IN_USE`): deactivate it; clients keep it. Only
  an active template can be the default or be chosen for a client.
* The PDF is still drawn with the practice letterhead and footer: reading the template there
  is the billing module's job.

### Client overview

`GET /clients/{id}/overview` returns the figures behind the Client 360 tabs in one call. Each
section is computed by the same rules as the screen it summarises (the WIP section is the
WIP module's own query, so its figures equal the WIP screens) and is `null` when the caller
may not see that module; money is pence and the year starts on 1 January in the practice
time zone. Cost values and margins are never part of it. `recent_activity` is the last ten
audit entries about the client (and clients merged into it) whose kind the caller may see.

## Jobs

* `number` from the `job` sequence. A job belongs to one client, optionally one service.
* Billing basis: `hourly` (bill time at value), `fixed` (bill `fixed_fee`; time is still
  recorded and its WIP is cleared by the fixed-fee bill), `non_billable` (time is
  recorded as non-billable; nothing to bill).
* Defaults from the service: `billing_basis`, `fixed_fee = default_fee`; a service that is not
  billable by default (`default_billable=false`) gives a job created without a billing basis the
  basis `non_billable`.
* Status: `planned → in_progress ↔ on_hold`; `in_progress → completed → closed`; `cancelled` from any
  open state. Time cannot be logged on `completed`, `closed` or `cancelled` jobs (409 `JOB_CLOSED`);
  reopening a completed or closed job (back to `in_progress`) is allowed with `job.update`
  (`app/services/time_jobs.py`).
* Budget: `budget_minutes`, `budget_fee`; job responses carry actuals: `logged_minutes`,
  `wip_value` (unbilled billable charge value), `billed_value`, `written_off_value`.
* Delete only when no time, disbursements or invoice lines reference the job.

## Time

* An entry: person, date, minutes (1–1440, rounded up to the practice increment), client and
  job (optional; a job implies its client), activity code (optional), description,
  `is_billable`.
* `is_billable` defaults to: false for `non_billable` jobs, for non-chargeable activity
  codes and for entries with no client; true otherwise. A billable entry needs a client.
* A service may limit the activity codes of its jobs' time (see Practice settings): another
  code is 422 `activity_not_allowed`, on entries, bulk entries and the timer alike. Time
  cannot be logged after the person's leaving date (422 `after_leaving_date`) and only for
  `active` staff.
* The entry's rate, value and `rate_source` come from the rate ladder (see Rates).
* Every entry belongs to the timesheet of its person and week (`week_start` = the practice
  `week_start` day on or before the date); the timesheet is created on demand.
* Editable by its owner (or `time.manage_all`) only while the timesheet is `open` or
  `rejected` **and** the entry is `unbilled`. Otherwise 409 `TIME_LOCKED`.
* A person logs at most 24 hours per day across entries (422).
* The **period lock** closes the entry's date (see Period lock): creating (bulk entries and a
  timer stop included), editing (the old and the new date), deleting and correcting time dated
  on or before the lock are 409 `PERIOD_LOCKED` (area `time`). Timesheet decisions still work in
  a locked week.
* Timer: one per person; start (optionally from a job), pause, resume, stop. Stop writes an
  entry for the elapsed time (rounded up to the increment, minimum one increment) on the
  start date and deletes the timer. Discard deletes it without an entry.
* The time increment rounds up every way of recording time alike: a new entry, a bulk entry, an
  edit, a timer stop, a copy and a correction.
* `internal_note` (up to 2000 characters, optional) is a remark that stays inside the practice:
  only the entry's owner and people with `time.read_all` or `time.manage_all` see it (everybody
  else, a line manager without those permissions included, gets `null`), and the audit trail
  records that a note changed, never its text. `service_id` / `service_name` are read-only: the
  service of the entry's job.
* Targets: a person's weekly capacity (`weekly_capacity_minutes`) is spread over their working
  weekdays (the remainder minutes go to the first days, so the days add up to the capacity). A
  day off, and any day before the start date or after the leaving date, has a target of 0.
  The week grid, the day view and the calendar show the target, the variance (logged minus target)
  and the **missing days**: working days up to today with no time. A person with a capacity of 0
  has none. Details and the JSON shapes are in `docs/modules/time.md`.
* Copying: a whole week into another week (`time.log` for one's own, `time.manage_all` for somebody
  else's) or one entry to another day. A copy is a new entry and obeys every rule of one (closed
  job, 24-hour day, increment, period lock, editable timesheet); a week copy is a partial success
  (the entries that cannot be copied come back with a reason), and a target week whose timesheet
  is `submitted` or `approved` is refused (409 `TIMESHEET_NOT_EDITABLE`).
* Corrections: time on an `approved` timesheet that is still `unbilled` cannot be edited, but an
  approver (`time.manage_all` or `timesheet.approve`) can **correct** it with a mandatory reason
  (422 `TIME_CORRECTION_REASON_REQUIRED`). It follows the rules of an edit, keeps the rate unless
  the client, job, activity or date changes, and writes one row of an append-only history (the entry
  before and after, who and why) plus the audit entry `time_entry.corrected`. Nobody corrects their
  own approved time unless they hold `organization.update` (403 `SELF_CORRECTION_FORBIDDEN`). Moving
  the entry into another week puts it on that week's timesheet, which it then follows. Billed time
  or an entry of a submitted, open or returned sheet is not correctable (409 `TIME_NOT_CORRECTABLE`):
  a submitted sheet is returned by its approver, an open or returned one is edited.

## Timesheets

* `open → submitted → approved`, or `submitted → rejected → (edit) → submitted`.
* Submit: owner only, needs at least one entry, sets `submitted_at`.
* Approve / reject: `timesheet.approve`, and the approver is not the owner unless they are
  Owner/Admin (`organization.update`). Reject needs a `decision_note`.
* Recall: the owner may pull a `submitted` (not yet decided) sheet back to `open`.
* Reopen: an approver may reopen an `approved` sheet (back to `open`) as long as none of its
  entries is billed.
* When the practice has `require_timesheet_approval`, only time on `approved` timesheets is
  WIP that can be billed; otherwise all unbilled billable time is.
* The approvals queue lists submitted sheets of the people the caller manages
  (`organization_members.manager_user_id`), or all with `time.read_all`.
* Missing timesheets (`GET /timesheets/missing`): for a week (default: the last completed one),
  the active staff with a weekly capacity above 0, employed on at least one working day of that
  week, whose timesheet is not yet submitted (`none`: there is none, `open`, or `rejected`).
  Everyone with `time.read_all` sees all of them, others the people they manage. One function
  (`app/services/timesheet_missing.py:missing_timesheets`) serves the endpoint, the reminders, the
  Dashboard and the Missing Timesheets report.
* Reminders (`POST /timesheets/reminders/run`, and a daily scheduled job for every practice): a
  person with a missing timesheet gets the *timesheet reminder* notification and the approvers of
  submitted sheets of that week or earlier get one *approval pending* summary each. Each reminder
  goes out **once per person and week** (`timesheet_reminders` remembers it), a type the practice
  switched off is skipped, and people who cannot be notified (no login, not active) are only
  counted.

## Disbursements

* Client (required), job (optional, must be the client's), date, description, supplier,
  net `amount` (> 0), VAT rate to charge on recharge (null = practice default), billable.
* Editable and deletable only while `unbilled`, and only outside a locked period: recording,
  editing (the old and the new `incurred_on`) and deleting a disbursement dated on or before the
  period lock are 409 `PERIOD_LOCKED` (area `expense`).

## WIP (work in progress)

WIP is billable time (on approved timesheets when approval is required) and billable
disbursements with `billing_status = 'unbilled'`. For each client and job: time value,
disbursement value, total, hours, oldest item date and age buckets (0–30, 31–60, 61–90,
91+ days by item date).

**Write-off without an invoice** (`wip.write_off`): choose items (or all WIP of a job), a
reason and a date. Creates `wip_write_offs` + one `wip_allocations` row per item
(`billed_value = 0`), sets the items `written_off`. Reversible by deleting the write-off
(items return to `unbilled`) only while none of them has been billed since. A write-off, and
its reversal, is dated and obeys the **period lock** (409 `PERIOD_LOCKED`, area `wip`): the
date of the write-off (today in the practice's time zone when none is sent) must be after the
lock, and so must the date of the write-off being reversed.

**Fixed fees.** How much of a fixed fee counts as WIP follows the practice's
`fixed_fee_wip_mode` (Practice settings): with `on_completion` the unbilled fixed fee is WIP
once the job is completed or closed; with `by_budget_progress` it is **fixed fee × min(1,
logged minutes ÷ budget minutes) minus what is already billed**.

## Invoices

Lifecycle (stored): `draft → [awaiting_approval → approved] → issued → void`. The approval steps apply
only when the practice asks for approval and the invoice reaches the threshold; a rejection returns the
invoice to `draft`. What the person sees (`credited`, `paid`, `overdue`, `part_paid`) is derived from
the payments and credit notes. Drafts have no number and may be deleted.

**Draft from WIP** (`invoice.create`): choose the client, the WIP items (time entries and
disbursements, all from that client and currently `unbilled`) and how to present them:

* Time is grouped into one line per job (`kind = time`, description = job name + period,
  `quantity` = hours to 2 dp, `unit_amount` = average rate, `net_amount` = sum of values),
  or `fixed_fee` lines for fixed-fee jobs (`net_amount = fixed_fee`, the job's time WIP
  is cleared by it), plus one `disbursement` line per disbursement.
* The caller may override each line's `net_amount` (the bill amount). The billed value is
  apportioned across the line's items in proportion to their WIP value
  (`money.apportion`), so `Σ billed_value = net_amount` exactly. `billed − wip` is the
  item's write-up/write-off.
* Manual lines (`kind = manual`) carry no WIP. A negative manual line is a discount.
* Allocation sets the items to `in_draft`; a draft line removed or the draft deleted
  reverses it (rows deleted, items `unbilled`).

**Line maths:** `net = round(quantity × unit_amount)` unless the net was overridden;
`vat = round(net × vat_rate_bp / 10000)` per line; invoice totals are sums of lines;
`gross = net + vat`. When the practice is not VAT-registered every line's rate is 0.
VAT rate per line: the line's chosen rate, else the service's, else the client's default,
else the practice default.

**Issue** (`invoice.issue`): needs at least one line and `gross_total ≥ 0`; takes the next
`invoice` number; `issue_date` (default today) and `due_date` (default issue date + client
payment terms, else practice terms); items move `in_draft → billed`. Issued invoices are
immutable except `notes` and `reference`.

**Void** (`invoice.issue`): only with no payments allocated (409 `INVOICE_HAS_PAYMENTS`)
and no credit notes (409 `INVOICE_HAS_CREDITS`); needs a reason; reverses the WIP
allocations (`reversed_at`) and returns the items to `unbilled`. The number is kept (void
invoices stay in the sequence for the audit trail).

**PDF**: `GET …/invoices/{id}/pdf` renders the practice letterhead, client billing address,
number, dates, lines, VAT summary by rate, totals, bank details and footer (fpdf2).
Drafts render with a "DRAFT" watermark.

**Send**: `POST …/invoices/{id}/send` queues a `send_invoice_email` job to the given
addresses, else the client's `invoice_email`, else its billing contacts, else its primary
contact, else the client email, with the PDF attached; sets `sent_at`.

**Status for display** (derived, not stored): `draft`, `void`, `paid` (balance 0),
`part_paid`, `overdue` (balance > 0 and `due_date` < today), `issued`.

## Credit notes

Against one issued invoice, issued (numbered) on creation, lines with net and VAT rate.
`gross_total` ≤ the invoice's outstanding balance (409 `CREDIT_EXCEEDS_BALANCE`).
Increases `invoices.credited_total` under the invoice row lock. Does not reinstate WIP.
Immutable once issued.

## Payments

* Client, date received, amount (> 0), method, reference.
* Allocations to that client's issued invoices; each allocation ≤ the invoice balance,
  `Σ allocations ≤ payment amount`; the remainder is "on account". Allocating updates
  `invoices.paid_total` and `payments.allocated_total` under row locks (invoice rows locked
  in id order to avoid deadlocks).
* `auto_allocate` allocates to the client's oldest open invoices first.
* Allocations can be removed (unallocate); a payment can be voided only when fully
  unallocated.
* Client statement: invoices, credit notes and payments in date order with a running balance.

## Reports

All accept `date_from`/`date_to` (inclusive) and filters (client, job, staff, partner,
manager, service) where sensible, return JSON and CSV (`?format=csv`, UTF-8 with BOM for
Excel).

| Report | Contents |
|---|---|
| WIP | Unbilled WIP per client/job: hours, time value, disbursements, total, age buckets. As at a date. |
| Aged debtors | Outstanding balance per client in buckets by days past due date: current, 1–30, 31–60, 61–90, 91+. As at a date. |
| Time summary | Minutes per person/client/job/activity, chargeable vs non-chargeable. |
| Utilisation | Per person: capacity (weekly capacity × working days in range / working days per week), total, chargeable minutes, utilisation (chargeable / capacity), target (`chargeable_target_bp`). |
| Recoverability | Per client/job/person: WIP value billed, billed value, write-ups/offs, recovery (billed / WIP value). From live allocations to issued invoices and write-offs dated in the range. |
| Billing summary | Invoices issued in the range (net, VAT, gross), credit notes, receipts, per month and per partner. |
| Job profitability | Per job: billed value, cost value of time, margin, budget vs actual (minutes and fee). Requires `rate.read` for cost figures. |
| VAT summary | Output VAT by rate on invoices issued minus credit notes in the range (invoice basis). |

## Dashboard

For the signed-in person: hours this week (chargeable / total), unsubmitted timesheet
weeks, timer state, jobs due in 14 days they manage or are assigned to. With
`report.financial` also: total WIP, WIP older than 90 days, debtors total, overdue debtors,
billed this month and last month, approvals waiting.

## Notifications and email templates

Endpoints, shapes and the `notify` call are in `docs/modules/notifications.md`.

* A notification belongs to one person of one practice. Nobody reads or marks another person's
  (404). Its `link` is a path inside the app (`/time/approvals`), never an absolute URL.
* The catalogue has seven types: timesheet reminder, timesheet returned, approval pending,
  budget alert, invoice delivery, payment reminder, failed job. Each has three channels:
  in-app, email, SMS. Defaults: in-app and email on, SMS off (invoice delivery: in-app only,
  because the sender asked for the email).
* What reaches a person: nothing when the practice switched the type off; else their own choice
  for the type, else the practice's default, else the catalogue's. Only active members who can
  sign in are notified; SMS goes to the mobile number the person saved in their settings.
* `notify` is idempotent per `dedupe_key` and person: a repeated trigger creates no second row
  and queues no second email or SMS. A notification never fails the action that raised it.
* Triggers: a submitted timesheet notifies the person's manager (if they can approve), else
  everyone who holds `timesheet.approve`; a rejected timesheet notifies its owner; a delivered
  invoice email notifies the person who sent it; a background job that failed for good
  (`failed`, or `dead` after its retries) notifies the practice's administrators.
* Email and SMS only leave through the messaging gateway: SMTP when it is configured, else the
  mock that records every message in `message_log` (one-time links removed). SMS is always the
  mock. A transient failure is retried with the job's backoff; a refused recipient fails the job.
* An email template is a built-in default per key (in code; for the invoice and the two
  invitations, the existing design) that a practice may replace: subject, HTML body, text body.
  It may use only its own merge fields as plain `{{ field }}` placeholders: no statements,
  attribute access, filters or calls, and no scripts or event handlers in the HTML. Nothing is
  evaluated; values are HTML-escaped in HTML. Changing or resetting a template is audited.

## Tickets (Application Testing)

The practice's own tracker for bugs and enhancements found while using the application.
Endpoints, shapes, the exact dashboard and the error codes are in `docs/modules/tickets.md`.

* A ticket has a **number** from the gapless `ticket` sequence (prefix `APT`, no padding) taken in
  the transaction that creates it, and a stored **code** (`APT271`, unique per practice); a later
  change of the prefix does not rename old tickets. A failed create leaves no gap; concurrent
  creates never share a number.
* **Status** is `open` (default), `in_progress`, `requirement_pending`, `clarification_required`,
  `blocked`, `ready_for_ba_review`, `reopened`, `closed`, `invalid` or `on_hold`; **priority** is `high`,
  `medium`, `low` or `not_set` (default). Any status may follow any other. `closed_at` is set
  exactly while the status is `closed` (a database CHECK). The category is `application`.
* Creating a ticket and every status change, by edit or by bulk action, write a
  `ticket_status_history` row (`from_status` null for the first) in the same transaction. The
  dashboard trend is rebuilt from that history.
* The **assignee** must be an active member of the practice (an assignee who later becomes inactive
  keeps the ticket and their name). **Tags** are the practice's own (composite foreign keys: a
  foreign tag or person is a 422, never a link). A ticket carries at most 20 tags; a practice has
  at most 200, with names unique ignoring case. Deleting a tag that tickets carry needs
  `?force=true` and takes it off them.
* **Who may do what**: `ticket.read` sees everything (lists, detail, comments, files, the
  dashboard); `ticket.create` raises tickets; `ticket.update` edits tickets and tags, comments
  and uploads; `ticket.delete` deletes tags and moderates. A comment is edited only
  by its author; its author deletes it, and so does anyone with `ticket.delete`; a file is deleted
  by its uploader or by `ticket.delete`. A comment never ends up with neither text nor files.
* **Bulk actions** (status, priority, add or remove a tag) take 1 to 200 tickets in one
  transaction: all or nothing. An id that is not the practice's fails the whole call (404). Each
  ticket that actually changes gets its own history and audit row, like a single change.
* **Files**: at most 2 MB and 25 per ticket. An upload is accepted only if its extension is on the
  allow-list (png, jpg, jpeg, gif, webp, pdf, docx, xlsx, doc, xls, csv), its content is what the
  extension says (magic bytes; never the name or the client's content type) and a declared
  content type fits. SVG, HTML and everything else are refused. Names are sanitised, storage keys
  are random and per practice, files are served with `nosniff`, `inline` only for images, and
  deleting a comment or file removes the stored file once the transaction has committed.
  Files are reached only through the API with the caller's permission; they are not virus
  scanned. Where they live is the `STORAGE_DIR` setting (`docs/deployment.md`).
* **Dashboard**: every figure is about the tickets **created** in the date range (practice calendar
  days; default the last 19 days; at most 366). The previous period is the same number of days
  just before. A card's change is `New` (nothing before, something now), a whole percent (half
  away from zero) or none (zero both times). Percentages of status and priority are to one
  decimal, half up. Tags: the 7 most used plus `Others` (distinct tickets with any other tag).
  The trend shows, for the 14 days ending on the range's last day, the status each of those
  tickets had at the end of the day. Top assignees include an `Unassigned` row.
* **Audit**: every write (`ticket.created`, `ticket.updated`, `ticket.status_changed`,
  `ticket_comment.*`, `ticket_attachment.*`, `ticket_tag.*`) is audited in the same transaction.
* Another practice's tickets, tags, comments and files answer 404.

## Audit log

The practice's permanent record of who did what. Endpoints, shapes, the permission mapping and the
export are in `docs/modules/audit.md`.

* **What is recorded** (spec 2.20): every create, update and delete of a record that matters,
  every approval, invoice issue, payment, write-off, rate change, period lock and status change,
  and sign-ins. An entry has the actor, the action, the record (`target_type`, `target_id`), the
  time, the IP address, the user agent and request id and, for an update, `changes: {field: [old,
  new]}`. It is written by the owning service in the same transaction as the change; an update
  that changes nothing writes nothing. Sign-ins are account-level (no practice): recorded, never
  listed in a practice's log.
* **Append-only**: a database trigger refuses every `UPDATE` and `DELETE` of `audit_logs`, on every
  partition; no endpoint and no code path changes an entry, and a practice's deletion keeps its
  trail. Nothing deletes entries: UK practices keep their records at least six years.
* **Who reads it**: the list needs `audit_log.read`, the export `audit_log.read` and
  `audit_log.export`. The history of **one record** needs `audit_log.read` or the read permission
  of the record's type (`client.read` for a client, `invoice.read` for an invoice ...); without
  `audit_log.read` the IP address is left out. A person without `rate.read` never sees cost rates,
  cost values or margins in the trail either (they are `null`).
* **The export** is the newest 50,000 entries that match the filters, CSV or Excel, with formula
  protection, and is itself an entry (`audit_log.exported`).
* The trail shows the same change in one shape whatever the service stored:
  `changes: [{field, before, after}]`, with a `label` and a `summary` for people.
