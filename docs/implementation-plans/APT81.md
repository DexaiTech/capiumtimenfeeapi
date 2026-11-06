# APT81 – Staff overtime rates: implementation plan

Status: **Plan ready for review — no code changed. Waiting for approval, and for the client's
answers to §11 (some answers change the design).**
Investigated on `origin/main` of both repositories (API `197f534`, UI `5059d4b`) on 2026-11-06,
following `CLAUDE.md` → docs → graphify graph → source. Every claim below was checked in code.

## 1. Original requirement

Ticket APT81 (Medium), "Staff Overtime Rates". Staff can log overtime freely, with a clear warning.
Overtime goes through the standard time approval flow; its cost uses one overtime rate set per role
or per staff member. The cost appears only on a Staff Cost Report, with flags against jobs worked in
overtime hours; billing uses that report outside the system.

1. **Staff set-up**: hours basis (weekly / monthly) + standard hours, effective-dated; hours beyond
   the standard hours in the period are overtime; past periods change only by a deliberate
   recalculation.
2. **Overtime rates**: multiplier of the standard **cost** rate, or a fixed hourly amount, with
   effective dates; assigned per role or per staff member (staff overrides role); exactly one rate,
   never stacked; **cost only — never changes client charge-out**.
3. **Capture and approval**: same capture and approval flow; warning "You are adding overtime hours.
   Are you sure?" with the overtime hours, confirm/cancel; confirmation logged (user, date, time);
   approvers see overtime marked and can approve/reject/amend; status Pending / Approved / Rejected
   (pending reported as unapproved, rejected removed from costs); allocated (on a job) vs
   unallocated (no job); entries crossing the threshold split into standard + overtime portions.
4. **Staff Cost Report** (the only place overtime cost shows): per staff and period, standard and
   overtime hours and cost (allocated / unallocated), total; Overtime flag per job (hours, rate,
   cost, status); drill-down staff → jobs; filters staff, role, job, client, period, status, overtime
   type; **no overtime cost in invoices, WIP, job profitability or client views**.
5. **Access and audit**: visible only with an explicit permission; changes to hours, rates,
   assignments and approvals logged.

Acceptance example: 37.5 h/week, £20/h cost, role rate 1.5× → £30/h; 42 h logged incl. 3 h overtime
on Job X and 1.5 h with no job → warning on the crossing entry; standard £750; overtime £135 = £90
allocated (Job X) + £45 unallocated, Pending; Job X flagged; invoice and WIP unchanged; a staff rate
replaces the role rate.

## 2. Requirement interpretation

A new, **internal cost-only** overtime model replacing today's simple overtime switch: overtime is
*derived* from each person's dated standard hours (not ticked by hand), costed at one dated overtime
rate, approved per entry, and reported on one new permission-gated report. Client-facing values
(charge-out, WIP, invoices, profitability) must not move because of overtime.

## 3. Existing implementation (evidence)

### 3.1 Overtime already exists — and contradicts APT81

| Piece | Where | Today |
|---|---|---|
| Schema | `alembic/versions/061_overtime.py:26-40` (tickets APT40-48) | `organizations.overtime_enabled` (default off), `overtime_multiplier_bp` (default 15000 = 1.5×); `organization_members.overtime_multiplier_bp` (per staff); `time_entries.is_overtime` |
| Pricing | `app/services/charge_rates.py:396-399` (`RateBook.resolve`), `:429-433` (`tiers`) | multiplies **both charge rate and cost rate** by the multiplier |
| Marking | `app/services/time_entries.py:816-821` (`resolve_overtime`), create `:382,426`, update `:589-616` | a manual tick; no threshold, no warning, no status |
| Other paths | rerate `rerate.py:223`, corrections `time_corrections.py:383`, cost fill `time_costs.py:77`, recovery `recovery_values.py:78`, billing summary `billing_summary.py:260`; copy-week `time_copy.py:45` and timer stop `timers.py:182` drop the flag | |
| Where the inflated values go | `charge_value` → WIP (`repositories/wip.py:142,263`), invoices (`services/invoices.py:201,1473`), reports/profitability (`repositories/reports.py:663,877`), budgets, dashboard; `cost_value` → profitability, cost-to-business, recovery | |
| UI | Settings → General → Rates "Overtime" switch + multiplier, help "**charged and costed** at the normal rate times the multiplier" (`ui/src/features/settings/components/general/RateModeCard.tsx:156-185`); per-staff multiplier (`team/components/staff/StaffFormFields.tsx:178-188`); "Overtime" tick in `time/components/EntryPanel.tsx:192-195,511-518`; badge/column in `CellEntriesPanel.tsx:65-68`, `TimeEntriesPage.tsx:252-264` | |
| Tests | API `tests/test_overtime.py` (16 tests, e.g. `test_overtime_time_is_priced_at_the_multiplier_for_charge_and_cost`); UI `GeneralSettingsOvertime.test.tsx`, `EditStaffPanel.overtime.test.tsx`, `EntryPanel.overtime.test.tsx`, `TimeEntriesPage.overtime.test.tsx` | assert today's behaviour |

