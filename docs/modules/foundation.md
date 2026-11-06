# Foundation: accounts, practices, staff, roles, audit, worker

The part of the API every other module stands on. It was carried over from the sibling
product and adapted: pages, teams, invitations, avatars and file storage were
removed (notifications came back as their own module: `docs/modules/notifications.md`); roles are the seven of `app/core/permissions.py` (every permission, with the system roles that hold it: `docs/modules/permissions.md`); a practice (`organizations` row)
carries the billing settings and the settings the expense, WIP, invoice-approval and rate modules read; a staff member (`organization_members` row) carries initials,
manager, weekly capacity and chargeable target.

> The Team module (`docs/modules/team-rates.md`) manages the same members as **staff**
> (`/staff`: code, grade, approver, working hours, start and leaving date, optional login,
> invitations). `/members` and `/people` below keep working: they also list staff without a login
> (their `email` is the work email), and a member's `status` can now also be `inactive` or `left`,
> which are the `/staff` names for `suspended` and `removed`. `/members` hides `left` and
> `removed` people by default (`status=left` or `status=removed` shows them).

All paths are under `/api/v1`. "Practice" and "organization" are the same thing; the URLs and
the JSON keep the word `organization`.

## Endpoints

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/system` | public | `closed_deployment` and, when closed, the one practice's name and slug |
| POST | `/auth/register` | public (403 `REGISTRATION_DISABLED` when closed) | Create an unverified account; 202 whether or not the email exists |
| POST | `/auth/login` | public | Access token in the body, refresh token as an httpOnly cookie (`ctf_refresh`, `__Secure-ctf_refresh` over HTTPS) scoped to `/api/v1/auth` |
| POST | `/auth/refresh` | cookie + `X-Requested-With` + allowed `Origin` | Rotate the refresh token (20 s grace for parallel tabs; reuse after it revokes the session) |
| POST | `/auth/logout` | cookie + browser headers | Revoke this session |
| POST | `/auth/logout-all` | signed in | Revoke every session; `{revoked: n}` |
| POST | `/auth/verify-email` | public | Verify with the emailed token |
| POST | `/auth/resend-verification` | public | New verification email |
| POST | `/auth/forgot-password` | public | 202 always; emails a one-time link to known addresses |
| POST | `/auth/reset-password` | public | New password with the token; signs out everywhere |
| GET | `/users/me` | signed in (also unverified, also with a temporary password) | The profile |
| PATCH | `/users/me` | verified | `full_name`, `timezone` |
| POST | `/users/me/password` | verified (allowed with a temporary password) | Change the password; other sessions are revoked |
| GET | `/users/me/sessions` | signed in | Active sessions, `is_current` marked |
| DELETE | `/users/me/sessions/{session_id}` | signed in | Revoke one of one's own sessions (another user's id is 404) |
| GET | `/permissions` | verified | The permission catalogue, by category then code |
| GET | `/users/me/organizations` | verified | My practices with my role and permissions in each |
| POST | `/organizations` | verified (403 `ORGANIZATION_CREATION_DISABLED` when closed) | Create a practice: roles, defaults, caller becomes Owner |
| GET | `/organizations/by-slug/{slug}` | active member | Resolve a slug |
| GET | `/organizations/{id}` | `organization.read` | The practice, my role and my permissions |
| PATCH | `/organizations/{id}` | `organization.update` | Practice and billing settings |
| POST | `/organizations/{id}/wip-valuation/preview` | `organization.update` | What re-valuing the unbilled time by a WIP valuation would do; nothing is saved |
| PUT | `/organizations/{id}/wip-valuation` | `organization.update` | Save the WIP valuation, optionally re-valuing the unbilled time |
| DELETE | `/organizations/{id}` | `organization.delete` | Soft delete; body `{confirm_name}` |
| GET | `/organizations/{id}/members` | `member.read` | Staff, by name, cursor-paginated; `status` (repeatable), `role_id`, `q` |
| GET | `/organizations/{id}/members/{user_id}` | `member.read` | One member (any status, removed included) |
| PATCH | `/organizations/{id}/members/{user_id}` | `member.update` | Role, status, manager, capacity, chargeable target, job title, initials |
| DELETE | `/organizations/{id}/members/{user_id}` | `member.remove` (none to remove oneself) | Remove (status `removed`, row kept) or leave |
| POST | `/organizations/{id}/people` | `member.invite` | Create a verified account with a temporary password and add it |
| POST | `/organizations/{id}/members/{user_id}/reset-password` | `member.update` | New temporary password; signs the person out |
| GET | `/organizations/{id}/roles` | `role.read` | System roles in fixed order, then custom by name, with member counts |
| POST | `/organizations/{id}/roles` | `role.manage` | Create a custom role |
| PATCH | `/organizations/{id}/roles/{role_id}` | `role.manage` | Rename or describe a custom role |
| PUT | `/organizations/{id}/roles/{role_id}/permissions` | `role.manage` | Replace a custom role's permissions |
| DELETE | `/organizations/{id}/roles/{role_id}` | `role.manage` | Soft-delete a custom role without members |
| GET | `/organizations/{id}/audit-logs` | `audit_log.read` | Newest first, cursor-paginated; `action` (exact or `prefix.`), `actor_user_id`, `from`, `to`, `target_type`, `target_id`, `q`; the record history and the export are in `docs/modules/audit.md` |
| GET | `/organizations/{id}/period-locks`, `/organizations/{id}/period-locks/history` | `period_lock.read` | The practice's period lock and its history; see `docs/modules/period-locks.md` |
| POST | `/organizations/{id}/period-locks`, `/organizations/{id}/period-locks/reopen` | `period_lock.manage`, `period_lock.reopen` | Lock time, expenses and WIP up to a date; reopen with a reason |
| GET | `/health/live`, `/health/ready` | public | Probes (ready = database reachable and migrations at head) |

Every practice route answers 404 for a practice the caller is not an active member of, and for
ids (members, roles) that belong to another practice. Permission checks come before the
target is looked up, so a caller without the permission gets 403 for any id.

## Shapes

`OrganizationRead` (`data` of PATCH, `data.organization` of the context responses):

```json
{
  "id": "…", "name": "Smith & Co", "slug": "smith-co", "timezone": "Europe/London",
  "week_start": 1, "working_days": [1, 2, 3, 4, 5], "minutes_per_day": 450,
  "currency": "GBP", "vat_registered": true, "vat_number": null, "company_number": null,
  "payment_terms_days": 30, "default_charge_rate": 0, "time_increment_minutes": 6,
  "require_timesheet_approval": true,
  "fixed_fee_wip_mode": "on_completion", "wip_adjustment_approval_threshold": 50000,
  "wip_adjustment_approval_required": true, "expense_approval_required": true,
  "receipt_required_threshold": 2500, "invoice_approval_required": false,
  "invoice_approval_threshold": 0, "mileage_rate": 45, "expense_markup_bp": 0,
  "wip_valuation_sources": ["client", "service", "grade", "staff"], "rate_mode": "staff_first",
  "email": null, "phone": null,
  "address_line1": null, "address_line2": null, "city": null, "postcode": null,
  "country": "GB", "bank_details": null, "invoice_footer": null, "created_at": "…Z"
}
```

`OrganizationContextRead`: `{organization, role: {id, name, system_key}, permissions: [codes]}`.

`OrganizationCreate`: `name` (1–120), `slug?` (3–40, `^[a-z0-9][a-z0-9-]*[a-z0-9]$`),
`timezone?` (IANA; default the creator's), `currency` (`^[A-Z]{3}$`, default GBP),
`vat_registered` (default true).

`OrganizationUpdate` (every field optional; unknown fields are 422):

| Field | Rule |
|---|---|
| `name` | 1–120 |
| `timezone` | IANA name |
| `week_start` | 1–7 (1 = Monday) |
| `working_days` | 1–7 unique values in 1–7, stored sorted |
| `minutes_per_day` | 1–1440 |
| `vat_registered`, `require_timesheet_approval` | booleans |
| `payment_terms_days` | 0–365 |
| `default_charge_rate` | integer pence per hour, 0–10¹² (a fraction is 422) |
| `time_increment_minutes` | one of 1, 5, 6, 10, 15, 30, 60 (a new practice starts with 6, tenths of an hour) |
| `fixed_fee_wip_mode` | `on_completion` or `by_budget_progress` |
| `wip_adjustment_approval_threshold`, `receipt_required_threshold`, `invoice_approval_threshold` | integer pence, 0–100 000 000 (a fraction, a negative number or more is 422 on that field) |
| `invoice_approval_required`, `expense_approval_required`, `wip_adjustment_approval_required`, `client_segments_enabled`, `branches_enabled` | booleans |
| `mileage_rate` | integer pence per mile, 0–1000 |
| `expense_markup_bp` | integer basis points, 0–100 000 (1000 = 10%) |
| `cost_plus_markup_bp` | integer basis points, 0–100 000 (5000 = 50%) |
| `recovery_rate_order` | `client_job`, `task` and `staff`, each once, in any order |
| `rate_mode` | Deprecated: `staff_first`, `grade_only` or `staff_only`; sets the WIP valuation it stands for (see [WIP valuation](#wip-valuation)) |
| `country` | ISO 3166 alpha-2, upper case |
| `vat_number`, `company_number` (≤ 20), `email` (valid address), `phone` (≤ 40), `address_line1`, `address_line2` (≤ 200), `city` (≤ 100), `postcode` (≤ 20), `bank_details` (≤ 500), `invoice_footer` (≤ 1000) | nullable: `null`, `""` or blanks clear them; text is trimmed |

The required fields reject an explicit `null` (422 on that field). `currency` and `slug` are
not accepted (422): the currency is fixed when the practice is created because every amount
is stored in it.

Settings the expense, WIP, invoice-approval and rate modules read, with what a **new** practice
starts with (practices that already existed got the same defaults for the new settings and keep
their own `time_increment_minutes`):

| Setting | Default | What it does |
|---|---|---|
| `time_increment_minutes` | 6 | Logged time is rounded up to a multiple of it (6 = tenths of an hour) |
| `fixed_fee_wip_mode` | `on_completion` | When the unbilled part of a fixed fee counts as WIP (`docs/domain.md`, "Work in progress") |
| `wip_adjustment_approval_threshold` | 50 000 (£500.00) | A WIP write-on or write-off above it needs approval; 0 = every adjustment does |
| `receipt_required_threshold` | 2 500 (£25.00) | An expense above it (gross) must carry a receipt |
| `invoice_approval_required` | false | Whether an invoice must be approved before it is issued |
| `expense_approval_required` | true | Whether expense claims are approved (see [Approval switches](#approval-switches)) |
| `wip_adjustment_approval_required` | true | Whether WIP adjustments above the thresholds are approved |
| `invoice_approval_threshold` | 0 | With approval on, invoices above it (gross) need it; 0 = every invoice |
| `mileage_rate` | 45 (45p a mile) | The rate a mileage expense is priced at |
| `expense_markup_bp` | 0 | Basis points added when an expense is recharged to the client |
| `cost_plus_markup_bp` | 5 000 (50%) | The margin on cost of the "cost plus" basis of the recovery analysis (`docs/modules/reports.md`), unless the client or the job has its own |
| `recovery_rate_order` | `client_job`, `task`, `staff` | The order in which the recovery analysis looks for the rate the work could have been billed at |
| `wip_valuation_sources` | `client`, `service`, `grade`, `staff` | The rate sources that price time, in order; see [WIP valuation](#wip-valuation) |
| `rate_mode` | `staff_first` | Deprecated, derived from `wip_valuation_sources` |
| `client_segments_enabled` | true | Whether the practice uses client segments (see [Client segments switch](#client-segments-switch)) |
| `branches_enabled` | false | Whether the practice uses branches: on, every client needs one (see [Branches switch](#branches-switch)) |

Out-of-range values are 422 `VALIDATION_ERROR` with the setting in `details[0].field` (a
threshold of -1 or 100 000 001, a `mileage_rate` of 1001, an `expense_markup_bp` of 100 001, a
fraction, a value outside the two modes); the database keeps the same bounds in named check
constraints (`ck_organizations_*`). Every change is audited as `organization.updated` with
`changes: {setting: [old, new]}`, and everyone in the practice can read them.

### WIP valuation

Ticket APT84: the practice chooses which rate sources value its time and in what order (Settings →
General → WIP & billing, "WIP valuation"). `wip_valuation_sources` lists the sources switched on,
first to last: `client` (the rates agreed with the client and the job's own hourly rate), `service`,
`grade` and `staff`; 1 to 4, each once (check constraint `ck_organizations_wip_valuation_sources`).
How the rate ladder uses it: `docs/modules/team-rates.md`, "WIP valuation". It is changed with its
own endpoints, not with `PATCH`:

* `POST /organizations/{id}/wip-valuation/preview` — body `{"sources": ["client", "service",
  "grade", "staff"]}`. Nothing is saved. Response `{entries_changed, wip_before, wip_after,
  skipped: {billed, in_draft, locked, written_off}, too_many}`: the unbilled time entries whose rate,
  value or source would change, the value (pence) of the unbilled, unlocked time now and by the new
  order, and the time left alone. `too_many` (the other fields null) above 20 000 unbilled entries.
* `PUT /organizations/{id}/wip-valuation` — body `{"sources": [...], "revalue": false}`. Saves the
  order (audit `organization.updated`, `changes: {wip_valuation_sources: [old, new]}`, only when it
  changes) and returns `OrganizationRead`. With `revalue: true` the unbilled time is re-valued by it in
  the same transaction through the re-rate engine (a re-rate run, audit `rate.rerun` with
  `wip_valuation_sources`); billed, in-draft, written-off and period-locked time keeps its value. 422
  `WIP_VALUATION_TOO_LARGE` with `revalue` above 20 000 unbilled entries (save without re-valuing and
  re-rate in parts in Team → Rates → Change Rates on Past Time).

Both need `organization.update`. 422 `WIP_VALUATION_SOURCES` ("Keep at least one rate switched on.")
when `sources` is empty, names another source or names one twice. The deprecated `rate_mode` is
derived from the order and `PATCH` still accepts it (see the table above). The column
`organizations.rate_mode` is kept for one release only, so the API of the previous release can read
it while a deploy runs: every write of `wip_valuation_sources` sets it to the derived mode, nothing
reads it, and a later migration drops it.

### Approval switches

Ticket APT53: a practice that does not want an approval process switches it off, area by area, in
General Settings (`PATCH /organizations/{id}`, `organization.update`, like every practice setting).
One switch per area rather than one master switch, because two of them already existed and
practices differ by area (a practice may approve invoices and not timesheets):

| Setting | Default | Off |
|---|---|---|
| `require_timesheet_approval` (Time recording) | true | Nobody approves time. Every timesheet is `approval_exempt`: its billable time is WIP as it is recorded, and **submitting** the week approves it at once (`decision_note` "Approval not required"), with no notification; nobody is reminded about approvals; open weeks with time are not "missing" or "unsubmitted"; the timesheet kind leaves the dashboard's pending approvals and My Actions, and `approvals_waiting` is `null` |
| `expense_approval_required` (Expenses) | true | A submitted claim is approved at once (history `submitted` then `approved`, note "Approval not required"; no notification); the expense kind leaves the dashboard's pending approvals |
| `wip_adjustment_approval_required` (WIP & billing) | true | Every write-on and write-off takes effect at once (`approval_required` false), whatever the two thresholds say; the thresholds are kept for when it is switched on again |
| `invoice_approval_required` (WIP & billing) | false | An invoice is issued without approval (`POST /invoices/{id}/submit` is 409 `APPROVAL_NOT_REQUIRED`) |

**Switching an area off settles what is waiting there**, in the same transaction as the change
(`app/services/approval_switches.py`): every submitted timesheet, submitted expense claim, pending
WIP adjustment and invoice awaiting approval is approved as decided by the person who switched it
off, with the note "Approval switched off", the usual audit entry of an approval
(`timesheet.approved`, `expense_claim.approved`, `wip.adjustment_approved`, `invoice.approved`)
and its usual notifications (the claimant of a claim, the preparers of an invoice). Invoices are
approved, not issued. Approval rules that still apply keep an item waiting for a person instead: an
expense claim with a line dated in a locked period, a WIP adjustment dated in a locked period or
writing off more WIP than is now left. Those are decided on their screens as before (the approve
and return endpoints keep working whatever the switch). The `organization.updated` entry carries
`approvals_settled: {area: {approved, left}}` (areas `timesheets`, `expense_claims`,
`wip_adjustments`, `invoices`).

Approving what waits rather than leaving it: with the switch off the approval screens are no
longer offered, so a waiting item would be stuck, and the practice has just said that this work
needs no approval. Switching an area **on** again changes nothing already decided: new work
needs approval from then on (open timesheet weeks need submitting and approving again before
their time is WIP, as when the setting was on before).

Migration `067` adds `expense_approval_required` and `wip_adjustment_approval_required` (both
on), and marks every sheet of a practice that already had timesheet approval off as
`approval_exempt`; sheets such a practice had waiting are left for its approvers. Tests:
`tests/test_approval_switches.py`.

### Client segments switch

Ticket APT70: a practice that does not group its clients into segments switches them off with
`client_segments_enabled` (default true, so nothing changes until it is switched off), on the
Settings → Client Segments page (`PATCH /organizations/{id}`, `organization.update`, audited as
`organization.updated` like every setting).

Off, the UI hides segments wherever they appear: the clients table column (and the column
chooser), the card view, the Segment filter and the "By segment" analytics, the "Set segment" bulk
action and the selected-clients CSV, the Segment field of the client form, the segment badges of
the client header and overview, and the "Client segments" card of a staff member. A segment
filter carried in a saved filter set or a `?segment=` link is ignored. The client export
(`GET /clients/export`) leaves the `segment` column out.

Nothing is deleted: the segments, the staff linked to them and every client's value are kept,
the API keeps reading and accepting them (the segment endpoints, the client fields, the list's
`segment` filter, the import's `segment` column), and switching segments on again brings them all
back. Staff linked to segments ("the segments they look after") is informational: it never limits
which clients someone can see, so switching segments off changes no one's access. Migration `075`
adds the column. Tests: `tests/test_client_segments_switch.py`.

### Branches switch

Ticket APT74: a practice with several offices groups its clients by branch. It lists its branches
on Settings → Branches (next to Client Segments; `/branches`, `docs/modules/clients.md`,
"Branches") and switches them on there with "Use branches" (`branches_enabled`, default false, so
nothing changes until a practice switches it on; `PATCH /organizations/{id}`,
`organization.update`, audited as `organization.updated` like every setting).

On, each client needs a branch: creating a client, converting a lead or importing a row without
one, or clearing a client's branch, answers 422 `BRANCH_REQUIRED`, and the client form marks the
Branch field required. Clients that have no branch yet are not blocked: they can still be edited
elsewhere, archived and worked on, and the clients list's "No branch" filter finds them. The UI
shows the branch in the clients table (a "Branch" column the Show/Hide Columns menu can hide), the
card view, a Branch filter, the client form and the client header and overview; the client export
has a `branch` column.

Off, the UI hides branches everywhere (like segments when they are off) and nothing is required;
the export leaves the `branch` column out. Nothing is deleted: the branches and every client's
branch are kept and come back when it is switched on again. Migration `077` adds the column, the
`branches` table and `clients.branch_id`. Tests: `tests/test_branches.py`.

`MemberRead`:

```json
{
  "user_id": "…", "full_name": "Ada Smith", "email": "ada@…",
  "role": {"id": "…", "name": "Staff", "system_key": "staff"},
  "status": "active", "manager_user_id": null, "weekly_capacity_minutes": 2250,
  "chargeable_target_bp": 7500, "job_title": null, "initials": "AS",
  "must_change_password": false, "joined_at": "…Z"
}
```

`MemberUpdate`: `role_id`, `status` (`active` | `suspended`), `manager_user_id` (nullable),
`weekly_capacity_minutes` (0–10080), `chargeable_target_bp` (0–10000), `job_title` (≤ 120,
nullable), `initials` (1–6 letters or digits, upper-cased, nullable). `role_id`, `status`,
`weekly_capacity_minutes` and `chargeable_target_bp` reject `null`.

`PersonCreate`: `full_name`, `email`, `role_id`, `job_title?`, `initials?`,
`manager_user_id?`, `weekly_capacity_minutes` (default 2250), `send_email` (default true).
Response `PersonCreated`: `{member, temporary_password, email_sent}` with
`Cache-Control: no-store`.

`RoleRead`: `{id, name, system_key, description, is_system, permissions, member_count}`.

## Error codes

| Status | Code | When |
|---|---|---|
| 401 | `AUTH_REQUIRED`, `AUTH_TOKEN_INVALID`, `AUTH_INVALID_CREDENTIALS`, `AUTH_REFRESH_INVALID` | No/invalid token, wrong password, bad refresh cookie |
| 400 | `AUTH_INVALID_CREDENTIALS` | Wrong current password on change (400 so the UI does not refresh) |
| 403 | `PERMISSION_DENIED` | Missing permission; own role/status change; granting permissions one lacks; non-Owner touching Owner |
| 403 | `AUTH_EMAIL_NOT_VERIFIED`, `AUTH_ACCOUNT_DISABLED` | Unverified or disabled account |
| 403 | `PASSWORD_CHANGE_REQUIRED` | Temporary password still set (only `/auth/*`, `GET /users/me`, `POST /users/me/password` work) |
| 403 | `REGISTRATION_DISABLED`, `ORGANIZATION_CREATION_DISABLED` | Closed deployment |
| 404 | `RESOURCE_NOT_FOUND` | Unknown id, another practice's id, not an active member |
| 409 | `CONFLICT` | Duplicate slug or role name (`details[0].field`), system role edit (`details.code = system_role`), role with members (`role_in_use`), last active Owner (`last_owner`), ownership limit (`organization_limit`) |
| 409 | `EMAIL_TAKEN` | Person's email already has an account |
| 409 | `INITIALS_TAKEN` | Initials used by another non-removed member of the practice; `details[0] = {field: "initials", code: "duplicate"}` |
| 409 | `USE_CHANGE_PASSWORD`, `ACCOUNT_SHARED` | Admin reset of one's own password, or of someone in another practice too |
| 413 | `REQUEST_TOO_LARGE` | Body over `MAX_REQUEST_BODY_KB` |
| 422 | `VALIDATION_ERROR` | `details: [{field, code, message}]`; also unknown role/manager ids |
| 429 | `RATE_LIMITED` | Auth limits or the global ceiling; `Retry-After` header |

## Rule decisions beyond docs/domain.md

- **Initials** are unique per practice among members whose status is not `removed`,
  compared upper-case (the partial unique index
  `uq_organization_members_organization_id_initials`). They are stored upper-cased. A
  suspended member keeps theirs reserved; removing a member frees them. The check runs
  under the practice row lock that every member change takes, so two concurrent requests
  cannot both win; the index is the backstop. The error is 409 `INITIALS_TAKEN`.
- **Blank clears.** For practice text settings and member `job_title`/`initials`, `null`,
  `""` and all-blank strings mean "clear".
- **Currency** is chosen at creation only (`OrganizationCreate.currency`) and is not
  patchable.
- **Billing settings** need `organization.update` (Owner, Admin, Partner); every member can
  read them through `GET /organizations/{id}` because time entry and billing depend on them.
- **Chargeable target and capacity** are edited with `member.update`, also for oneself; staff
  without it cannot change their own targets.
- **Practice defaults** (`app/services/practice_defaults.py`) are seeded in the creation
  transaction, through the API and through `python -m app.maintenance.bootstrap` alike: VAT
  rates, activity codes, services, grades, ticket tags, expense types, the seven number
  sequences (including `estimate`, `EST00001`), the Standard invoice template and an empty period
  lock (`docs/modules/period-locks.md`).
- **Audit**: `organization.created/updated/deleted`, `member.created/updated/role_changed/
  suspended/reactivated/removed/password_reset`, `role.created/updated/permissions_changed/
  deleted`. `organization.updated` and `member.updated` carry `changes: {field: [old, new]}`;
  sending current values writes nothing. Passwords never reach the log.
- **Bootstrap** takes `--practice-name` (alias `--org-name`), `--admin-name`,
  `--admin-email`, `--slug`, `--password`, `--timezone` (default Europe/London); it prints
  "Practice:" and "Owner:" lines and the generated password once.
