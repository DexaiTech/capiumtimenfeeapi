# Dashboard pages: cards, My Actions and the widgets

The eight pages of the Dashboard menu (Overview, My Actions, My Timesheet, Pending Approvals,
Missing Timesheets, Top Clients by WIP, Billing Trend, Debtor Ageing) are built from the endpoints
below. They are additive: `GET /dashboard` (see [reports.md](reports.md)) is unchanged and still
serves the headline figures of the earlier screens.

Every figure comes from the database, scoped to the practice, and is worked out with the same
definitions as the screen it summarises: WIP as the WIP report counts it, debtors as the aged
debtors report counts them, billing as the billing summary counts it, missing timesheets as
`GET /timesheets/missing` lists them. `tests/test_dashboard_pages_widgets.py` compares them.

## Files

| Layer | Files |
|---|---|
| Router | `app/api/v1/dashboard_pages.py` (prefix `/organizations/{organization_id}/dashboard`) |
| Schemas | `app/schemas/dashboard_pages.py` |
| Services | `app/services/dashboard_common.py` (periods, comparisons, who sees what), `dashboard_cards.py`, `dashboard_actions.py`, `dashboard_approvals.py`, `dashboard_missing.py`, `dashboard_widgets.py` (My Timesheet, Pending Approvals, Missing Timesheets, Top Clients, Billing Trend, Debtor Ageing) |
| Repository | `app/repositories/dashboard_pages.py`; the headline queries of `app/repositories/dashboard.py` and the WIP, debtor and billing queries of `app/repositories/reports.py` are reused |
| Tests | `tests/test_dashboard_pages_cards.py`, `test_dashboard_pages_actions.py`, `test_dashboard_pages_widgets.py`, `test_dashboard_pages_openapi.py`, `test_dashboard_periods.py`, helpers `tests/dashboard_page_helpers.py` |

No migration and no new permission: the cards and widgets read what other modules own.

## Endpoints

All under `/api/v1/organizations/{organization_id}/dashboard`, any member of the practice; the
permissions below decide what each widget shows. Money is pence, durations whole minutes, dates
ISO in the practice time zone, timestamps UTC.

| Method | Path | Shows with | Purpose |
|---|---|---|---|
| GET | `/cards?period=` | per card, below | The six summary cards for a period |
| GET | `/my-actions?limit=` | any member | The caller's actions, most urgent first |
| GET | `/my-timesheet` | `time.log` | The current week against the target, the latest entries, the timer |
| GET | `/pending-approvals?kind=&limit=` | `timesheet.approve` or `expense.approve` | One approval queue: counts per kind and the oldest items |
| GET | `/missing-timesheets?week_start=` | `time.read_all` or `timesheet.approve` | Staff whose timesheet for a week is not submitted |
| GET | `/top-clients-by-wip?limit=&period=` | `wip.read` or `report.financial` | The clients with the most unbilled WIP |
| GET | `/billing-trend?months=` | `invoice.read` or `report.financial` | Billed, credited and received per month |
| GET | `/debtor-ageing?limit=&period=` | `payment.read`, `invoice.read` or `report.financial` | Outstanding balance by age, and the biggest debtors |

**A widget never answers 403.** Without the permissions behind it the response is 200 with
`visible: false` and every other field empty (null, 0 or `[]`; cards keep their place in the list).
That is what lets a page load, refresh and fail one widget at a time. The only errors are the usual
ones: 401, 404 (not a member of the practice, or another practice's id) and 422 (a query value out of
range). Every widget also carries `as_at` (today in the practice time zone) and `generated_at`
(UTC), the "last updated" stamp of the UI.

Each widget is a fixed number of queries whatever the amount of data (tested).

## Periods

`period` is `this_week` (from the practice's first weekday), `this_month` or `ytd` (the calendar
year), always up to today. The window is `[date_from, today]`; the previous window has the same
number of days a period earlier (the same days of last week, of last month, of last year; the 31st
is compared with the end of a shorter month, 29 February with 28 February).

| Period | `date_from` | `previous_from` .. `previous_to` (today 17/10/2026) |
|---|---|---|
| `this_week` | first weekday on or before today (12/10) | 05/10 .. 10/10 |
| `this_month` | the 1st (01/10) | 01/09 .. 17/09 |
| `ytd` | 1 January (01/01) | 01/01/2025 .. 17/10/2025 |

## GET /cards

