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
  "receipt_required_threshold": 2500, "invoice_approval_required": false,
  "invoice_approval_threshold": 0, "mileage_rate": 45, "expense_markup_bp": 0,
  "rate_mode": "staff_first",
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
| `invoice_approval_required` | boolean |
| `mileage_rate` | integer pence per mile, 0–1000 |
| `expense_markup_bp` | integer basis points, 0–100 000 (1000 = 10%) |
| `rate_mode` | `staff_first` or `grade_only` |
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
| `invoice_approval_threshold` | 0 | With approval on, invoices above it (gross) need it; 0 = every invoice |
| `mileage_rate` | 45 (45p a mile) | The rate a mileage expense is priced at |
| `expense_markup_bp` | 0 | Basis points added when an expense is recharged to the client |
| `rate_mode` | `staff_first` | `grade_only` skips the staff-level rate when time is priced (`docs/domain.md`, "Rates") |

Out-of-range values are 422 `VALIDATION_ERROR` with the setting in `details[0].field` (a
threshold of -1 or 100 000 001, a `mileage_rate` of 1001, an `expense_markup_bp` of 100 001, a
fraction, a value outside the two modes); the database keeps the same bounds in named check
constraints (`ck_organizations_*`). Every change is audited as `organization.updated` with
`changes: {setting: [old, new]}`, and everyone in the practice can read them.

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