**Conflicts with APT81**: (1) charge-out is raised today (APT81: never); (2) manual tick (APT81:
derived from standard hours); (3) multiplier only (APT81: also fixed amount, effective dates, role
assignment); (4) no overtime status or confirmation.

### 3.2 Staff hours, cost rates, approval, reports

- Staff hours: `organization_members.weekly_capacity_minutes` (default 2250 = 37.5 h),
  `working_minutes_per_day`, `working_days`, `chargeable_target_bp`, start/leaving dates
  (`app/db/models/organization.py:462-490`); **undated** — a PATCH overwrites
  (`app/services/staff.py:343`). Used by targets (`time_targets.py:57`) and utilisation
  (`repositories/reports.py:728`).
- Effective-dated pattern to reuse: `rate_rules` with a period CHECK and the GiST exclusion
  `ex_rate_rules_scope_period` → 409 `RATE_OVERLAP` (`app/db/models/rates.py:125-146`); UI
  `RateRulePanel`, `EndDateDialog`, `RuleStatusBadge`.
- Cost rates: staff rule → grade rule/grade default → 0 (`charge_rates.py:356-388`); stored on each
  entry as `cost_rate` / `cost_value` with `rate_source` (`app/db/models/time.py:174-181`).
  Cost rules exist only at staff and **grade** level.
- Capture: POST/PATCH `/time-entries`, bulk, copy-entry, copy-week, timer stop, `/correct`,
  `/remove`; UI write paths: `EntryPanel` (6 screens), Week grid autosave (`WeekView.tsx:115-140`),
  timer stop (`TimerCard.tsx:197-213`), copy entry, copy week, opening-balance import.
- **Every entry needs a client and a job** (`time_entries.py:715-737`, APT64, on main); internal
  time goes to the built-in Internal client's `INT-*` jobs (migration 078).
- Approval is per **weekly timesheet** (`open/submitted/approved/rejected`,
  `app/db/models/time.py:26,45`); reject returns the whole week; approved time changes through
  corrections; approval can be switched off or a person exempted (`docs/modules/time.md:748-770`).
  There is **no per-entry status**.
- Reports: no staff cost report; nearest is `GET /reports/cost-to-business`
  (`report.financial` + `rate.read`, `services/reports_ext.py:331`); UI reports are config-driven
  (`reports/config.ts`, `ReportScreen.tsx`, `definitions/costToBusiness.tsx`). Cost masking by
  `rate.read`. 65 permission codes (`app/core/permissions.py`, counted by
  `tests/test_permissions_doc.py`).
- Audit: `audit.record(...)` in the same transaction; labels in `audit_catalogue.py`.
- Period locks: writes blocked (`period_locks.assert_open_many`, `time_entries.py:405`); rerate
  skips locked days (`period_locks.py:135`).

## 4. Root cause

Not a defect — a new capability. The existing overtime (061) was built for a different rule
(premium on charge and cost, ticked by hand), so APT81 **replaces** it rather than extending it.

## 5. Affected screens and modules

API: time entries (all write paths), timesheets/approvals, rates (`charge_rates.py`), recovery,
reports, staff, settings, permissions, audit, period locks, imports.
UI: Settings → General → Rates (overtime section), staff Add/Edit and detail page, Team → Rates
(new Overtime Rates page), every time-entry screen (EntryPanel ×6, Week grid, timer, copy entry,
copy week), timesheet approvals (+ dashboard pending approvals), Reports (new Staff Cost report),
Time Entries list (overtime column).

## 6. Components and files involved (expected)