```json
{
  "period": "this_week", "date_from": "2026-10-12", "date_to": "2026-10-17",
  "previous_from": "2026-10-05", "previous_to": "2026-10-10",
  "as_at": "2026-10-17", "generated_at": "2026-10-16T23:30:00Z",
  "cards": [
    {
      "key": "my_hours", "title": "My Hours This Week", "visible": true, "unit": "minutes", "value": 120,
      "comparison": {"basis": "previous_period", "previous": 240, "change": -120, "change_bp": -5000},
      "secondary": {"key": "target", "value": 2250, "unit": "minutes"},
      "link": "/dashboard/my-timesheet"
    }
  ]
}
```

Always six cards in this order; a card the caller may not see stays with `visible: false`,
`value`, `comparison` and `secondary` null. `link` is a path inside the practice (below `/{slug}`).

| `key` | Title | Shown with | `value` | `comparison` | `secondary` |
|---|---|---|---|---|---|
| `my_hours` | My Hours This Week / Month / Year to Date | `time.log` | The caller's minutes in the window | `previous_period` | `target`: the caller's target for the whole week or month (year to date: up to today), from their working days, start and leaving dates |
| `missing_timesheets` | Missing Timesheets | `time.read_all` or `timesheet.approve` | Timesheets missing in the completed weeks of the window (see below) | none | `people`: distinct people |
| `pending_approvals` | Pending Approvals | an approve permission (below) | Everything waiting for the caller | none | none |
| `unbilled_wip` | Unbilled WIP | `wip.read` or `report.financial` | WIP as at today (time and disbursements, as the WIP report) | `period_start` | `over_90_days`: the part older than 90 days |
| `billed` | Billed This Week / Month / Year to Date | `invoice.read` or `report.financial` | Net of invoices issued in the window, before credit notes | `previous_period` | none |
| `overdue_debtors` | Overdue Debtors | `payment.read`, `invoice.read` or `report.financial` | Balance past its due date as at today | `period_start` | `total_outstanding` |

`comparison`: `basis` `previous_period` is the figure of the previous window; `period_start` is the
figure **as at the day before the period began** (for a figure that stands, such as WIP and
debtors, the change is its movement within the period; both are recomputed from dated records, so
nothing is stored). `change` is value minus previous, `change_bp` is `change` over `previous` in
basis points rounded half up (3681 = +36.81%), null when `previous` is 0.

**Missing timesheets in a window.** The completed weeks that overlap `[date_from, today]` (and
always the last completed week, which is the one due now) are examined with the rule of
`GET /timesheets/missing`: expected people (active, capacity above 0, employed in the week) with no
`submitted` or `approved` timesheet. `this_week` is therefore the last completed week only. The
scope is everyone's with `time.read_all`, otherwise the people the caller manages and the caller.
At most 60 weeks.

## GET /my-actions?limit=

`limit` 1-100, default 50. Answers `{visible, as_at, generated_at, total, counts: {high, medium, low},
actions: []}` (`visible` is always true: every member has actions); `total` and `counts` cover every
action, `actions` is cut at `limit`. Sorted by
priority, then by the kind order of the table, then by date (oldest first).

```json
{
  "id": "job_overdue:6f1e…", "kind": "job_overdue", "priority": "high", "title": "Job overdue",
  "subject": "J00002 Late", "context": "Beta LLP", "date": "2026-10-10", "days_until_due": -7,
  "count": 1, "amount": null, "usage_bp": null, "link": "/jobs/6f1e…"
}
```

| Priority | Kind (sort order) | Who gets it, and when | `count`, `date`, link |
|---|---|---|---|
| high | `timesheet_returned` | `time.log`; timesheets with time of past weeks that were rejected | how many, the oldest week, `/time` |
| high | `expense_returned` | claims of the caller that were returned | how many, the oldest return day, `/expenses/mine` |
| high | `task_overdue` | open tasks assigned to the caller, past their due date, on open jobs (at most 20) | 1, due date, `/jobs/{id}` |
| high | `job_overdue` | open jobs the caller manages or is assigned to, past their due date | 1, due date, `/jobs/{id}` |
| high | `budget_exceeded` | open jobs of the caller at or over 100% of a budget (`job_budget_alerts`), one action per job with the highest `usage_bp` | 1, `/jobs/{id}` |
| medium | `timesheet_to_submit` | `time.log`; timesheets with time of past weeks that are still open | how many, the oldest week, `/time` |
| medium | `timesheet_missing` | `time.log`, expected to record time, nothing logged in the last completed week | 1, that week, `/time` |
| medium | `timesheet_approvals` | `timesheet.approve`: submitted timesheets to decide on | how many, the oldest week, `/time/approvals` |
| medium | `expense_approvals` | `expense.approve`: submitted claims to decide on | how many, the oldest submission day, `/expenses/approvals` |
| medium | `invoice_approvals`, `wip_adjustment_approvals` | reserved for those modules (not produced yet) | |
| medium | `budget_warning` | open jobs at 80% (below 100%) of a budget | 1, `/jobs/{id}` |
| medium or low | `job_due_soon` | open jobs due within 14 days: medium up to 7 days, low after | 1, due date, `/jobs/{id}` |
| low | `draft_invoices` | `invoice.create`: draft invoices the caller prepared | how many, the oldest day, `amount` = their gross total, `/billing/invoices` |

