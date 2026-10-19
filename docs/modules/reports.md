# Reports and dashboard

Read-only aggregates over the time, WIP, billing and payment tables. Repositories read the
tables directly (every query is filtered by `organization_id`), so reports never depend on
the services of other modules. Rules come from [domain.md](../domain.md#reports); the
decisions below fill the gaps.

## Endpoints

All under `/api/v1/organizations/{organization_id}`. Each report returns
`{rows, totals, filters, page}` (`filters` are echoed with defaults filled in; `page` says
which window of the rows came back; WIP has no `page`) and downloads as a file with
`?format=csv|xlsx|pdf` (WIP: `csv` only). Code: `app/api/v1/reports.py` →
`app/services/{reports,reports_ext,report_export,report_csv,report_paging}.py` →
`app/repositories/{reports,reports_ext}.py`; schemas `app/schemas/{reports,reports_ext}.py`.
Tests: `tests/test_reports_*.py` (the first eight reports) and `tests/test_reports_v2_*.py`
(paging, drill-down, downloads, expenses, payments, missing timesheets, consistency; helpers in
`tests/report_helpers.py`).

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/reports/wip` | `report.financial` | Unbilled WIP per client and job as at a date, aged 0–30 / 31–60 / 61–90 / 91+ days |
| GET | `/reports/aged-debtors` | `report.financial` | Outstanding balance per client as at a date: current, 1–30, 31–60, 61–90, 91+ days past due |
| GET | `/reports/time-summary` | `report.read` | Chargeable / non-chargeable minutes by `group_by` = person, client, job, activity, service, day, week or month |
| GET | `/reports/utilisation` | `report.read` | Per person: capacity, total and chargeable minutes, utilisation and target (basis points) |
| GET | `/reports/recoverability` | `report.financial` | WIP billed, billed value, write-ups and write-offs, recovery per client, job or person |
| GET | `/reports/billing-summary` | `report.financial` | Invoices, credit notes and receipts per month or per partner |
| GET | `/reports/job-profitability` | `report.financial` | Per job: time, cost, billed, written off, margin, budget use (cost and margin need `rate.read`) |
| GET | `/reports/vat-summary` | `report.financial` | Output VAT by rate: invoices issued minus credit notes |
| GET | `/reports/expenses` | `report.read` or `report.financial` | Expense lines by type, person, client, job, month or claim status: net, VAT, gross, billable, recharged, to reimburse. Everyone's with `expense.read_all`, else one's own |
| GET | `/reports/payments` | `report.financial` | Payments received by month, client or method: received, allocated, on account, voided or reversed |
| GET | `/reports/missing-timesheets` | `report.read` | Staff with no submitted timesheet, per person and week over a range of weeks: logged against expected, state, reminders |
| GET | `/dashboard` | any member | The caller's week, timer, unsubmitted weeks, jobs due; approvals waiting; practice figures with `report.financial` |

The ten reports of the spec (Featurte.md 2.17) are: Time Analysis = `time-summary`, Utilisation =
`utilisation`, Job Profitability = `job-profitability`, WIP & Ageing = `wip` + `aged-debtors`,
Recovery = `recoverability`, Billing = `billing-summary`, Expenses = `expenses`, Aged Debtors =
`aged-debtors`, Payments = `payments`, Missing Timesheets = `missing-timesheets`. The VAT summary
is an extra.

Common query parameters: `date_from`, `date_to` (or `as_at` for WIP and aged debtors; weeks
for missing timesheets), `client_id`, `job_id`, `user_id`, `partner_user_id`, `manager_user_id`,
`service_id`, `activity_code_id` where relevant, `group_by` where listed. Unknown parameters
answer 422. Every report but WIP also takes:

| Parameter | Meaning |
|---|---|
| `format` | `json` (default), `csv`, `xlsx` or `pdf`: a file holds every row and needs `report.export` ([Downloads](#downloads)). WIP: `json` or `csv` |
| `limit` | 1–1000 rows per page of the JSON answer. Default: every row |
| `offset` | Rows to skip (0 default); `page.next_offset` gives the next one |

## Paging

The whole report is worked out and a window of its rows is returned, so `totals` always cover
every row. `page` is `{total, limit, offset, has_more, next_offset}`: `total` the rows of the
whole report, `limit` what was asked (`null`: every row), `next_offset` the `offset` of the next
page (`null` on the last). A page past the end is empty (200). Rows come in a fixed, documented
order, so pages never overlap or skip a row between requests:

| Report | Order |
|---|---|
| aged-debtors | client name, code, id |
| time-summary | person, client, job, activity, service: group name (empty last), code, id; day, week, month: date |
| utilisation | name, user id |
| recoverability | group name (empty last), the job's client, code, id |
| billing-summary | month: date; partner: name, "No partner" last |
| job-profitability | client name, client code, job number |
| vat-summary | rate (highest first), name |
| expenses | type, person, client, job: name (empty last), the job's client, code, id; month: date; status: draft, submitted, approved, returned |
| payments | month: date; client: name; method: method code |
| missing-timesheets | week (newest first), person name, user id |

## Downloads

`?format=csv`, `xlsx` or `pdf` on every report except WIP (the WIP report answers CSV only) sends the
whole report as an attachment (`Content-Disposition: attachment; filename="<report>_<date or
range>.<ext>"`), whatever `limit` and `offset` say. The three formats share one definition of
columns (`report_csv.columns_of`) and one row of totals.

* **Permission**: the report's own permission **and** `report.export` (Owner, Admin, Partner,
  Manager and Billing hold it; Staff and Viewer do not). Without it: 403 `PERMISSION_DENIED`
  for every file format; the JSON answer needs the report's permission only. *Change from V1:
  `?format=csv` used to need no more than the report permission.*