API: new migrations; `app/db/models/{organization,time,rates}.py`; new `app/services/overtime.py`
(threshold split + rate lookup); `time_entries.py`, `time_copy.py`, `timers.py`,
`time_corrections.py`, `timesheets.py` (or new overtime approval routes); `charge_rates.py` (remove
the 061 multiplier); new `reports` staff-cost service/repository/schemas/route;
`core/permissions.py`; `audit_catalogue.py`; docs `time.md`, `team-rates.md`, `reports.md`,
`permissions.md`, `database.md`.
UI: `RateModeCard.tsx`, `StaffFormFields.tsx`, `StaffDetailPage.tsx`, new Overtime Rates page
(reusing `RateRulesView`/`RateRulePanel`), a shared `useOvertimeConfirm` + `ConfirmDialog` used by
`EntryPanel`, `WeekView`/`autosave.ts`, `TimerCard`, `CopyEntryDialog`, `CopyWeekDialog`;
`TimesheetApprovalsPage`/`SheetEntries` (marker + per-entry actions); new
`reports/definitions/staffCost.tsx`; `constants/permissions.ts`.

## 7. Proposed solution (to be confirmed after §11)

**Phased delivery — each phase its own PR pair, plan-checked and approved before it starts:**

| Phase | Scope |
|---|---|
| **A. Set-up** | Dated `staff_hours_bases` (weekly/monthly, standard minutes, effective from/to, exclusion constraint), seeded from today's `weekly_capacity_minutes`. Dated `overtime_rates` (level grade-or-role / staff, mode multiplier or fixed £/h). Team → Rates → Overtime Rates page; "Working hours" card on the staff page. **Retire the 061 multiplier from charge-out** (cost-only from now on). |
| **B. Capture** | Server-side threshold split per person and period: the API returns 409 `OVERTIME_CONFIRMATION_REQUIRED` with `details.overtime_minutes` unless the request carries `confirm_overtime: true` (same pattern as `confirm_impact`); one shared UI hook + `ConfirmDialog` on every write path. Overtime stored in a separate `time_entry_overtime` table (entry, overtime minutes, allocated flag, rate, rate source, cost, status, confirmed by/at), so no existing query (WIP, invoices, profitability) can pick it up. |
| **C. Approval** | Per-entry overtime status Pending / Approved / Rejected: marker in the approval queue, approve/reject/amend actions, audit. |
| **D. Report** | `GET /reports/staff-cost` (+ CSV/XLSX/PDF) and the UI report: per staff/period standard vs overtime (allocated/unallocated), Overtime flags per job, drill-down, filters; new permission(s). |

Recalculation (after a change of hours basis or rates) is a preview → apply flow modelled on
re-rate, skipping locked periods.

## 8. UI changes / 9. API changes / 10. Database changes

As in §7. Database: 2–3 new tables, 1–2 new permissions, a data migration retiring 061 (decision §11.9).
API: new routes for hours bases, overtime rates, overtime approval, staff-cost report; 409 on time
writes. UI: 1 new page, 1 new report, 1 new card, a shared confirm on 5 write paths, approvals changes.

## 11. Questions for the client (needed before Phase A)

1. **"Role"** = **grade** (Partner/Manager/Senior/Junior). *Decided 2026-11-06: grade.*
2. Standard hours default to each person's current weekly capacity (37.5 h)?
3. Weekly period = the practice week? Monthly = calendar month? Part weeks, starters and leavers?
4. Every time entry must have a client and job (APT64). Does **"unallocated"** mean time on the
   Internal client's jobs?
5. Which entries become overtime: in order of date then time recorded? Editing/deleting an earlier
   entry re-splits later ones — acceptable?
6. Do profitability / cost-to-business keep the **standard** cost of overtime hours (only the
   premium is report-only), or exclude overtime hours entirely?
7. Does rejecting overtime remove the minutes, or only the extra cost?
8. Overtime status for people exempt from approval, or when approval is off?
9. **Existing overtime**: entries already ticked as overtime carry a raised charge value (possibly
   already in WIP or invoiced). Re-price unbilled ones at standard rates and leave billed ones? Is
   overtime switched on for any live practice today?
10. Warning on copy-week, timer stop and imports (batch confirm)?
11. Is "amend" the existing correction flow?
12. Who may see the report (new permission; can staff see their own overtime cost)?
13. Warning wording: house style titles are one question ("Add overtime hours?") with the client's
    sentence in the body — OK?

## 12. Regression risks