## GET /my-timesheet

The current week from the practice's first weekday. `visible: false` without `time.log`.

```json
{
  "visible": true, "as_at": "2026-10-17", "generated_at": "…",
  "week_start": "2026-10-12", "week_end": "2026-10-18",
  "timesheet_id": "…", "timesheet_status": "open", "decision_note": null,
  "total_minutes": 120, "chargeable_minutes": 90, "non_chargeable_minutes": 30,
  "target_minutes": 2250, "target_to_date_minutes": 2250,
  "today_minutes": 0, "today_target_minutes": 0,
  "utilisation_bp": 400, "chargeable_target_bp": 7500,
  "days": [{"date": "2026-10-12", "minutes": 90, "billable_minutes": 90, "target_minutes": 450,
            "is_working_day": true, "is_missing": false}],
  "missing_days": ["2026-10-14", "2026-10-15", "2026-10-16"],
  "entry_count": 2,
  "entries": [{"id": "…", "entry_date": "2026-10-13", "minutes": 30, "client_id": null,
               "client_name": null, "job_id": null, "job_number": null, "job_name": null,
               "description": null, "is_billable": false, "billing_status": "unbilled"}],
  "timer": null
}
```

`timesheet_status` is `open` when the week has no sheet yet; `decision_note` is the reason of a
rejected sheet. `days` has seven entries; `is_missing` is a working day up to today with no time.
`utilisation_bp` is chargeable minutes over the week's target (null without a target). `entries` are
the latest 50, newest first (`entry_count` is the whole week). `timer` is the shape of
`GET /dashboard` (`DashboardTimer`). No rate or cost is returned.

## GET /pending-approvals?kind=&limit=

`kind` is `timesheet` or `expense` (omitted: all), `limit` 1-50, default 10.

```json
{
  "visible": true, "as_at": "…", "generated_at": "…", "total": 2,
  "kinds": [
    {"kind": "timesheet", "count": 1, "oldest_date": "2026-10-05", "link": "/time/approvals"},
    {"kind": "expense", "count": 1, "oldest_date": "2026-09-10", "link": "/expenses/approvals"}
  ],
  "items": [
    {"kind": "expense", "id": "…", "title": "Olly Other", "subject": "Train to Leeds",
     "date": "2026-09-10", "submitted_at": "2026-09-10T09:00:00Z", "minutes": null,
     "amount": 1200, "link": "/expenses/approvals"},
    {"kind": "timesheet", "id": "…", "title": "Sam Staff", "subject": null,
     "date": "2026-10-05", "submitted_at": "…", "minutes": 150, "amount": null,
     "link": "/time/approvals"}
  ]
}
```

* `kinds` lists the kinds the caller may decide (a count of 0 is listed); `total` adds them up
  whatever `kind` asks for. `visible` is false when the caller may decide none.
* **Timesheets** (`timesheet.approve`): submitted sheets of everyone with `time.read_all`, otherwise of
  the people the caller manages, as on the approvals screen. **Expense claims** (`expense.approve`):
  submitted claims of everyone with `expense.read_all`, otherwise of the people the caller manages.
  The caller's **own** are left out unless they hold `organization.update` (the screens refuse the
  self-approval otherwise).
* `items` are the oldest first by `date` (the week of a timesheet, the submission day of a claim);
  `title` is whose it is, `amount` the gross total of a claim, `minutes` the hours of a timesheet.
  Items are decided on the module's own endpoints (`POST /timesheets/{id}/approve`,
  `POST /expense-claims/{id}/approve`, returns need a reason there).
* Invoices awaiting approval and WIP adjustments join the queue when those modules have them: the
  kind is one entry in `LIVE_APPROVAL_KINDS` of `dashboard_common.py` and one query in
  `dashboard_approvals.py`.

## GET /missing-timesheets?week_start=

```json
{
  "visible": true, "scope": "practice", "as_at": "…", "generated_at": "…",
  "week_start": "2026-10-05", "week_end": "2026-10-11", "last_completed_week_start": "2026-10-05",
  "count": 2, "rows": [], "can_remind": true
}
```

