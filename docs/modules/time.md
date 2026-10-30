# Module: jobs, time, timer, timesheets (prefix `time`)

Implements docs/domain.md sections **Rates**, **Jobs**, **Time** and **Timesheets**. Every
route is under `/api/v1/organizations/{organization_id}`, uses the standard envelope and is
cursor-paginated where it lists (`limit`, `cursor`, `meta.next_cursor`).

## Files

| Layer | Files |
|---|---|
| Routers | `app/api/v1/jobs.py`, `app/api/v1/time_entries.py` (time, week grid, bulk, timer), `app/api/v1/timesheets.py` (timesheets, approvals); `/rates/revalue` is in `app/api/v1/rate_rules.py` (Team & Rates) |
| Schemas | `app/schemas/jobs.py`, `app/schemas/time.py` |
| Services | `app/services/time_jobs.py` (JobService), `time_entries.py`, `timers.py`, `timesheets.py`, `charge_rates.py`, `time_rules.py` (shared rules and error classes) |
| Repositories | `app/repositories/time_jobs.py`, `time_entries.py` (entries, timer, WIP clause), `timesheets.py`, `time_refs.py` (clients/services/codes/members lookups), `time_rates.py`, `time_paging.py` (date keyset helper) |
| Time recording gaps | routers `app/api/v1/time_views.py`, `app/api/v1/timesheet_reminders.py`; services `time_targets.py`, `time_week.py`, `time_views.py`, `time_copy.py`, `time_corrections.py`, `timesheet_missing.py`, `timesheet_reminders.py`, `time_visibility.py`; repositories `time_overview.py`, `time_history.py`, `timesheet_reminders.py`; schemas `app/schemas/time_views.py`; models `app/db/models/time_history.py`; migrations `022`, `023` (last sections of this document) |
| Tests | `tests/test_jobs.py`, `test_time_entries.py`, `test_timer.py`, `test_timesheets.py`, `test_charge_rates.py`, helpers in `tests/time_helpers.py`; time recording gaps: `test_time_entry_fields.py`, `test_time_week_targets.py`, `test_time_copy.py`, `test_time_corrections.py`, `test_timesheet_missing.py`, `test_time_migrations.py`, `test_time_gaps_openapi.py`, helpers in `tests/time_gap_helpers.py` |

`app/services/jobs.py` and `app/repositories/jobs.py` already hold the **background-job
queue** (used by the worker), so the client-work job code lives in `time_jobs.py` instead.

