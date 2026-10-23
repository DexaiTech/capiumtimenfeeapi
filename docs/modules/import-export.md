# Bulk import and export of staff, rates and jobs

Code: `app/api/v1/data_import_export.py` → `app/services/{staff,rate,job}_import.py`,
`app/services/{staff,rate,job}_export.py`, shared CSV helpers in `app/services/import_csv.py`
and the write phase in `app/services/import_run.py`; result schema in `app/schemas/imports.py`.
Tests: `tests/test_{staff,rate,job}_import.py`, `tests/test_import_export_openapi.py` (helpers in
`tests/import_export_helpers.py`). No schema change, no migration. The clients import
(`docs/modules/clients.md`) and the opening balance import use the same upload rules.

All paths are under `/api/v1/organizations/{organization_id}`. Money in the files is **pounds**
(`48.00`, `£1,250.50`), never pence. Dates are `YYYY-MM-DD` or `DD/MM/YYYY`.

## Endpoints and permissions

Every operation needs the practice permission `data.import_export` (Owner and Admin) **and** the
permission of the data it touches; a caller without either gets 403.

| Method | Path | Also requires |
| --- | --- | --- |
| GET | `/staff/export` | `member.read` |
| POST | `/imports/staff?dry_run=` | `member.invite` (`rate.manage` for rate columns, see below) |
| GET | `/rates/export` | `rate.read` |
| POST | `/imports/rates?dry_run=&close_previous=` | `rate.manage` |
| GET | `/jobs/export` | `job.read` |
| POST | `/imports/jobs?dry_run=` | `job.create` |

The `/export` routes are registered before the staff, rates and jobs routers so they are not read
as an id.

## Upload rules (all three imports, as for clients)

* `multipart/form-data` with a `file` part, or a raw `text/csv` body; at most 5 MB (413
  `FILE_TOO_LARGE`) and 5000 rows (422 `IMPORT_TOO_MANY_ROWS`); other content types 415
  `FILE_TYPE_NOT_ALLOWED`; a missing `file` part, an empty file or a file without data rows 400
  `FILE_INVALID`. Text is UTF-8 (BOM allowed), falling back to Windows-1252. The body-size
  middleware exempts `/imports/<name>`.
* The header row is mapped by name: case, spaces and punctuation are ignored and the aliases below
  work. Columns that match nothing are ignored and listed in `ignored_columns`. A missing required
  column is 422 `IMPORT_MISSING_COLUMNS`; two columns for one field 422
  `IMPORT_DUPLICATE_COLUMNS`. Blank rows are skipped; row numbers are 1-based data rows (the
  header is row 0).
* `dry_run=true` validates and writes nothing: 200 with the result and per-row `errors`. A real
  run is **all-or-nothing**: any row error answers 422 `IMPORT_INVALID` with the same errors in
  `details`, otherwise every row is written in one transaction.
* Rows are validated by really writing them inside a savepoint (each in its own), then the whole
  transaction is rolled back for a dry run or when any row failed. So a file is checked against
  the same rules and messages as the matching create endpoint, including interactions between
  rows of the file (a repeated email, overlapping rates, numbers from a sequence). Numbers drawn
  from a sequence are therefore also shown on a dry run (for example the job numbers), and are
  not consumed by it.
* Each real import writes **one summary audit entry** (`staff.imported`, `rate.imported`,
  `job.imported`: `count`, and the first 100 codes for staff and jobs). Records written through
  the existing services keep their own entries (`job.created` per job; rate history rows
  `rate_changes` with `source = import`). Staff and rate imports write no per-record audit entry.

Response (`BulkImportResult`): `dry_run`, `total_rows`, `valid_rows`, `created` (0 on a dry run),
`columns` (recognised, as named here), `ignored_columns`, `errors[]` (`row`, `field`, `code`,
`message`), `items[]` (`row`, `code`, `name`, `id`; `id` is null on a dry run) and `counts`
(`rates` for staff, `cost`/`charge` for rates).

## Staff

Creates staff like `POST /staff`: no login (a placeholder account), the Staff role, status
`active`. Existing staff are never changed. Required columns: `full_name`, `grade`, `start_date`.

| Column | Aliases | Notes |
| --- | --- | --- |
| `code` | staff code, staff no, employee id | Default: the `staff` sequence (skips codes in the file). Used code: row error `STAFF_CODE_TAKEN` (message "Another staff member already has this code."); repeated in the file: `duplicate` on the later row. |
| `full_name` | name, staff name, employee | Required. |
| `email` | work email | Work email; no two active staff share one (`STAFF_EMAIL_TAKEN`). |
| `grade` | grade code, grade name | Code or name; must exist and be active. |
| `job_title` | title, position | |
| `initials` | | Upper-cased, unique in the practice. |
| `start_date` | start, joined | Required. |
| `leaving_date` | leaving, end date | Not before `start_date`. |
| `working_minutes_per_day` | minutes per day | 1-1440; default the practice's. |
| `hours_per_day` | | Hours (`7.5`) converted to minutes; not together with the minutes column. |
| `working_days` | days, work days | `Mon-Fri`, `Mon;Wed;Fri`, `1,2,3,4,5` (1 = Monday); default the practice's. |
| `chargeable_target_percent` | target, chargeable target | `75` or `75%` becomes 7500 basis points; default the grade's, else 75%. |
| `approver_code` | approver | Staff code of an existing, active staff member who can sign in (as `POST /staff`). A person created by the same file has no login yet: row error `approver_cannot_sign_in`. |
| `cost_rate`, `charge_rate` | cost, charge | Pounds per hour; create staff-level rate rules (kind cost / charge, `user_id` set) through `RateRuleService.write_rule`. Without `rate.manage` a row that has one gets a `permission_denied` row error. |
| `rate_effective_from` | rate from | Start of those rules; default `start_date`. |