`week_start` is any day of the week (default the last completed week; 1900-2999). `rows` are those
of `GET /timesheets/missing` (the same function, see [time.md](time.md#missing-timesheets-and-reminders)).
`scope` is `practice` (`time.read_all`) or `team` (with only `timesheet.approve`: the people the
caller manages and the caller). `can_remind` is true with `organization.update` or
`timesheet.approve`; the reminders are sent with `POST /timesheets/reminders/run`.

## GET /top-clients-by-wip?limit=&period=

`limit` 1-50 (default 10), `period` default `this_month`.

```json
{
  "visible": true, "period": "this_month", "date_from": "2026-10-01",
  "total": 22300, "previous_total": 12000, "client_count": 3, "others_amount": 0,
  "items": [{"client_id": "…", "client_code": "ALP", "client_name": "Alpha Ltd", "amount": 15000,
             "time_value": 15000, "disbursement_value": 0, "minutes": 75, "oldest_date": "2026-07-01",
             "over_90_days": 12000, "share_bp": 6726, "previous_amount": 12000, "change": 3000}]
}
```

WIP as at today, largest first (ties by name). `previous_amount`/`previous_total` are as at the day
before the period began. `others_amount` is the WIP of the clients not listed.

## GET /billing-trend?months=

`months` 1-24, default 12.

```json
{
  "visible": true, "months": 12,
  "series": [{"month": "2026-10-01", "billed": 1500, "invoice_count": 2, "credit_notes": 200,
              "credit_note_count": 1, "net_billed": 1300, "receipts": 1000}],
  "this_month": {"date_from": "2026-10-01", "date_to": "2026-10-17", "billed": 1500,
                 "credit_notes": 200, "net_billed": 1300, "receipts": 1000},
  "last_month": {"date_from": "2026-09-01", "date_to": "2026-09-17", "billed": 500,
                 "credit_notes": 0, "net_billed": 500, "receipts": 0},
  "billed_comparison": {"basis": "previous_period", "previous": 500, "change": 1000, "change_bp": 20000}
}
```

`series` runs from the oldest month to the current one, empty months included. `billed` is the net of
invoices issued in the month (by issue date, before credit notes), `credit_notes` the net of credit
notes issued, `receipts` the payments received that are not voided (money on account included): the
columns of `GET /reports/billing-summary?group_by=month`. `last_month` is the same number of days of
the month before as `this_month` has so far, so the two are comparable; `billed_comparison` equals
the `billed` card for `this_month`.

## GET /debtor-ageing?limit=&period=

`limit` 1-50 (default 10), `period` default `this_month`.

```json
{
  "visible": true, "period": "this_month", "date_from": "2026-10-01",
  "total": 4280, "overdue_total": 3080, "previous_total": 2720, "previous_overdue_total": 2120,
  "invoice_count": 5, "debtor_count": 3,
  "buckets": [{"key": "current", "amount": 1200, "invoice_count": 2, "share_bp": 2804}],
  "top_debtors": [{"client_id": "…", "client_code": "ALP", "client_name": "Alpha Ltd", "invoice_count": 3,
                   "total": 3560, "overdue": 2960, "oldest_due_date": "2026-08-31", "current": 600,
                   "days_1_30": 1200, "days_31_60": 1760, "days_61_90": 0, "days_90_plus": 0}]
}
```

The balance of issued invoices as at today (gross less payments received and credit notes issued
by then; void and draft invoices left out), the rules of the aged debtors report. `buckets` are
always the five of `current` (not yet due, due today included), `days_1_30`, `days_31_60`,
`days_61_90` and `days_90_plus` (more than 90 days past the due date: the report's `days_91_plus`).
`overdue_total` is everything but `current`. `previous_*` are as at the day before the period began.
`top_debtors` are ordered by the overdue balance, then the balance, then the name;
`oldest_due_date` is the oldest due date among the overdue invoices (null when none is).

## Rule decisions

* **Permissions** decide visibility, never a 403: the sets above are the ones of the Dashboard
  navigation of the UI (`src/constants/navigation.ts`).
* **Which clock.** "Today" is the practice's date; the reports and the time services read two clocks
  (`app.services.reports.utc_now`, `app.services.time_rules.practice_now`); the tests pin both
  (`tests/dashboard_page_helpers.pin_clock`).
* **Missing timesheets** are not stored: they are worked out from members, timesheets and time, so
  a person who has no start date is expected every week (as `GET /timesheets/missing` already does).
* **Own work only** for hours, actions and the timesheet; the approval queues follow the access
  rules of their modules; money widgets are practice-wide with their permission.

## Known limits

* Invoices awaiting approval, WIP adjustments awaiting approval and failed recurring invoice runs
  do not appear in Pending Approvals and My Actions yet: those modules have not reached the
  repository when this was written. The queue and the actions are built so that each is one more
  source (see above).
* The year is the calendar year; a practice with another financial year has no setting for it yet.
* `ytd` missing timesheets examine at most the last 60 completed weeks.