- Removing the 061 multiplier changes charge-out for overtime-ticked entries (intended; needs §11.9).
- Time write paths gain a confirmation step; must not block normal (non-overtime) time.
- Re-splitting on edits; locked periods; monthly basis across weekly sheets.
- `test_overtime.py` and the 4 UI overtime tests assert the old behaviour — rewritten only with the
  client's sign-off on retiring it (never weakened).

## 13. Testing strategy

Per phase: the acceptance example as an API test (£750 standard; £135 overtime = £90 allocated to
Job X + £45 unallocated; Pending; Job X flagged; invoice and WIP unchanged; staff rate replaces role
rate, never stacked); effective dates and overlap; locks; permissions and cost masking; OpenAPI and
permission-doc tests; migration up/down. UI: confirm/cancel/retry on every write path, approvals,
report filters/drill-down, permission variants. Full API (CI) and UI suites, type check, lint,
format, production build; local run and live check per phase.

## 14. Acceptance criteria

The ticket's acceptance example passes end to end; no overtime amount appears in charge-out, WIP,
invoices, job profitability or any client-facing view; overtime is visible only with the new
permission; every change listed in requirement 5 is in the audit trail.

**Size (honest): Large, not Medium** — about 10–15 API dev-days and 10–14 UI dev-days (4–6 weeks),
which is why we propose four phases.

## 15. Decisions and Phase A contract (2026-11-06)

Decided: "role" = **grade** (§11.1); APT84 first, then APT81 (APT84's migration 080 comes before
this one). **Phase A is set-up only — no change to how time is priced.** Retiring the 061
multiplier moves to Phase B, because it changes charges on recorded overtime and needs the client's
answer to §11.9. Assumed (§11.2): standard hours start from each person's current weekly capacity.

### Phase A — API (migration 081, after 080)

- `staff_hours_bases`: `id`, `organization_id`, `user_id`, `basis` (`weekly`|`monthly`),
  `standard_minutes` (> 0), `effective_from`, `effective_to` (null = open-ended), audit columns;
  no overlapping periods per person (exclusion constraint, 409 `HOURS_OVERLAP`). Backfill: one
  open-ended `weekly` row per member from `weekly_capacity_minutes`, effective from the member's
  start date, else the day they joined.
- `overtime_rates`: `id`, `organization_id`, `level` (`grade`|`staff`), `grade_id` / `user_id`
  (exactly one, by level), `mode` (`multiplier`|`fixed`), `multiplier_bp` (10000–100000, for
  multiplier) / `rate` (pence per hour, for fixed), `effective_from`, `effective_to`; no
  overlapping periods per scope (409 `RATE_OVERLAP`, like rate rules).
- Endpoints (organization-scoped; summary + description on each):
  - `GET /staff/{user_id}/hours` (history, current first); `POST /staff/{user_id}/hours`
    `{basis, standard_minutes, effective_from}` — ends the open-ended period the day before;
    `DELETE /staff/{user_id}/hours/{id}`. Read `member.read`; write `member.update`.
  - `GET /overtime-rates` (filters `level`, `user_id`, `grade_id`, `status` current|future|ended);
    `POST /overtime-rates`; `PATCH /overtime-rates/{id}` `{effective_to}` (end a rate);
    `DELETE /overtime-rates/{id}` (a rate not yet used). Read `rate.read`; write `rate.manage`.
  - `GET /overtime-rates/resolve?user_id=&date=` → the one rate that applies (staff overrides
    grade, never both) or none, with its source.
- Audit: `staff_hours.added|ended|deleted`, `overtime_rate.added|ended|deleted`.
- Nothing else changes: pricing, the overtime switch and the per-staff multiplier stay as they are
  until Phase B.

### Phase A — UI

- Team → Rates → **Overtime Rates** page (reusing the Cost/Charge-out Rates patterns: list with
  status, add panel — grade or staff, multiplier "× the cost rate" or fixed "£ per hour", effective
  dates — end and delete), gated by `rate.read` / `rate.manage`.
- Staff detail page: **Working hours** card — current basis and standard hours, dated history,
  "Change working hours" panel (weekly / monthly, standard hours h:mm, effective from), gated by
  `member.read` / `member.update`.
- No change to Settings → Rates (overtime switch), Edit staff (multiplier) or time entry until
  Phase B.

## 16. Decisions and contract for Phases B–D (2026-11-06)