## Endpoints

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/jobs` | `job.read` | List jobs with actuals; filters `client_id`, `status` (multi), `manager_user_id`, `partner_user_id`, `assignee` (id or `me`), `service_id`, `due_from`, `due_to`, `q`; `sort` = `due_date`/`number`/`created_at` (prefix `-` for desc, default `-created_at`) |
| POST | `/jobs` | `job.create` | Create (number from `job` sequence, defaults from service) |
| GET | `/jobs/{id}` | `job.read` | One job with assignees and actuals |
| POST | `/jobs/bulk` | `job.update` (`job.delete` for `delete`) | One action on 1-200 jobs, all or nothing (see "Bulk action on jobs"). Declared before `/jobs/{id}`. |
| PATCH | `/jobs/{id}` | `job.update` | Edit details (not client, not status) |
| POST | `/jobs/{id}/status` | `job.update` | Status transition `{status}` |
| PUT | `/jobs/{id}/assignees` | `job.update` | Replace assignees `{user_ids}` (active members) |
| DELETE | `/jobs/{id}` | `job.delete` | Soft delete when unused |
| GET | `/time-entries` | any member (own) / `time.read_all` | List; `user_id` = `me` (default), id or `all`; `date_from`, `date_to`, `client_id`, `job_id`, `billable`, `billing_status` (multi), `timesheet_status` (multi) |
| GET | `/time-entries/week` | own / `time.read_all` / manager | Weekly grid with the person's targets, working days, missing days and day variance; `week_start` (any day of the week, default this week), `user_id` |
| POST | `/time-entries` | `time.log` (own) / `time.manage_all` | Log time |
| POST | `/time-entries/bulk` | as above, per entry | Up to 100 entries, atomically |
| GET | `/time-entries/{id}` | own / `time.read_all` | One entry |
| PATCH | `/time-entries/{id}` | `time.log` (own) / `time.manage_all` | Edit (re-prices) |
| DELETE | `/time-entries/{id}` | `time.log` (own) / `time.manage_all` | Delete |
| GET | `/time-entries/day` | own / `time.read_all` / manager | A day: entries, totals, target, minutes remaining and the caller's own timer; `date`, `user_id` |
| GET | `/time-entries/calendar` | own / `time.read_all` / manager | One row per day of `from`..`to` (at most 62 days): totals, target and the week's sheet status; `user_id` |
| POST | `/time-entries/copy-week` | `time.log` (own) / `time.manage_all` | Copy a week into another week (200, partial success) |
| POST | `/time-entries/{id}/copy` | `time.log` (own) / `time.manage_all` | Copy one entry to another day (201) |
| POST | `/time-entries/{id}/correct` | `time.manage_all` or `timesheet.approve` | Correct approved, unbilled time; a reason is required |
| POST | `/time-entries/{id}/remove` | `time.manage_all` or `timesheet.approve` | Delete approved, unbilled time posted by mistake; a reason is required (audited as `time_entry.deleted`); not one's own time without `organization.update` |
| GET | `/time-entries/{id}/history` | own / `time.read_all` / manager | The corrections of an entry, newest first, cursor-paginated |
| POST | `/rates/revalue` | `rate.manage` | Re-price unbilled entries `{date_from, date_to, user_id?, client_id?}` (V1; the preview and apply pair is in `docs/modules/team-rates.md`) |
| GET | `/timer` | `time.log` | The caller's timer or `null` |
| POST | `/timer/start` | `time.log` | `{client_id?, job_id?, activity_code_id?, description?, is_billable?, replace?}` |
| POST | `/timer/pause` | `time.log` | Pause |
| POST | `/timer/resume` | `time.log` | Resume |
| POST | `/timer/stop` | `time.log` | Write the entry (optional body `{description}`), delete the timer, return the entry (201) |
| DELETE | `/timer` | `time.log` | Discard without an entry |
| GET | `/timesheets` | own / `time.read_all` / manager | `user_id` (`me` default, id, `all`), `from`, `to` (bound `week_start`), `status` (multi); newest first |
| GET | `/timesheets/{id}` | own / `time.read_all` / manager | Sheet with entries, totals, `day_totals` |
| POST | `/timesheets/{id}/submit` | owner | `open`/`rejected` → `submitted` |
| POST | `/timesheets/{id}/recall` | owner | `submitted` → `open` |
| POST | `/timesheets/{id}/approve` | `timesheet.approve` | `submitted` → `approved`, optional `{decision_note}` |
| POST | `/timesheets/{id}/reject` | `timesheet.approve` | `submitted` → `rejected`, `{decision_note}` required |
| POST | `/timesheets/{id}/reopen` | `timesheet.approve` | `approved` → `open` when no entry is billed |
| DELETE | `/timesheets/{id}` | `time.log` (own) / `time.manage_all` | Delete an `open`/`rejected` sheet and its time, when none of it is billed (409 otherwise) |
| GET | `/approvals` | any member | Submitted sheets of the people the caller manages (all with `time.read_all`), oldest week first |
| POST | `/approvals/bulk-approve` | `timesheet.approve` | `{timesheet_ids}` (1–100), atomically |
| GET | `/timesheets/missing` | `time.read_all`, else the people the caller manages | Staff whose timesheet for a week is not submitted; `week_start`, `user_id`, `manager_user_id` |
| POST | `/timesheets/reminders/run` | `organization.update` or `timesheet.approve` | Send the timesheet and approval reminders of a week; optional `{week_start}` |

## Bulk action on jobs

`POST /jobs/bulk`, body `{job_ids, action, value?}`. All or nothing in one transaction: the jobs are locked in id order and a
rule that any job breaks (409) stops the whole action, so nothing is ever partly applied. Permission `job.update`, or `job.delete`
for `delete` (403 otherwise, as for the single operations).

| Field | Type | Notes |
|---|---|---|
| `job_ids` | uuid[] 1-200 | Duplicates count once. Any id that is not a live job of the practice: 404 `RESOURCE_NOT_FOUND`, `details: {"job_ids": [the unknown ids]}`, nothing changes. |
| `action` | `set_status`, `set_manager`, `set_due_date`, `delete` | |
| `value` | string or null | `set_status`: a job status (required). `set_manager`: a staff user id (an active member, 422 otherwise); null clears the manager. `set_due_date`: `YYYY-MM-DD`; null clears it. `delete`: no value (a value is 422). A wrong value is 422 on `value`. |

Rules are those of the single operations. `set_status` uses the same transition table as `POST /jobs/{id}/status` (a job that is
already in the wanted status is skipped; one that may not move there answers 409 `INVALID_STATUS_TRANSITION`, `details`: one entry
per job with `job_id`, `number`, `reason`, `from`, `allowed`) and sets `completed_at`/`closed_at` the same way. `set_manager` and
`set_due_date` answer 409 `JOB_FIELD_MANAGED_BY_PM` for a job linked to practice management; `set_due_date` answers 409
`JOB_DUE_DATE_INVALID` for a due date before a job's start date or one that leaves an open task due after it. `delete` answers 409
`JOB_IN_USE` when any selected job has time, disbursements, expense lines, invoice lines or write-offs (it deletes none); the jobs
that can go also lose their running timers.

Response `{"action", "affected", "changed"}`: `affected` is the number of distinct jobs selected, `changed` how many actually
changed (a job already in the wanted state does not count). Each job that changed gets its own audit row, the one the single
operation writes (`job.status_changed`, `job.updated` with `changes`, `job.deleted`), with `bulk: true` in its meta. A changed
status of a linked job is queued for practice management as for a single change.

## Bulk actions on tasks

**Add many to a job** - `POST /jobs/{job_id}/tasks/bulk`, body `{"tasks": [TaskCreate, ...]}` (1-100 items, each as in
`POST /jobs/{id}/tasks`). Permission `job.update`. The tasks are added at the end of the job's list in the order sent, in one
transaction. If any item is refused (a due date outside the job's dates, an inactive assignee, the 500-task limit) the answer is
409 `BULK_REFUSED` with `details` = one entry per refused item (`name` = `Task <n>: <title>`, `row`, `error_code`, `reason`) and
nothing is added. A closed or cancelled job answers 409 `JOB_CLOSED`, an unknown job 404. Response 201 with the created `TaskRead[]`.

**Change or delete many** - `POST /tasks/bulk`, body `{task_ids, action, value?}` (1-200 tasks of any jobs). All or nothing.

| `action` | `value` | Same rules as |
|---|---|---|
| `set_status` | `not_started`, `in_progress`, `on_hold` or `completed` | `PATCH /jobs/{id}/tasks/{id}/status`: needs `job.update`, or being one of the task's assignees |
| `set_due_date` | `YYYY-MM-DD`, or null to clear | `PATCH /jobs/{id}/tasks/{id}` (within the job's dates) |
| `add_assignee` / `remove_assignee` | a staff user id (an active member to add) | `PUT .../assignees` (the person is added to or taken from the current list) |
| `delete` | none | `DELETE /jobs/{id}/tasks/{id}`: 409 `TASK_IN_USE` for a task with time or a running timer |

The route needs `job.read`; every action except `set_status` needs `job.update` (403 otherwise). Each task goes through the
**same service call** as its single endpoint, so a closed job (`JOB_CLOSED`), the job's dates and the other rules hold. A task
refused by any rule refuses the batch: 409 `BULK_REFUSED`, `details` = one entry per task (`name` = title, `task_id`, `job_id`,
`error_code`, `reason`), nothing changes. Unknown or foreign ids: 404, `details: {"task_ids": [...]}`. Response
`{"action", "affected", "changed"}` (`changed` leaves out tasks already in the wanted state). Each changed task writes its usual audit
entry, and one `task.bulk_changed` entry records `action`, `count`, `task_ids` (at most 100) and `bulk: true`.

## Default tasks of a service

A service can carry a list of default tasks (`PUT /services/{id}/task-templates`, see `clients.md`). They save typing the same tasks
into every job:

- **A new job** - `POST /jobs` with `add_default_tasks: true` (default false, so existing callers and the CSV import are unchanged;
  ignored without a service or when the service has none) adds one task per template, in order, in the same transaction as the job. Each
  task is `not_started`, takes the template's title, description and hours budget, is assigned to the job's assignees, and is due
  `due_offset_days` after the job's `start_date` when the job has one and the result lies within the job's dates (otherwise it has no due
  date; a date never fails the job). Every task goes through the normal task rules, so the 500-task limit stops the whole job (409
  `TASK_LIMIT_REACHED`, nothing created). Audited as `task.created` (meta `source: "service_template"`) and `default_tasks_added` on `job.created`.
- **An existing job** - `POST /jobs/{job_id}/tasks/from-service` (no body, `job.update`) adds the templates of the job's service that the
  job does not have yet, comparing titles ignoring case. Response `{"added", "skipped"}`. 409 `SERVICE_HAS_NO_TASKS` when the job has no
  service or the service has no templates, 409 `JOB_CLOSED` for a closed or cancelled job.
- **Copy job** is unchanged (it copies the source job's own tasks).

Not yet automatic: jobs from deadlines (see `docs/plan.md`), and tasks in the job CSV import.

## Recurring jobs from engagements

A client engagement (`client_services`, see `clients.md`) with `auto_create_jobs: true` gets one job per period without anyone creating
it. The daily worker `create_engagement_jobs` (`app/services/engagement_jobs.py`, registered in `app/workers/handlers.py` next to
`run_recurring_invoices`) does it for every practice; `POST /clients/{id}/services/{engagement_id}/create-job` does it on demand.

- **Periods** are anchored on the engagement's `start_date`: monthly = start + k months, quarterly = start + 3k, annually = start + 12k,
  always counted from the start (31 January -> 28 February -> 31 March; 29 February -> 28 February in common years). A period ends the
  day before the next one starts (`period_end`). `one_off` has one period from `start_date` with no end.
- **Due** when `today >= period_start - job_lead_days` and the period has not ended and starts on or before the engagement's `end_date`
  and the engagement is active and the period has no run row. "Today" is the practice time zone. History is never backfilled: only the
  latest period whose window has opened is considered, so at most one job per engagement per run, however long the worker was down.
- **The job** goes through `JobService.create`, so every job rule applies: the engagement's client and service; name `"{service} - {label}"`
  with label `Oct 2026` (monthly, period start), `Oct 2026 - Dec 2026` (quarterly, start month - end month), `Year to Sep 2027` (annually,
  month of `period_end`) and no label for `one_off`; `period_start`/`period_end` = the period; `start_date` = `period_start`;
  `due_date` null; status `planned`; manager and partner of the client when they are still active members; billing basis from the service
  (and the engagement `fee` as `fixed_fee` for a fixed-fee service); the service's default tasks (`add_default_tasks`); no assignees.
  Names may repeat (jobs are numbered). The worker acts as the practice's oldest active Owner; the audit entries carry `source:
  "engagement_schedule"` (`engagement_manual` for the route).
- **Idempotent**: `engagement_job_runs` has one row per (engagement, `period_start`) (unique), written in the same transaction as the job,
  and the engagement row is locked (`skip_locked` in the worker), so a delivered-twice run creates one job. Deleting the job keeps the row
  (`job_id` becomes null on a hard delete), so the period is not created again.
- **Failures** (the client is archived or inactive, the service was deactivated, a validation rule) are logged and recorded as a `failed`
  run with the reason, visible in `GET .../job-runs`; one failing engagement or practice does not stop the others. The worker retries a
  failed period **at most once a day**: only when the failed run's `updated_at` date (practice zone) is before today. The manual route
  retries at once.
- **Manual route**: creates the job of the latest period whose window has opened (else the upcoming one) whether or not `auto_create_jobs`
  is on. 409 `ENGAGEMENT_JOB_EXISTS` when that period already has its job; 409 `ENGAGEMENT_NOT_SCHEDULABLE` (reason in the message) when
  the engagement has no start date, is inactive or ended, or the client or service does not accept work.

## Shapes (abridged)

**JobRead**: `id, number, name, description, client_id, client_code, client_name, service_id,
service_name, status, billing_basis, fixed_fee, hourly_rate, budget_minutes, budget_fee,
period_start, period_end, start_date, due_date, partner_user_id, manager_user_id,
assignee_user_ids[], completed_at, created_at, updated_at` + actuals `logged_minutes,
billable_minutes, time_value, wip_value, billed_value, written_off_value,
budget_minutes_used_bp, budget_fee_used_bp`.

**JobCreate**: `client_id` (required), `name` (required), `description, service_id,
billing_basis, fixed_fee, hourly_rate, budget_minutes, budget_fee, period_start, period_end,
start_date, due_date, partner_user_id, manager_user_id, status` (`planned` | `in_progress`,
default `planned`), `assignee_user_ids[]`. **JobUpdate**: the same minus `client_id`,
`status`, `assignee_user_ids`, all optional.

**TimeEntryRead**: `id, user_id, timesheet_id, timesheet_status, entry_date, minutes,
client_id, client_code, client_name, job_id, job_number, job_name, service_id, service_name`
(the job's service, read-only; `null` without a job), `activity_code_id, activity_code,
activity_name, description, internal_note` (`null` unless the caller owns the entry or holds
`time.read_all` or `time.manage_all`), `is_billable, charge_rate, charge_value, cost_rate,
cost_value` (cost fields `null` without `rate.read`), `rate_source` (the level of the rate
ladder that priced the entry, see `docs/modules/team-rates.md`; `null` for entries saved before
it existed), `billing_status, is_locked, period_locked_through` (the last day of the locked period
the entry's date lies in, `null` when it is open: such time cannot be edited or deleted; filled in
by the list, one entry and the day view), `created_by, created_at, updated_at`.

**TimeEntryCreate**: `user_id?, entry_date, minutes (1–1440), client_id?, job_id?,
activity_code_id?, description?, internal_note? (up to 2000 characters), is_billable?`.
**TimeEntryUpdate**: all optional; `client_id`/`job_id`/`activity_code_id`/`is_billable`/
`internal_note` accept `null` (a blank note is stored as `null`). **Bulk**: `{entries: [...]}`.

**WeekGrid**: `user_id, week_start, week_end, days[7], timesheet_id, timesheet_status,
rows[{client_*, job_*, activity_*, is_billable, minutes[7], total_minutes, entry_ids[]}],
day_totals[7], total_minutes, chargeable_minutes, non_chargeable_minutes`, and the target
figures `today, targets{daily_minutes{date: minutes}, weekly_minutes}, working_days[],
missing_days[], day_variance[7]` (see "Targets, working days and missing days").

**TimerRead**: `id, user_id, client_id, job_id, activity_code_id, description, is_billable,
is_running, started_at, accumulated_seconds, elapsed_seconds, started_on, created_at`.

**TimesheetRead**: `id, user_id, user_name, week_start, week_end, status, submitted_at,
decided_by, decided_at, decision_note, entry_count, total_minutes, billable_minutes,
non_billable_minutes, billable_value, created_at, updated_at`; **TimesheetDetail** adds
`entries[]` and `day_totals[{day, minutes}]`.

**RevalueResult**: `entries_examined, entries_changed, charge_value_before,
charge_value_after, value_delta, cost_value_delta` (`null` without `rate.read`).

## Error codes

| Status | Code | When |
|---|---|---|
| 409 | `CLIENT_INACTIVE` | New job / time / timer for an `inactive` or `archived` client |
| 409 | `JOB_CLOSED` | Time or timer on a `completed`/`cancelled` job |
| 409 | `TIME_LOCKED` | Writing time on a `submitted`/`approved` sheet, or an entry that is not `unbilled` |
| 409 | `INVALID_STATUS_TRANSITION` | Job or timesheet status change not allowed (`details.from/to`) |
| 409 | `JOB_IN_USE` | Deleting a job referenced by time, disbursements, invoice lines or write-offs (bulk: `details` lists each such job) |
| 409 | `JOB_FIELD_MANAGED_BY_PM` | A bulk `set_manager` / `set_due_date` that includes a job linked to practice management (`details`: one entry per job) |
| 409 | `JOB_DUE_DATE_INVALID` | A bulk `set_due_date` that is before a job's start date or leaves an open task due after it (`details`: one entry per job) |
| 409 | `TIMER_RUNNING` | Start with a timer already present (without `replace`), resume a running timer |
| 409 | `TIMER_NOT_RUNNING` | Pause a paused timer |
| 409 | `TIMESHEET_HAS_BILLED_TIME` | Reopen with an entry `in_draft`, `billed` or `written_off` |
| 409 | `TIMESHEET_NOT_EDITABLE` | `copy-week` into a week whose timesheet is `submitted` or `approved` (`details.timesheet_id`, `timesheet_status`, `week_start`) |
| 409 | `TIME_NOT_CORRECTABLE` | Correcting an entry whose timesheet is not `approved` or that is not `unbilled` (`details.timesheet_status`, `billing_status`) |
| 409 | `PERIOD_LOCKED` | A copy or correction dated in a locked period (raised by `app/services/period_locks.py`; a skipped copy in `copy-week`) |
| 422 | `TIME_CORRECTION_REASON_REQUIRED` | A correction without a reason (`details[0].field` = `reason`) |
| 422 | `TIME_CORRECTION_EMPTY` | A correction that sends no field, or changes nothing |
| 403 | `SELF_CORRECTION_FORBIDDEN` | Correcting one's own approved time without `organization.update` |
| 404 | `TIMER_NOT_FOUND` | Pause/resume/stop/discard without a timer |
| 404 | `RESOURCE_NOT_FOUND` | Unknown or other practice's job, client, service, activity code, entry, sheet (`details.field` names a body reference) |
| 403 | `PERMISSION_DENIED` | Missing permission, other person's time without `time.read_all`/`time.manage_all`, submit/recall by a non-owner |
| 403 | `SELF_APPROVAL_FORBIDDEN` | Approve/reject/reopen one's own sheet without `organization.update` |
| 422 | `DAY_LIMIT_EXCEEDED` | More than 1440 minutes for one person on one day (after rounding) |
| 422 | `TIMESHEET_EMPTY` | Submitting a sheet with no entries |
| 422 | `VALIDATION_ERROR` | Body/query validation; people (`user_id`, `manager_user_id`, assignees) that are not active members; inactive service or activity code; job of another client; fixed job without fee; billable without client; `details[].code` `activity_not_allowed` (activity code outside the job's service's list; also on bulk entries and the timer) and `after_leaving_date` (time after the person's leaving date) |

## Rules and decisions beyond docs/domain.md

* **Rates** (`app/services/charge_rates.py`, rules in `docs/domain.md` and
  `docs/modules/team-rates.md`): `resolve(session, org, user_id, client_id, job_id,
  entry_date) -> (charge_rate, cost_rate)` and `resolve_many(session, org, [RateQuery])` (four
  queries for any number of entries; each result also carries the `source` level, saved on the
  entry as `rate_source`). A `jobs.hourly_rate` of 0 is a rate that exists (it is not skipped).
  Rates are re-taken on every change of an entry (entries can only change while `unbilled`);
  a rate change alone never touches an entry.
* **Revalue** re-prices every `unbilled` entry in the date range, whatever its timesheet
  status, at the current hierarchy; `in_draft`/`billed`/`written_off` entries are never
  touched. One audit record `time_entries.revalued` per call, and a re-rate run of source
  `revalue` (visible in `GET /rates/reruns`). Range at most five years.
* **Billable default**: false without a client, for `non_billable` jobs and non-chargeable
  activity codes. Explicit `is_billable: true` is accepted for a non-chargeable activity code
  but refused (422) without a client or on a `non_billable` job.
* **Editing**: the rules `CLIENT_INACTIVE`/`JOB_CLOSED` apply on create, and on update when
  the client/job/activity, minutes or date change; a description-only edit of time on a
  completed job is allowed. Deleting is allowed (lock rules still apply). `is_billable` is
  recomputed when it is sent as `null` or when the client/job/activity changes without it.
  Setting `job_id` without `client_id` takes the job's client; clearing `job_id` keeps the
  client. Moving an entry to another week moves it to that week's sheet (both must be
  editable). `user_id` cannot be changed.
* **Who may log for whom**: own time `time.log`; anyone else's `time.manage_all` and the
  person must be an active member. Reading others' entries needs `time.read_all`; timesheets
  and the week grid also allow the person's manager (`organization_members.manager_user_id`).
* **References** in bodies (client, job, service, activity code) of another practice or
  unknown answer **404** (`details.field` names the field); people that are not active
  members answer 422 (house style of `members.py`).
* **Jobs**: client and status are not patchable (status has its own endpoint; changing the
  client would orphan time). New jobs start `planned` or `in_progress`. Moving to the same
  status is an invalid transition. `completed_at` is set on completion and cleared on reopen.
  Deleting a job is a soft delete (`deleted_at`) and also discards timers running on it.
  Changing `hourly_rate` (or any rate) never rewrites existing entries.
* **Job actuals**: `logged_minutes` (all time), `billable_minutes`, `time_value` (charge value
  of billable time), `wip_value` (billable, `unbilled` time; not gated by timesheet approval,
  time only), `billed_value` (`SUM(wip_allocations.billed_value)` of live allocations to
  **issued** invoices, via the job's time entries and disbursements), `written_off_value`
  (`SUM(wip_value)` of live write-off allocations of the job's items),
  `budget_minutes_used_bp = logged_minutes / budget_minutes`, `budget_fee_used_bp =
  time_value / budget_fee` (null without a budget).
* **Week grid**: rows grouped by (client, job, activity code, is_billable); "chargeable" =
  billable minutes. A week without a sheet reports `timesheet_status: "open"` and
  `timesheet_id: null`.
* **Timer**: `started_on` (the entry date on stop) is the practice-time-zone date of the
  timer's `created_at`. Elapsed seconds → whole minutes rounded up (minimum 1, at most 1440)
  → practice increment. The timer is kept if writing the entry fails. `replace: true` stops
  the old timer (writing its entry) in the same transaction.
* **Timesheets**: resubmitting clears the previous decision fields; reopening clears them and
  `submitted_at`. Approve accepts an optional `decision_note`. Bulk approve is all-or-nothing
  and de-duplicates ids. The approvals queue needs no permission (an empty list for people
  who manage nobody). `GET /timesheets?user_id=all` without `time.read_all` lists the caller
  and their reports.
* **Concurrency**: every time write locks the person's timesheet row(s) `FOR UPDATE` (created
  on demand with `INSERT … ON CONFLICT DO NOTHING`, locked in id order), then the entry row.
  That serialises the daily cap, the sheet status checks and edits against submit/approve.
  Revalue locks the entries in id order. Reopen locks the sheet and then its entries, so it
  cannot race a billing allocation (which locks entries). The timer insert uses `ON CONFLICT
  DO NOTHING` on `(organization_id, user_id)` and reports `TIMER_RUNNING`; stop/pause/resume
  lock the timer row. Job changes lock the job row `FOR UPDATE`; time writes and timer
  start/stop take the job `FOR KEY SHARE` (re-reading status and `deleted_at`), so time never
  lands on a job closed or deleted at the same moment. Lock order is job, then timer (job
  delete locks the job, then deletes its timers; stop and start lock the timer's job first).
  An entry's own sheet is locked by `timesheet_id`, never re-derived from its date, so
  entries stay editable after the practice changes `week_start`; an edited entry moves only
  when its new date is outside its sheet's week. Entry and grid dates are bounded to
  1900-01-01..2999-12-31. Job numbers come from the locked `job` sequence.
* **Audit** (all in the same transaction): `job.created`, `job.updated` (changes),
  `job.status_changed`, `job.assignees_changed`, `job.deleted`, `time_entry.created`
  (`source` = `manual`/`bulk`/`timer`, `rate_source`), `time_entry.updated` (changes),
  `time_entry.deleted`, `time_entries.revalued`, `timesheet.submitted|recalled|approved|
  rejected|reopened` (person, week, totals, note); and for the recording gaps `time_entry.corrected`
  (reason, history id, changes), `time_entry.week_copied`, `time_entry.copied` and
  `timesheet.reminders_sent`, with `time_entry.created` carrying `source` = `copy_week` / `copy`.

## Time recording: targets, copying, corrections and reminders

Everything in this section is additive: no earlier path, field or rule changed. The UI builds
the My Timesheet, Team Timesheets, Timesheet Approvals and Missing Timesheets screens from it.

### Entry fields

* `internal_note` (text, at most 2000 characters, `null`) is accepted by `POST /time-entries`,
  `POST /time-entries/bulk` (per entry) and `PATCH /time-entries/{id}` and returned on every
  entry (`GET /time-entries`, `/{id}`, `/day`, the timesheet detail, copies, corrections). A blank
  value is stored as `null`. It is internal: no client-facing document may print it. It is
  **hidden (`null`) unless the caller owns the entry or holds `time.read_all` or `time.manage_all`**:
  a person's manager who has neither sees the timesheet but not the note. The same applies to the
  note inside the history snapshots. The audit trail records that a note changed
  (`changes.internal_note` is `[had a note before, has one now]`), never its text.
* `service_id` and `service_name` are the service of the entry's job (read-only, `null` without a
  job or when the job has no service). An entry has no service of its own.

### Targets, working days and missing days

`GET /time-entries/week` (any day of the week in `week_start`, default this week) now also
returns the following. The example is a Monday to Sunday week with 450, 480, 0, 300, 0, 60 and 0
minutes logged, seen on Wednesday 16 September:

```json
{
  "today": "2026-09-16",
  "targets": {
    "daily_minutes": {"2026-09-07": 450, "2026-09-08": 450, "2026-09-09": 450,
                      "2026-09-10": 450, "2026-09-11": 450, "2026-09-12": 0, "2026-09-13": 0},
    "weekly_minutes": 2250
  },
  "working_days": ["2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"],
  "missing_days": ["2026-09-09", "2026-09-11"],
  "day_variance": [0, 30, -450, -150, -450, 60, 0]
}
```

The rule (`app/services/time_targets.py`, shared by the day and calendar views and the missing
timesheets list):

1. A person's **pattern** is their weekly capacity (`weekly_capacity_minutes`, default 2250 =
   37.5 hours), their working weekdays (`working_days`, ISO 1 = Monday, default Monday to
   Friday) and their employment period (`start_date` to `leaving_date`, both inclusive, either
   may be open). Someone who is not a member of the practice (only a `time.read_all` caller can
   ask for such an id) falls back to the practice's `minutes_per_day` x `working_days`.
2. The **daily target** of a working day is the capacity divided by the number of working weekdays;
   when it does not divide, the first working weekdays (in ISO order) get one minute more, so a
   full week always adds up to the capacity exactly (2000 minutes on Monday, Wednesday and Friday:
   667, 667, 666). A day off, and any day before the start date or after the leaving date, has a
   target of 0. `weekly_minutes` is the sum of the week's daily targets (so it is smaller in the
   week somebody starts or leaves).
3. `working_days` are the dates of the week that are working weekdays inside the employment
   period (they are listed even when the capacity is 0).
4. `missing_days` are the working days **up to and including `today`** (the practice's time zone)
   with no minutes logged. Days in the future are never missing; a person with a capacity of 0 is
   never asked for time, so they have none.
5. `day_variance[i]` is the minutes logged on `days[i]` minus that day's target (positive: over
   target, including time on a day off). `day_totals`, `total_minutes` and the other earlier fields
   are unchanged.

`GET /time-entries/day?date=&user_id=` (`date` required; `user_id` is `me` by default or a user id;
own, `time.read_all` or the person's manager) returns one day:

```json
{
  "user_id": "…", "date": "2026-09-07", "week_start": "2026-09-07",
  "timesheet_id": "…", "timesheet_status": "open", "is_working_day": true,
  "entries": [ "...TimeEntryRead..." ],
  "total_minutes": 180, "billable_minutes": 120, "non_billable_minutes": 60,
  "target_minutes": 450, "remaining_minutes": 270, "variance_minutes": -270,
  "timer": null
}
```

`remaining_minutes` is the target minus the total, never below 0. `timesheet_id` is `null` (and
`timesheet_status` `open`) while the week has no timesheet. `timer` is the caller's own timer
(`TimerRead`) when they view their own day and have one, otherwise `null`: another person's timer is
never shown.

`GET /time-entries/calendar?from=&to=&user_id=` (both dates required, `to` on or after `from`, at
most **62 days** in all: 422 with `details[0].field` `to`, `code` `range_too_long` beyond that) returns
one row per day of the range (14 of them in this example, two shown):

```json
{
  "user_id": "…", "date_from": "2026-09-07", "date_to": "2026-09-20",
  "days": [
    {"date": "2026-09-07", "week_start": "2026-09-07", "entry_count": 1, "total_minutes": 120,
     "billable_minutes": 120, "target_minutes": 450, "is_working_day": true,
     "timesheet_status": "approved"},
    {"date": "2026-09-08", "week_start": "2026-09-07", "entry_count": 2, "total_minutes": 90,
     "billable_minutes": 30, "target_minutes": 450, "is_working_day": true,
     "timesheet_status": "approved"}
  ],
  "total_minutes": 210, "billable_minutes": 150, "target_minutes": 4500
}
```

`timesheet_status` is the status of the timesheet of the day's week (`open` when the week has none).
Both views follow the access rules of the week grid: the person's own time, anybody's with
`time.read_all`, or the people the caller manages (403 otherwise). As in the week grid, a
`time.read_all` caller who asks for an id that is not a member of the practice gets an empty view
built on the practice's default working pattern, not an error.

### Copying time

`POST /time-entries/copy-week` copies one week's entries into another week, day for day:

```json
{
  "from_week_start": "2026-09-07",
  "to_week_start": "2026-09-14",
  "user_id": null,
  "entry_ids": null,
  "include_notes": false
}
```

* Both weeks may be given as any day of them (moved back to the practice's first day of the week).
  They must be different weeks (422, `details[0].field` `to_week_start`); the target may be earlier
  or later. The offset is whole weeks, so an entry on Tuesday lands on Tuesday.
* `user_id` copies someone else's week (needs `time.manage_all`, the right to see that person's
  time and an active member: 403 / 422 otherwise); the copies belong to that person and
  `created_by` is the caller. Without it the caller's own week is copied (needs `time.log`).
* `entry_ids` (1-500 ids) copies only those entries; an id that is not an entry of that person in the
  source week (or of another practice) answers 404 `RESOURCE_NOT_FOUND` with `details.field` `entry_ids`.
  `include_notes` also copies the internal notes; descriptions, client, job, activity, billable status
  and minutes are always copied.
* The target week's timesheet must not be `submitted` or `approved`: 409 `TIMESHEET_NOT_EDITABLE`
  and nothing is created (a returned/`rejected` or `open` sheet is fine; the entries join it).
* **Each copy is a new entry and is validated exactly like one** (the same code path as
  `POST /time-entries`): minutes are rounded up to the practice time increment, at most 24 hours a
  day (counting what is already there and the copies made before it), no time on a closed job or for
  an inactive client, no time after the person's leaving date or outside the activity codes of the
  job's service, no time in a locked period (`app/services/period_locks.py`, area `time`), and the
  sheet must be editable. The rate is taken again for the new date. Copies are made in the order of
  the source entries (date, then when they were logged).
* Partial success is normal: the answer is **200** with what was made and what was not.

```json
{
  "from_week_start": "2026-09-07",
  "to_week_start": "2026-09-14",
  "created": [ "...TimeEntryRead..." ],
  "skipped": [
    {"source_entry_id": "…", "code": "JOB_CLOSED", "message": "Time cannot be logged on a completed or cancelled job."}
  ]
}
```

  `skipped[].code` is the code the same entry would get when created by hand: `JOB_CLOSED`,
  `CLIENT_INACTIVE`, `DAY_LIMIT_EXCEEDED`, `PERIOD_LOCKED`, `ACTIVITY_NOT_ALLOWED`, `AFTER_LEAVING_DATE`,
  `TIME_LOCKED` (the sheet was submitted while copying), `RESOURCE_NOT_FOUND` or `VALIDATION_ERROR`. A skipped
  copy leaves nothing behind. Nothing to copy is a 200 with two empty lists (and no timesheet is created).
  Audit: `time_entry.created` per copy (`source` = `copy_week`) and one `time_entry.week_copied` with the
  source/new ids and the skipped codes (never any note text).

`POST /time-entries/{entry_id}/copy` with `{"date": "2026-09-09", "include_notes": false}` (`entry_date` is accepted for
`date`) copies one entry to another day, the same day included, for the same person (own: `time.log`;
someone else's: `time.manage_all` and the right to see their time). It answers **201** with the new
`TimeEntryRead` and follows every rule of `POST /time-entries` with its error codes (409 `JOB_CLOSED`,
`CLIENT_INACTIVE`, `TIME_LOCKED` for a submitted/approved week, `PERIOD_LOCKED`; 422 `DAY_LIMIT_EXCEEDED`).
Audit: `time_entry.created` (`source` = `copy`) and `time_entry.copied` (`source_entry_id`).

### Corrections of approved, unbilled time

An approved timesheet and its entries are locked (`TIME_LOCKED`), so a mistake in approved time
cannot be edited. A submitted sheet is returned by the approver with a reason
(`POST /timesheets/{id}/reject`), an open or returned one is edited. **Approved time that has not
been billed is corrected** with `POST /time-entries/{entry_id}/correct`:

```json
{
  "reason": "Hours were mis-keyed",
  "minutes": 90,
  "date": "2026-09-08",
  "job_id": "…",
  "description": "…",
  "internal_note": "…",
  "billable": true
}
```

* **Who**: `time.manage_all` or `timesheet.approve` (403 `PERMISSION_DENIED`). Nobody corrects their
  own approved time unless they hold `organization.update` (403 `SELF_CORRECTION_FORBIDDEN`), like the
  self-approval rule of timesheets.
* **When**: only an entry whose timesheet is `approved` and whose `billing_status` is `unbilled`; any
  other state is 409 `TIME_NOT_CORRECTABLE` (`details.timesheet_status`, `details.billing_status`).
* **`reason`** is required, 1-2000 characters, trimmed: missing or blank answers 422
  `TIME_CORRECTION_REASON_REQUIRED` (`details[0].field` = `reason`); more than 2000 is a plain
  422 `VALIDATION_ERROR`.
* **Fields**: any of `date`, `minutes`, `client_id`, `job_id`, `activity_code_id`, `description`,
  `billable`, `internal_note` (`entry_date` and `is_billable` are accepted for `date` and `billable`;
  `client_id`, `job_id`, `activity_code_id`, `description` and `internal_note` accept `null` to clear
  them; `date` and `minutes` do not). Sending none, or only
  values the entry already has, answers 422 `TIME_CORRECTION_EMPTY`; nothing is written then.
* **Rules of an edit**: minutes are rounded up to the practice increment; at most 24 hours a day;
  `JOB_CLOSED` / `CLIENT_INACTIVE` when the client, job, activity, minutes or date change (a
  description-only correction is allowed on a closed job); an activity code outside the job's service's
  list, time after the leaving date, a job of another client, billable time without a client or on a
  non-billable job are 422s; unknown or other practices' references are 404; `is_billable` is
  re-defaulted when the client, job or activity changes without it. A locked period refuses the
  correction when the entry's date **or** the new date lies in it (409 `PERIOD_LOCKED`).
* **Rate**: kept as saved unless the client, job, **date** or activity changes, in which case it is
  taken again from the rate hierarchy for the new values; the charge and cost values follow the minutes
  (`minutes x rate / 60`, half up). `rate_source` is kept or re-taken with the rate.
* **Moving into another week**: the entry joins the timesheet of its new week (created open when
  missing) **whatever that sheet's status** and from then on follows it (an open sheet is edited, not
  corrected). The totals of both sheets are computed from their entries, so both stay consistent.
* The answer is the corrected `TimeEntryRead` (200). In one transaction one history row and one
  audit entry `time_entry.corrected` are written: the audit meta holds `reason`, `history_id`,
  `client_name` and `changes` `{field: [before, after]}` (the note only as booleans).

`GET /time-entries/{entry_id}/history` (the person, their manager or `time.read_all`; 403 otherwise;
unknown or other practices' entries 404; cursor-paginated, newest first) lists the corrections:

```json
{
  "id": "…", "time_entry_id": "…",
  "user_id": "…", "user_name": "Pat Person",
  "corrected_by": "…", "corrected_by_name": "Mia Manager",
  "reason": "Hours were mis-keyed",
  "before": {"entry_date": "2026-09-07", "minutes": 60, "charge_value": 10000, "week_start": "2026-09-07"},
  "after": {"entry_date": "2026-09-07", "minutes": 90, "charge_value": 15000, "week_start": "2026-09-07"},
  "changes": {"minutes": [60, 90], "charge_value": [10000, 15000]},
  "created_at": "2026-09-16T10:00:00Z"
}
```

`before` and `after` are complete snapshots (`entry_date, minutes, client_id, job_id,
activity_code_id, description, internal_note, is_billable, charge_rate, cost_rate, charge_value,
cost_value, rate_source, timesheet_id, week_start` and the names of that moment `client_name,
job_number, job_name, activity_code`); `changes` lists the keys whose value differs. `cost_rate` and
`cost_value` are `null` without `rate.read`, `internal_note` is `null` unless the reader may see
notes, and `changes` is worked out after that, so nothing hidden leaks through it. The history is
**append-only** (a trigger refuses every UPDATE and DELETE) and stays readable even if the entry itself
is deleted later (the rows keep no foreign key to the entry).

### Missing timesheets and reminders

`GET /timesheets/missing?week_start=&user_id=&manager_user_id=` lists the staff whose timesheet for a
week is not submitted, sorted by name (a plain list, no paging). `week_start` is any day of the week
(default: the **last completed week**, the week before the current one in the practice). Access: everyone
with `time.read_all`; otherwise the people the caller manages (an empty list when they manage nobody);
`user_id` (`me` or a person: 403 unless own, managed or `time.read_all`, 404 unknown) and
`manager_user_id` (the people that manager approves; anyone else's needs `time.read_all`) narrow it.

```json
{
  "user_id": "…", "name": "Ann Adams", "week_start": "2026-09-07",
  "status": "none", "timesheet_id": null,
  "logged_minutes": 0, "expected_minutes": 2250,
  "missing_days": ["2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"],
  "last_reminded_at": null, "has_login": true
}
```

* **Who must record time**: active staff (`status` `active`) with a weekly capacity above 0 who are
  employed on at least one working day of the week (start and leaving dates, working weekdays). Staff
  with a capacity of 0 (people who do not record time), inactive and left staff, and people who have
  not started yet are never listed. A member with no start date is expected for every week.
* `status`: `none` (no timesheet exists for the week), `open` (it exists and is not submitted) or
  `rejected` (returned and not yet resubmitted). People whose sheet is `submitted` or `approved` are not
  missing and are left out.
* `expected_minutes` is the person's target for that week (see the rule above); `logged_minutes` all
  minutes dated in the week; `missing_days` their working days up to today with no time; `has_login`
  is the member's `login_enabled` (false: they cannot sign in, so they cannot be reminded);
  `last_reminded_at` is when a timesheet reminder for that week was last sent.

Other modules reuse the same function (the Dashboard widget and the Missing Timesheets report), so
the figures always agree:

```python
from app.services.timesheet_missing import MissingScope, missing_timesheets