Export columns: `code`, `full_name`, `email`, `grade` (code), `job_title`, `initials`,
`start_date`, `leaving_date`, `working_minutes_per_day`, `working_days` (`Mon-Fri` or
`Mon;Wed;Fri`), `chargeable_target_percent`, `approver_code`, `status` (active/inactive/left),
`login_status` (none/invited/active) and, only with `rate.read`, `cost_rate` and `charge_rate`
(the standard rate today as `GET /staff` resolves it, in pounds). Every staff member of any
status, ordered by code. The first columns are what the import reads (`status` and `login_status`
are ignored on import), but people without a start date (for example the practice owner) must get
one before their row imports.

## Rates

One row is one rate rule, written with `RateRuleService.write_rule` (reference checks, overlap
check, history). Required columns: `kind`, `rate`, `effective_from`.

| Column | Aliases | Notes |
| --- | --- | --- |
| `kind` | type | `cost` or `charge`. |
| `staff_code` | staff | Staff code (any status; a person who left is `MEMBER_REMOVED`). |
| `grade` | grade code, grade name | Code or name. |
| `client_code` | client | |
| `service_code` | service | |
| `rate` | amount, rate per hour | Pounds per hour, at most two decimals. |
| `effective_from` | from, start date, valid from | |
| `effective_to` | to, end date, valid to | Optional, inclusive; blank = open-ended. |

The filled scope columns decide the level exactly as `POST /rates`: client + service + staff,
client + service, client + staff, client alone (everyone), service + grade, staff alone, grade alone.
Anything else is a row error (`invalid_scope` on `row`); cost rates exist only for staff or grade
(`invalid_kind`). An overlap with an existing rule or an earlier row of the same file is
`RATE_OVERLAP` on `effective_from`. With `close_previous=true` an earlier rule of the same scope
that runs into the new period is ended the day before it, when that is a plain shortening (also
for rows of the same file, in file order).

Export columns: the eight import columns (`kind`, `staff_code`, `grade`, `client_code`,
`service_code`, `rate`, `effective_from`, `effective_to`) then `level`, `status`
(current/future/ended today), `staff_name`, `client_name`, `service_name`. Every rule of every
level, charge before cost, most specific level first. The route needs `rate.read`, the permission
that shows cost rates everywhere, so cost rules are not masked here.

## Jobs

One row is one job, created with `JobService.create` (what `POST /jobs` calls): the service gives
the default billing basis and fixed fee, an inactive client is `CLIENT_INACTIVE`, people must be
active members, a fixed job needs a fee. Required columns: `name`, `client_code`.

| Column | Aliases | Notes |
| --- | --- | --- |
| `name` | job, job name, title | |
| `client_code` | client | Existing, not deleted. |
| `service_code` | service | Existing and active. |
| `status` | | `planned` (default) or `in_progress`; later statuses are set on the job. |
| `billing_basis` | basis, billing | `hourly`, `fixed`, `non_billable` (`Non-billable` works). |
| `fixed_fee`, `hourly_rate`, `budget_fee` | fee, rate, | Pounds. |
| `budget_hours` | budget, hours budget | Hours (`12.5`), stored as minutes. |
| `period_start`, `period_end`, `start_date`, `due_date` | start, due, deadline | |
| `manager`, `partner` | manager code, manager email, ... | Staff code or email address. |
| `assignees` | staff, assigned to | Staff codes or emails separated by `;`. |
| `description` | notes | |

The job number always comes from the `job` sequence; a `number` column is ignored (it is reported
in `ignored_columns`, which is what an exported file's `number` column does).

Export columns: `number`, `name`, `client_code`, `service_code`, `status`, `billing_basis`,
`fixed_fee`, `hourly_rate`, `budget_hours`, `budget_fee`, `period_start`, `period_end`,
`start_date`, `due_date`, `manager`, `partner`, `assignees`, `description`, `client_name`. Every
job that is not deleted, ordered by number; no actuals or WIP.

## Export format

CSV, UTF-8 with a BOM (opens in Excel), CRLF line ends, `Content-Disposition: attachment`
(`staff.csv`, `rates.csv`, `jobs.csv`). Text cells that start with `=`, `+`, `-`, `@`, tab or CR are
prefixed with `'` (CSV injection), as in the clients export.

## Errors

| Code | Status | When |
| --- | --- | --- |
| `IMPORT_INVALID` | 422 | Real run with row errors (`details` = the row errors); nothing written. |
| `IMPORT_MISSING_COLUMNS`, `IMPORT_DUPLICATE_COLUMNS`, `IMPORT_TOO_MANY_ROWS` | 422 | File-level. |
| `FILE_INVALID`, `FILE_TOO_LARGE`, `FILE_TYPE_NOT_ALLOWED` | 400, 413, 415 | Upload problems. |
| `PERMISSION_DENIED` | 403 | Missing `data.import_export` or the data permission. |

Row error codes include `missing`, `invalid`, `duplicate`, `unknown_grade`, `inactive_grade`,
`unknown_staff`, `unknown_client`, `unknown_service`, `unknown_member`, `approver_cannot_sign_in`,
`permission_denied`, `invalid_scope`, `invalid_kind`, and the codes of the create endpoints
(`STAFF_CODE_TAKEN`, `STAFF_EMAIL_TAKEN`, `RATE_OVERLAP`, `CLIENT_INACTIVE`, `MEMBER_REMOVED`, ...).

## Audit actions

`staff.imported` (`member`), `rate.imported` (`rate_rule`) and `job.imported` (`job`), each with a
`meta` of `count` (and `rates`, `cost`/`charge`, `close_previous`, `codes`). Also see
`docs/modules/audit.md`.