* **CSV**: UTF-8 with BOM, CRLF, a header row, the rows and a final Total row. Money in decimal
  pounds (`1234.56`), hours to 2 dp, percentages to 2 dp.
* **Excel** (`xlsx_export.render_xlsx`): one sheet named after the report with the title (report
  and practice) and the filters in words on top, a bold frozen filtered header row, real
  numbers (pounds with a `£` format, hours to 2 dp, percentages, `dd/mm/yyyy` dates) and a bold
  Total row, so the sheet can be summed and pivoted.
* **PDF** (fpdf2): the title, the practice, the filters in words (`Period: 01/09/2026 to
  30/09/2026`, `Grouped by: client`, `Client: Acme Ltd (ACME)`), the table with `£1,234.56`, `h:mm`
  and `dd/mm/yyyy`, the headings repeated on every page, the Total row, "Page n of m" and the
  date it was generated. Tables of more than seven columns are printed landscape. The pages are
  uncompressed (greppable). Text is Latin-1 (typographic quotes and dashes become plain ones,
  other characters `?`).
* **Masking**: without `rate.read` the cost and margin columns are blank in every format.
* **Formula injection**: a text cell starting with `= + - @` gets a leading apostrophe in CSV
  and Excel.
* **Audit**: every download writes `report.exported` (`target_type` `report`) with the caller, IP
  address and meta `{report, format, filters, row_count}`; never the rows. JSON answers are not
  audited.

## Drill-down