async def my_team(session, organization_id, week_start, me):
    scope = MissingScope(manager_user_id=me)
    return await missing_timesheets(session, organization_id, week_start, scope)
```

`missing_timesheets(session, organization_id, week_start, scope=None, *, organization=None)`
returns `list[MissingTimesheet]` (`app/schemas/time_views.py`); `MissingScope(user_ids=None,
manager_user_id=None)` narrows to some people (`None`: everyone), `last_completed_week(organization)` is the
default week and `organization=` skips the lookup of the practice when the caller has it.

`POST /timesheets/reminders/run` with an optional body `{"week_start": "2026-09-07"}` (any day of the
week; default the last completed week) sends the reminders and answers 200 with the counts:

```json
{
  "week_start": "2026-09-07", "missing": 3, "reminded": 2, "already_reminded": 1, "unreachable": 0,
  "sheets_waiting": 2, "approvers_reminded": 2, "approvers_already_reminded": 0, "disabled": []
}
```

* Needs `organization.update` or `timesheet.approve` (403). With `organization.update` or
  `time.read_all` it covers the whole practice; with only `timesheet.approve`, the people the caller manages.
* **Staff**: everyone in the missing list who can be notified (an active member with a login) gets the
  *timesheet reminder* notification ("Timesheet reminder", link `/time`). The others are counted in
  `unreachable`.
* **Approvers**: every approver of a `submitted` timesheet of that week or an earlier week gets one
  summary *approval pending* notification ("Timesheets waiting for approval", link `/time/approvals`:
  how many sheets, from whom, the oldest week). The approvers of a sheet are the person's manager when
  the manager holds `timesheet.approve`, otherwise everyone who does (never the person themselves), as
  for the notice sent on submission.
* **Once per person and week**: each reminder is recorded in `timesheet_reminders` (one row per person,
  week and kind `timesheet` / `approval`; a unique key makes two runs at the same moment safe), so a
  second run only reaches people not reminded about that week yet (`already_reminded`,
  `approvers_already_reminded`). A new week is a new reminder. People who have submitted since drop out
  of the list. A reminder that could not be raised is not recorded, so a later run retries it.
* The practice's notification switches apply: a type the practice has switched off is skipped, not
  recorded, and named in `disabled` (`timesheet_reminder`, `approval_pending`); each
  person's own notification preferences decide how they hear of it. The emails use the practice's
  templates of those types (for an approver summary `submitter_name` is "3 staff members" or the one
  person's name, `week_start` the oldest week, `total_hours` the hours of those sheets).
* **Scheduled**: the worker (`app/workers/handlers.py`, job `send_timesheet_reminders`, registered in
  `PERIODIC` with a period of 86,400 seconds, so the first worker tick of each UTC day queues it) runs
  the same service for every practice and the last completed week; the once-per-week rule makes the
  repeat runs harmless. A practice that fails is logged and skipped; the others still run. The endpoint
  is the manual trigger.
* Audit: `timesheet.reminders_sent` (the counts and `source` = `manual` or `scheduler`); a scheduled run
  is only audited when it sent something.

### Rules in one place

* The 24-hour day, the daily cap, the closed-job rule, the time increment (6 minutes by default for a
  new practice), the editable-sheet rule and the period lock are the same for a new entry, a bulk entry,
  a timer stop, a copy and a correction.
* Period locks (`app/services/period_locks.py`, area `time`): a copy is created by the same code as any
  new entry, which checks the lock (a locked date is a skipped copy in `copy-week`, a 409 `PERIOD_LOCKED`
  for a single copy); a correction checks both the entry's date and the new date.
* Nothing here adds a permission: `time.log`, `time.read_all`, `time.manage_all`, `timesheet.approve`
  and `organization.update` decide everything.

## Billable WIP time (for billing, WIP and reports)

`app/services/time_entries.py:wip_eligibility_clause(organization)` returns a SQLAlchemy
condition over `time_entries` (no join needed):

```
is_billable AND billing_status = 'unbilled'
  [AND EXISTS (SELECT 1 FROM timesheets t WHERE t.id = time_entries.timesheet_id
               AND t.status = 'approved')]   -- only when require_timesheet_approval