Decided by the developer: (§11.9) from release overtime never raises charge-out; saved entries keep
their stored values (billed untouched, unbilled change only if re-rated). (§11.4) "Unallocated" =
overtime on the built-in Internal client's jobs; overtime on any other job is allocated. (§11.7)
rejecting overtime removes only the overtime **premium** (the hours stay at standard cost). (§11.12)
a new permission `report.staff_cost` ("View the staff cost report"), given to Owner, Admin and Partner.
Recommended defaults adopted (§11.2/3/5/6/8/10/11/13): new staff get an open-ended weekly hours row
from their weekly capacity when added; weekly period = Monday–Sunday, monthly = calendar month;
entries become overtime in order of entry date, then time recorded (edits re-split the period);
profitability and cost-to-business keep the **standard** cost of every hour (the premium is only on
the Staff Cost Report); overtime is auto-approved for people exempt from approval or when approval
is off; the warning also applies to copy, copy-week and timer stop; "amend" = the existing
correction flow (which re-splits); the dialog title is "Add overtime hours?" with the client's
sentence "You are adding overtime hours. Are you sure?" and the overtime hours in the body.
The practice "Overtime" switch stays as the on/off for the whole feature (off: no detection, no
warnings); the 061 multipliers (practice and per staff) are retired — overtime rates replace them.

### API (migration 082)

- `time_entry_overtime`: `id`, `organization_id`, `time_entry_id` (unique), `user_id`,
  `overtime_minutes` (> 0), `allocated` (bool), `overtime_rate_id` (nullable), `rate_source`
  (`staff`|`grade`|`none`), `overtime_rate` (pence/h actually applied), `overtime_cost` (pence),
  `premium_cost` (pence = overtime cost − standard cost of those minutes, ≥ 0), `status`
  (`pending`|`approved`|`rejected`), `confirmed_by`, `confirmed_at`, `decided_by`, `decided_at`,
  `decision_note`, timestamps. New permission `report.staff_cost` (catalogue + default roles).
- Detection: on every time write (create, update, copy, copy-week, timer stop, correct, remove,
  delete) recompute the person's period; a write that **adds or increases** overtime and lacks
  `confirm_overtime: true` → 409 `OVERTIME_CONFIRMATION_REQUIRED`, `details: {overtime_minutes,
  standard_minutes, period_start, period_end}`; with it → saved, rows created/updated (status
  `pending`, or `approved` when exempt / approval off), audit `overtime.confirmed` (user, time).
  Re-splits caused by edits elsewhere need no confirmation. Locked periods are never rewritten.
- `RateBook` no longer applies the 061 multiplier to charge or cost; `time_entries.is_overtime`
  becomes "has an overtime portion" (kept in step).
- Approval: `GET /overtime` (filters user, status, from, to, allocated; with the entry, job,
  client), `POST /overtime/{id}/approve`, `POST /overtime/{id}/reject` `{note}`; permission: the
  person's approver or `time.manage_all`; audit `overtime.approved|rejected`.
- Report: `GET /reports/staff-cost` (filters `from`, `to`, `user_id`, `grade_id`, `job_id`,
  `client_id`, `status`, `overtime_type` allocated|unallocated) → rows per staff member and period:
  `standard_minutes`, `standard_cost`, `overtime_minutes_allocated`, `overtime_cost_allocated`,
  `overtime_minutes_unallocated`, `overtime_cost_unallocated`, `total_cost`, `pending_overtime_cost`;
  `GET /reports/staff-cost/jobs?user_id=…` → flagged jobs (job, client, overtime minutes, rate
  applied, cost, status); CSV/XLSX/PDF downloads with `report.export`; permission
  `report.staff_cost`; `report.exported` audit. Overtime amounts appear nowhere else.

### UI

- One shared confirm (`ConfirmDialog`, "Add overtime hours?") wired into every time write path:
  EntryPanel (6 screens), Week grid autosave, timer stop, copy entry, copy week. Overtime badge on
  entries; approvals show overtime entries with Approve / Reject (note).
- Settings → Rates: the "Overtime" switch help text becomes cost-only; the multiplier fields
  (practice and Edit staff) are removed (overtime rates replace them).
- Reports → **Staff Cost** report (config-driven `ReportScreen`): filters as above, per-staff rows,
  drill-down to flagged jobs, downloads, gated by `report.staff_cost`.