Rows carry the ids of their source records (and, for month, week and day rows, the days they
cover, cut to the report's range), so a screen can open the records behind a number. The fields
are named like the filters of the module endpoints:

| Report, grouping | Row fields | Records behind it |
|---|---|---|
| time-summary person / client / job / activity / service | `user_id` / `client_id` / `job_id` + `client_id` / `activity_code_id` / `service_id` | `GET /time-entries?user_id=&client_id=&job_id=&date_from=&date_to=` (activity and service: the Time Analysis page) |
| time-summary day / week / month | `date_from`, `date_to` | `GET /time-entries?date_from=&date_to=` plus the report's filters |
| utilisation | `user_id` | `GET /time-entries?user_id=&date_from=&date_to=` |
| job-profitability | `job_id`, `client_id` | `GET /jobs/{job_id}`, `GET /time-entries?job_id=` |
| recoverability client / job / person | `client_id` / `job_id` + `client_id` / `user_id` | the client's or job's WIP and invoices |
| billing-summary month | `date_from`, `date_to` | `GET /invoices?date_from=&date_to=`, `GET /payments?date_from=&date_to=` |
| billing-summary partner | `partner_user_id` | the partner's clients |
| aged-debtors | `client_id` | `GET /invoices?client_id=&outstanding=true` |
| wip | `client_id`, `job_id` | `GET /clients/{client_id}/wip?job_id=` |
| expenses type / person / client / job / status | `expense_type_id` / `user_id` / `client_id` / `job_id` + `client_id` / `claim_status` | `GET /expenses?expense_type_id=&user_id=&client_id=&job_id=&status=&date_from=&date_to=` |
| expenses month | `date_from`, `date_to` | `GET /expenses?date_from=&date_to=` |
| payments client / method / month | `client_id` / `method` / `date_from`, `date_to` | `GET /payments?client_id=&method=&date_from=&date_to=` |
| missing-timesheets | `user_id`, `timesheet_id`, `week_start` | `GET /time-entries/week?user_id=&week_start=`, `GET /timesheets/{timesheet_id}` |

## Charts

There is no separate chart contract: a chart is drawn from `rows` (and `totals`), and the figures
are the same as the table's. The Reports pages use, for instance: time-summary by `month` or
`week` (x = `name`, y = `total_minutes` and `chargeable_minutes`), by `client` (the largest
`charge_value`), utilisation (`utilisation_bp` against `target_bp` per person), billing-summary
by `month` (`net_billed` and `receipts`), aged-debtors (the five buckets of `totals`), expenses by
`type` (`gross`) or `month`, payments by `month` (`allocated` and `on_account`). Fetch the whole
report (no `limit`) for a chart.

## The three reports of wave 2

### GET /reports/expenses

Expense lines (`docs/modules/expenses.md`) whose `incurred_on` falls in `date_from`..`date_to`
(default: this month). `group_by`: `type` (default), `person` (the claimant), `client`, `job`,
`month` (every month of the range, empty ones included) or `status` (the claim's status). Filters,
named as in `GET /expenses`: `status` (claim status, repeat the parameter; default: every status,
drafts included), `client_id`, `job_id`, `user_id` (the claimant), `expense_type_id`,
`is_billable`, `billing_status` (repeat).

* **Who**: `report.read` or `report.financial`. Claims of everyone with `expense.read_all` or
  `expense.manage`; otherwise one's own and, with `expense.approve`, those of the people one
  manages (as `GET /expense-claims`). Someone without any expense permission (the Viewer) gets an
  empty report, not a 403. `user_id` of a person outside that scope: 403.
* Row: `{key, code, name, client_name, expense_type_id, user_id, client_id, job_id,
  claim_status, date_from, date_to}` (the id fields of the grouping; the others `null`) and the
  measures below. A line without a client is "No client (overhead)" (`group_by=client`), one without
  a job "No job".
* Measures (pence): `line_count`, `net`, `vat`, `gross`, `billable_net` (net of the lines billable to
  a client), `recharge_amount` (what the clients are charged: net plus markup, or the amount a
  manager set), `recharged_amount` (the part already invoiced: billing status `billed`),
  `reimbursable` (gross of the lines the claimant paid, `payer = staff`). `totals` hold the same
  eight measures; every one is additive.
* Figures equal `GET /expenses` for the same filters: the sum of its `net`, `vat`, `gross`,
  `recharge_amount` and `line_count`.

### GET /reports/payments

Payments whose `received_on` falls in the range (default: this month). `group_by`: `month` (default,
every month of the range), `client` or `method` (`bank_transfer`, `card`, `direct_debit`, `cash`,
`cheque`, `other`). Filters: `client_id`, `method`, `partner_user_id`, `manager_user_id` (the
client's). Needs `report.financial`.

* Row: `{key, code, name, client_id, method, date_from, date_to}` and the measures: `payment_count`
  and `received` (payments that count: not voided, and not reversed once payments can be
  reversed), `allocated` (their `allocated_total` as it stands now), `on_account` (`received −
  allocated`), `void_count` and `void_amount` (payments received in the range that were voided or
  reversed; they are not part of `received`).
* `received` equals the `receipts` of the billing summary and the amounts `GET /payments` lists
  (without the `void` ones) for the same range and client.

### GET /reports/missing-timesheets

One row per person and week for the weeks from `week_from` to `week_to` (each any day of its week;
default: the last completed week, the week before the current one; with only `week_from`: up to the
last completed week, or that one week when it is later; with only `week_to`: that one week). At
most **26 weeks**, none that has not started (the current week is allowed).
`user_id`, `manager_user_id` and `status` (`none`, `open`, `rejected`; repeat) narrow it. Needs
`report.read`.

* **Who** is listed is decided by the same function and rules as `GET /timesheets/missing`
  (`timesheet_missing.missing_timesheets`, `docs/modules/time.md`): active staff with a weekly
  capacity above 0 employed on a working day of the week whose timesheet is not submitted or
  approved. Everyone with `time.read_all` sees all of them; otherwise the people the caller
  manages (nobody: an empty list; `user_id` of anyone else: 403, one's own: allowed).
* Row: `{user_id, name, week_start, status, timesheet_id, logged_minutes, expected_minutes,
  shortfall_minutes, missing_day_count, missing_days, last_reminded_at, has_login}`:
  `shortfall_minutes = max(expected − logged, 0)`; the other fields are the screen's.
* `totals`: `timesheets` (rows), `people` (different people), `weeks`, `none_count`, `open_count`,
  `rejected_count`, `logged_minutes`, `expected_minutes`, `shortfall_minutes`, `missing_day_count`.
* `filters` also echo `week_from`, `week_to` (the `week_start` of the first and last week) and
  `status`; `date_from`/`date_to` are the first and last day covered.

## Errors

| Status | Code | When |
|---|---|---|
| 403 | `PERMISSION_DENIED` | Missing `report.read` / `report.financial`; a download (`format` csv, xlsx or pdf) without `report.export`; another person's `user_id` without `time.read_all` (time reports and missing timesheets: unless managed) or without `expense.read_all` (expenses: unless one's own or managed) |
| 404 | `RESOURCE_NOT_FOUND` | Not a member, or a filter id of another practice (client, job, person, service, activity code, expense type) |
| 422 | `VALIDATION_ERROR` | `date_from` after `date_to` (field `date_from`); a range of 10 years or more (field `date_to`, code `range_too_long`); `week_from` after `week_to`; a range of more than 26 weeks (`week_to`, `range_too_long`); a week that has not started (`week_to`, `future_week`); `limit` outside 1–1000, `offset` below 0, `format` not json/csv/xlsx/pdf; an invalid value of a filter; an unknown parameter |

## Rule decisions

* "Today" and default dates are in the practice time zone. `date_to` defaults to today,
  `date_from` to the first of that month; job profitability defaults to all time.
* **WIP as at a date** counts eligible items dated on or before `as_at` that were still
  unbilled at the end of that day: an item billed or written off after `as_at` counts as
  WIP at `as_at`. Items are aged from `as_at`.
* **Aged debtors as at a date**: for issued invoices dated on or before it, balance = gross
  − allocations of payments received by that date − credit notes issued by that date.
  Today this equals `gross − paid_total − credited_total`. "Current" is not yet due (due
  today is current). Void invoices and voided payments are left out entirely, including
  for dates before the void.
* Partner and manager filters on WIP, time summary, recoverability and job profitability use
  the job's partner/manager, falling back to the client's; aged debtors and the billing
  summary use the client's.
* `user_id` and `activity_code_id` match time only, so they leave disbursements out.
* "Chargeable" means `is_billable`.
* Without `time.read_all`, time summary and utilisation cover only the caller's own time.
  Cost and margin are `null` without `rate.read`.
* Utilisation capacity = round half up(weekly capacity × working days in the range ÷
  working days per week). It lists members who are not removed, plus removed members who
  logged time in the range. The totals' target is the capacity-weighted average.
* Recoverability = billed value ÷ (WIP value billed + WIP written off), from live
  allocations (`reversed_at IS NULL`) to issued invoices and write-offs dated in the range.
* Billing summary receipts are the full amount of non-voided payments, including money on
  account. The month view lists every month in the range, empty ones included.
* Job profitability margin = billed time − cost of time; disbursement recharges are left
  out. A fixed-fee line with no WIP items under it does not count as billed for the job.
* The VAT summary groups by the rate snapshot stored on each line (`vat_rate_bp`).
* The aged-debtors rows and totals carry `days_90_plus`, the spec's `90+` bucket: the same figure
  as `days_91_plus` (more than 90 days past due), which stays.
* **Figures equal the main screens** (2.17), for the same filters: time summary and utilisation
  against `GET /time-entries`, billing summary and VAT summary against `GET /invoices`, aged
  debtors against the outstanding invoices, payments against `GET /payments` and the billing
  summary's receipts, expenses against `GET /expenses`, missing timesheets against `GET
  /timesheets/missing`, WIP against `GET /wip`. `tests/test_reports_v2_consistency.py` and the
  tests of each report assert it.
* Dashboard: `approvals_waiting` appears with `timesheet.approve` or `report.financial`
  (all submitted sheets with `time.read_all`, otherwise those of the people the caller
  manages). `billed_this_month` / `billed_last_month` are net of invoices issued, before
  credit notes. `jobs_due` includes overdue open jobs the caller manages or is assigned to.

## Known limits

* Reports are computed in full and then windowed (see [Paging](#paging)); size is bounded by
  the date range (under 10 years; 26 weeks for missing timesheets) and the filters.
* Payments that are reversed (payment reversal of the billing module) count as void in the
  payments report and in the billing summary's receipts as soon as `payments.reversed_at`
  exists (`live_payment()` in `repositories/reports.py`).
* A report runs a few read queries without a shared snapshot, so a write committed between
  them can make its parts disagree slightly. Run month-end reports after billing has closed.