```

The repository form is `app/repositories/time_entries.py:wip_time_clause(require_approval)`.

## Client status check

`app/services/time_rules.py:ensure_client_accepts_work(session, client)` checks the status
of the already-loaded client (`prospect`/`active`, else 409 `CLIENT_INACTIVE`) and then
delegates to `app/services/clients.py:ensure_client_accepts_work`, which re-checks it under
a `FOR KEY SHARE` lock so a concurrent archive waits for the job/time/timer transaction.
It is used by job create, time create/update and timer start.

## Schema

The reserved migration `008_jobs_and_time_changes.py` is still a no-op. The time recording gaps added
two migrations:

* **`022_time_entry_internal_note.py`**: `time_entries.internal_note` (`TEXT`, null) and the check
  `ck_time_entries_internal_note_length` (`internal_note IS NULL OR length(internal_note) <= 2000`).
* **`023_time_corrections_and_reminders.py`**:
  * `time_entry_history`: `id`, `organization_id` (foreign key to `organizations`, `ON DELETE CASCADE`),
    `time_entry_id`, `user_id` (whose time it is), `corrected_by`, `reason`, `before` and `after`
    (`JSONB` snapshots), `created_at` (default `clock_timestamp()`, so corrections made in one
    transaction still sort in the order they were written). Checks: the trimmed reason is 1-2000
    characters (`ck_time_entry_history_reason_length`) and both snapshots are JSON objects
    (`ck_time_entry_history_snapshots_are_objects`). Unique `(organization_id, id)`; index
    `(organization_id, time_entry_id, created_at)`.
  * The trigger `trg_time_entry_history_append_only` (function `prevent_time_entry_history_change()`)
    refuses every UPDATE and every DELETE that somebody typed (`restrict_violation`, "time_entry_history
    is append-only"). The practice's own deletion still removes its rows (the cascade runs one trigger
    level deeper: `pg_trigger_depth() > 1`, as for `period_lock_events`) and so does `TRUNCATE` (the
    maintenance reset and the tests).
  * `timesheet_reminders`: `id`, `organization_id` (cascade), `user_id`, `week_start`, `kind`
    (`timesheet` or `approval`, `ck_timesheet_reminders_kind`), `reminded_at` (default `now()`); unique
    `(organization_id, user_id, week_start, kind)`, which is the once-per-person-and-week guarantee,
    and an index on `(organization_id, week_start, kind)`.
  * Both tables name people and entries **without foreign keys** (like `rate_changes`): the history
    outlives a deleted entry, and neither table can block deleting or merging staff through the
    staff-reference check.

## Time that needs no approval

`organizations.auto_approve_privileged_time` (on by default; `PATCH /organizations/{id}`) and the
permission `timesheet.exempt` (Owner, Admin and Partner hold it; give it to a manager or a custom
role to include them) decide whose time needs no approval. When a practice requires timesheet approval
(`require_timesheet_approval`), a person who holds the permission has `timesheets.approval_exempt =
true` on their sheets and:

- their unbilled billable time **counts as approved** for WIP, billing, client and job figures and the
  reports (`wip_time_clause`, `sheet_counts_as_approved` in `app/repositories/timesheets.py`), whether or
  not the week was submitted;
- their time stays editable until they submit; **submitting** the week approves it at once
  (`decision_note` "Approval not required") and nobody is asked: it never reaches `/approvals`;
- their open weeks are not listed as "unsubmitted" on the dashboards.

The flag is kept in step by `TimesheetRepository.sync_exemptions`: when time is written, and when the
setting, a member's role or a role's permissions change. Turning the setting off makes the existing weeks
need approval again. Tests: `tests/test_time_approval_exempt.py`.
