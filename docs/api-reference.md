# API reference

Every endpoint of the API, grouped by router, with the permission it needs and the module doc
that holds its contract (request and response shapes, error codes, rules). 431 routes.

* **Live schema:** Swagger UI at `/docs` and the schema at `/openapi.json` when
  `APP_ENV` is not `production` (`create_app` in `app/main.py`). Each route's OpenAPI
  description names its permission and error codes; several `tests/test_*_openapi.py` files check
  that.
* **Paths:** a path starting with `/` but not `/api/v1` or `/health` is relative to
  `/api/v1/organizations/{organization_id}`; `/` alone is that practice itself.
* **Envelope, errors, auth:** see [architecture.md](architecture.md#response-envelope).

## How the permission column was derived

The table was generated from the running router tree (`app.main:app`), not written by hand:

| Value | Meaning |
|---|---|
| `` `code` `` | The route depends on `require("code")` (`app/dependencies/organization.py`). `a + b` means both. |
| `integration on` | Also 409 `INTEGRATION_DISABLED` while the practice has the central client integration off (`require_central`, `app/api/v1/central_clients.py`). |
| `mock only, 404 in prod` | Exists only while the mock gateway is in use and `APP_ENV` is not `production`. |
| any member | Any active member of the practice; the route description says "Any member". |
| member; service: `codes` | Any member reaches the service, which applies these permissions (own versus others, field masking). Codes taken from the route's OpenAPI description; read the module doc for the exact rule. |
| member; service rules | Any member; the service limits what they see or do (own notifications, own claims, ...). |
| signed in | A valid access token; not practice-scoped. |
| public | No token. Auth routes are rate-limited; "403 when closed" means `CLOSED_DEPLOYMENT=true` refuses it. |

Every practice route also needs an active membership (404 otherwise) and an active subscription
(403 `SUBSCRIPTION_UNAVAILABLE`), except `GET /subscription` and the subscription mock switch.

To regenerate after adding routes, walk `app.main:app.routes` (FastAPI 0.142 nests included
routers as `_IncludedRouter`; follow `.original_router.routes`) and read each `APIRoute`'s
`dependant` tree for `require.<locals>.dependency` closures.

## Health and system

Routers (3 routes): `health.py`, `system.py`. Module doc: [deployment](deployment.md), [foundation](modules/foundation.md).

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/health/live` | public | Process is up |
| GET | `/health/ready` | public | Database reachable and migrations at head |
| GET | `/api/v1/system` | public | Deployment information |

## Authentication

Routers (9 routes): `auth.py`. Module doc: [foundation](modules/foundation.md).

| Method | Path | Permission | Purpose |
|---|---|---|---|
| POST | `/api/v1/auth/register` | public; 403 when closed | Create an account |
| POST | `/api/v1/auth/login` | public (rate-limited) | Sign in |
| POST | `/api/v1/auth/refresh` | public (rate-limited) | Exchange the refresh cookie for a new access token |
| POST | `/api/v1/auth/logout` | public (rate-limited) | Sign out of this device |
| POST | `/api/v1/auth/logout-all` | signed in | Sign out of every device |
| POST | `/api/v1/auth/verify-email` | public (rate-limited) | Verify an email address |
| POST | `/api/v1/auth/resend-verification` | public (rate-limited) | Send a new verification email |
| POST | `/api/v1/auth/forgot-password` | public (rate-limited) | Request a password reset |
| POST | `/api/v1/auth/reset-password` | public (rate-limited) | Set a new password with a reset token |

## Users (the signed-in person)

Routers (8 routes): `users.py`. Module doc: [foundation](modules/foundation.md).

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/api/v1/users/me` | signed in | Get the current user |
| PATCH | `/api/v1/users/me` | signed in | Update the profile |
| POST | `/api/v1/users/me/password` | signed in | Change the password |
| GET | `/api/v1/users/me/sessions` | signed in | List active sessions |
| DELETE | `/api/v1/users/me/sessions/{session_id}` | signed in | Revoke a session |
| POST | `/api/v1/users/me/avatar` | signed in | Set the profile picture |
| DELETE | `/api/v1/users/me/avatar` | signed in | Remove the profile picture |
| GET | `/api/v1/users/{user_id}/avatar` | signed in | Download a profile picture |

## Practices, members, people, roles

Routers (20 routes): `organizations.py`, `members.py`, `people.py`, `roles.py`. Module doc: [foundation](modules/foundation.md), [permissions](modules/permissions.md).

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/api/v1/users/me/organizations` | signed in | List my practices |
| POST | `/api/v1/organizations` | 403 when closed | Create a practice |
| GET | `/api/v1/organizations/by-slug/{slug}` | signed in | Resolve a practice by slug |
| GET | `/` | `organization.read` | Get a practice |
| PATCH | `/` | `organization.update` | Update practice settings |
| DELETE | `/` | `organization.delete` | Delete a practice |
| GET | `/members` | `member.read` | List members |
| GET | `/members/{user_id}` | `member.read` | Get a member |
| PATCH | `/members/{user_id}` | `member.update` | Update a member |
| DELETE | `/members/{user_id}` | member; service: `member.remove` | Remove a member or leave |
| GET | `/members/{user_id}/permissions` | `member.read` + `role.read` | A member's permissions |
| PUT | `/members/{user_id}/permissions` | `role.manage` | Change a member's own permissions |
| POST | `/people` | `member.invite` | Create a person |
| POST | `/members/{user_id}/reset-password` | `member.update` | Reset a member's password |
| GET | `/api/v1/permissions` | signed in | List the permission catalogue |
| GET | `/roles` | `role.read` | List roles |
| POST | `/roles` | `role.manage` | Create a custom role |
| PATCH | `/roles/{role_id}` | `role.manage` | Rename or describe a custom role |
| PUT | `/roles/{role_id}/permissions` | `role.manage` | Replace a custom role's permissions |
| DELETE | `/roles/{role_id}` | `role.manage` | Delete a custom role |

## Audit log

Routers (3 routes): `audit.py`. Module doc: [audit](modules/audit.md).

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/audit-logs` | `audit_log.read` | List audit-log entries |
| GET | `/audit-logs/history` | member; service: `audit_log.read`, `client.read`, `invoice.read`, `job.read` … | History of one record |
| GET | `/audit-logs/export` | `audit_log.export` + `audit_log.read` | Export the audit log |

## Practice settings, segments, branches

Routers (41 routes): `settings.py`, `segments.py`, `branches.py`. Module doc: [clients](modules/clients.md), [team-rates](modules/team-rates.md).

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/vat-rates` | any member | List VAT rates |
| GET | `/vat-rates/{rate_id}` | any member | Get a VAT rate |
| POST | `/vat-rates` | `settings.manage` | Create a VAT rate |
| PATCH | `/vat-rates/{rate_id}` | `settings.manage` | Update a VAT rate |
| DELETE | `/vat-rates/{rate_id}` | `settings.manage` | Delete or archive a VAT rate |
| GET | `/services` | any member | List services |
| GET | `/services/{service_id}` | any member | Get a service |
| POST | `/services` | `settings.manage` | Create a service |
| PATCH | `/services/{service_id}` | `settings.manage` | Update a service |
| POST | `/services/{service_id}/deactivate` | `settings.manage` | Deactivate a service |
| POST | `/services/{service_id}/activate` | `settings.manage` | Reactivate a service |
| DELETE | `/services/{service_id}` | `settings.manage` | Delete or deactivate a service |
| GET | `/services/{service_id}/task-templates` | any member | Default tasks of a service |
| PUT | `/services/{service_id}/task-templates` | `settings.manage` | Set the default tasks of a service |
| GET | `/services/{service_id}/activity-codes` | any member | Activity codes of a service |
| PUT | `/services/{service_id}/activity-codes` | `settings.manage` | Set the activity codes of a service |
| GET | `/activity-codes` | any member | List activity codes |
| GET | `/activity-codes/{activity_id}` | any member | Get an activity code |
| POST | `/activity-codes` | `settings.manage` | Create an activity code |
| PATCH | `/activity-codes/{activity_id}` | `settings.manage` | Update an activity code |
| POST | `/activity-codes/{activity_id}/deactivate` | `settings.manage` | Deactivate an activity code |
| POST | `/activity-codes/{activity_id}/activate` | `settings.manage` | Reactivate an activity code |
| DELETE | `/activity-codes/{activity_id}` | `settings.manage` | Delete or deactivate an activity code |
| GET | `/sequences` | any member | List number sequences |
| PATCH | `/sequences/{kind}` | `organization.update` | Change a number sequence |
| GET | `/segments` | any member | List segments |
| POST | `/segments` | `settings.manage` | Create a segment |
| GET | `/segments/{segment_id}` | any member | Get a segment |
| PATCH | `/segments/{segment_id}` | `settings.manage` | Update a segment |
| POST | `/segments/{segment_id}/deactivate` | `settings.manage` | Deactivate a segment |
| POST | `/segments/{segment_id}/activate` | `settings.manage` | Reactivate a segment |
| DELETE | `/segments/{segment_id}` | `settings.manage` | Delete or deactivate a segment |
| GET | `/staff/{staff_id}/segments` | `member.read` | List the segments of a staff member |
| PUT | `/staff/{staff_id}/segments` | `member.update` | Set the segments of a staff member |
| GET | `/branches` | any member | List branches |
| POST | `/branches` | `settings.manage` | Create a branch |
| GET | `/branches/{branch_id}` | any member | Get a branch |
| PATCH | `/branches/{branch_id}` | `settings.manage` | Update a branch |
| POST | `/branches/{branch_id}/deactivate` | `settings.manage` | Deactivate a branch |
| POST | `/branches/{branch_id}/activate` | `settings.manage` | Reactivate a branch |
| DELETE | `/branches/{branch_id}` | `settings.manage` | Delete or deactivate a branch |

## Period locks

Routers (6 routes): `period_locks.py`. Module doc: [period-locks](modules/period-locks.md).

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/period-locks` | `period_lock.read` | Get the period locks |
| GET | `/period-locks/calendar` | `period_lock.read` | List the periods of an accounting year |
| PUT | `/period-locks/settings` | `period_lock.manage` | Set how the practice locks |
| GET | `/period-locks/history` | `period_lock.read` | List the lock history |
| POST | `/period-locks` | `period_lock.manage` | Lock a period |
| POST | `/period-locks/reopen` | `period_lock.reopen` | Unlock a locked period |

## Onboarding

Routers (2 routes): `onboarding.py`. Module doc: no module doc yet (see [Gaps](#gaps)).

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/onboarding` | `organization.read` | Get the onboarding checklist |
| PUT | `/onboarding/steps/{step_key}` | `onboarding.manage` | Mark an onboarding step |

## Staff, grades, rates

Routers (40 routes): `staff.py`, `grades.py`, `rates.py`, `rate_rules.py`. Module doc: [team-rates](modules/team-rates.md).

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/staff` | `member.read` | List staff |
| POST | `/staff` | `member.invite` | Create a staff member |
| POST | `/staff/bulk` | `member.update` | Change or remove several staff members |
| GET | `/staff/{staff_id}` | `member.read` | Get a staff member |
| PATCH | `/staff/{staff_id}` | `member.update` | Update a staff member |
| POST | `/staff/{staff_id}/deactivate` | `member.remove` | Deactivate a staff member or mark them as left |
| POST | `/staff/{staff_id}/reactivate` | `member.update` | Reactivate a staff member |
| DELETE | `/staff/{staff_id}` | `member.remove` | Delete a staff member |
| POST | `/staff/{staff_id}/invite` | `member.invite` | Invite a staff member to sign in |
| POST | `/staff/{staff_id}/link-login` | `member.invite` | Link a login to a staff member |
| POST | `/staff/{staff_id}/unlink-login` | `member.remove` | Remove a staff member's login |
| GET | `/grades` | `member.read` | List grades |
| POST | `/grades` | `rate.manage` | Create a grade |
| GET | `/grades/{grade_id}` | `member.read` | Get a grade |
| PATCH | `/grades/{grade_id}` | `rate.manage` | Update a grade |
| POST | `/grades/{grade_id}/deactivate` | `rate.manage` | Deactivate a grade |
| POST | `/grades/{grade_id}/activate` | `rate.manage` | Reactivate a grade |
| DELETE | `/grades/{grade_id}` | `rate.manage` | Delete or deactivate a grade |
| GET | `/staff-rates/current` | `rate.read` | Current staff rates |
| GET | `/members/{user_id}/rates` | `rate.read` | A member's rate history |
| POST | `/members/{user_id}/rates` | `rate.manage` | Add a staff rate |
| DELETE | `/staff-rates/{rate_id}` | `rate.manage` | Delete a staff rate |
| GET | `/clients/{client_id}/rates` | `rate.read` | List a client's rates |
| POST | `/clients/{client_id}/rates` | `rate.manage` | Add a client rate |
| PATCH | `/clients/{client_id}/rates/{rate_id}` | `rate.manage` | Change a client rate |
| DELETE | `/clients/{client_id}/rates/{rate_id}` | `rate.manage` | Delete a client rate |
| GET | `/rates` | `rate.read` | List rates |
| POST | `/rates` | `rate.manage` | Add a rate |
| POST | `/rates/impact` | `rate.manage` | Preview what adding a rate changes |
| GET | `/rates/resolve` | `rate.read` | Preview the rate that applies |
| GET | `/rates/history` | `rate.read` | Rate change history |
| POST | `/rates/rerate/preview` | `rate.manage` | Preview re-rating unbilled time |
| POST | `/rates/rerate` | `rate.manage` | Re-rate unbilled time |
| GET | `/rates/reruns` | `rate.read` | List re-rate runs |
| GET | `/rates/reruns/{run_id}` | `rate.read` | Get a re-rate run |
| GET | `/rates/reruns/{run_id}/items` | `rate.read` | List the entries of a re-rate run |
| POST | `/rates/revalue` | `rate.manage` | Re-price unbilled time (V1) |
| GET | `/rates/{rule_id}` | `rate.read` | Get a rate |
| PATCH | `/rates/{rule_id}` | `rate.manage` | Change or end-date a rate |
| DELETE | `/rates/{rule_id}` | `rate.manage` | Delete an unused rate |

## Import and export

Routers (7 routes): `data_import_export.py`, `opening_balances.py`. Module doc: [import-export](modules/import-export.md), [clients](modules/clients.md#import).

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/staff/export` | `data.import_export` + `member.read` | Export staff as CSV |
| POST | `/imports/staff` | `data.import_export` + `member.invite` | Import staff from CSV |
| GET | `/rates/export` | `data.import_export` + `rate.read` | Export rates as CSV |
| POST | `/imports/rates` | `data.import_export` + `rate.manage` | Import rates from CSV |
| GET | `/jobs/export` | `data.import_export` + `job.read` | Export jobs as CSV |
| POST | `/imports/jobs` | `data.import_export` + `job.create` | Import jobs from CSV |
| POST | `/imports/opening_balance_time` | `opening_balance.import` | Import historic unbilled time from CSV |

## Clients, Client 360, leads

Routers (29 routes): `clients.py`, `client_overview.py`, `leads.py`. Module doc: [clients](modules/clients.md).

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/clients/summary` | `client.read` | Client list header counts |
| GET | `/clients/export` | `client.read` | Export clients as CSV |
| POST | `/imports/clients` | `client.create` | Import clients from CSV |
| GET | `/clients` | `client.read` | List clients |
| POST | `/clients` | `client.create` | Create a client |
| POST | `/clients/bulk` | `` | Bulk action on clients |
| GET | `/clients/{client_id}` | `client.read` | Get a client overview |
| PATCH | `/clients/{client_id}` | `client.update` | Update a client |
| POST | `/clients/{client_id}/archive` | `client.delete` | Archive a client |
| DELETE | `/clients/{client_id}` | `client.delete` | Delete a client |
| GET | `/clients/{client_id}/contacts` | `client.read` | List a client's contacts |
| POST | `/clients/{client_id}/contacts` | `client.update` | Add a contact |
| PATCH | `/clients/{client_id}/contacts/{contact_id}` | `client.update` | Update a contact |
| DELETE | `/clients/{client_id}/contacts/{contact_id}` | `client.update` | Delete a contact |
| GET | `/clients/{client_id}/services` | `client.read` | List a client's services |
| POST | `/clients/{client_id}/services` | `client.update` | Add a service to a client |
| PATCH | `/clients/{client_id}/services/{engagement_id}` | `client.update` | Update a client's service |
| DELETE | `/clients/{client_id}/services/{engagement_id}` | `client.update` | Remove a service from a client |
| POST | `/clients/{client_id}/services/{engagement_id}/create-job` | `client.update` + `job.create` | Create the job of a client's service now |
| GET | `/clients/{client_id}/services/{engagement_id}/job-runs` | `client.read` | List the jobs created for a client's service |
| GET | `/clients/{client_id}/overview` | `client.read` | Get a client's 360 overview |
| GET | `/leads` | `client.read` | List leads |
| GET | `/leads/summary` | `client.read` | Lead totals |
| POST | `/leads` | `client.create` | Add a lead |
| GET | `/leads/{lead_id}` | `client.read` | Get a lead |
| PATCH | `/leads/{lead_id}` | `client.update` | Update a lead |
| POST | `/leads/{lead_id}/archive` | `client.delete` | Archive a lead |
| POST | `/leads/{lead_id}/unarchive` | `client.delete` | Unarchive a lead |
| POST | `/leads/{lead_id}/convert` | `client.create` | Convert a lead to a client |

## Central clients

Routers (11 routes): `central_clients.py`. Module doc: [central-clients](modules/central-clients.md).

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/central-clients` | `client.create`; integration on | Search the central client service |
| POST | `/central-clients/{central_id}/enable` | `client.create`; integration on | Enable a central client |
| POST | `/clients/central-sync` | `client.update`; integration on | Synchronise every linked client |
| POST | `/clients/{client_id}/central-sync` | `client.update`; integration on | Synchronise a client with the central client service |
| POST | `/clients/{client_id}/central-link` | `client.create`; integration on | Link a client to a central client |
| DELETE | `/clients/{client_id}/central-link` | `client.create`; integration on | Unlink a client from the central client service |
| POST | `/central-clients/events` | `settings.manage`; integration on | Receive a central client event |
| POST | `/central-clients/process-changes` | `settings.manage`; integration on | Pull and apply the central change feed |
| GET | `/central-clients/mock/availability` | `settings.manage`; mock only, 404 in prod | Is the mock central service on? |
| POST | `/central-clients/mock/availability` | `settings.manage`; mock only, 404 in prod | Switch the mock central service on or off |
| POST | `/central-clients/mock/changes` | `settings.manage`; mock only, 404 in prod | Push a change into the mock central service |

## Integrations and subscription

Routers (20 routes): `integrations.py`, `subscription.py`, `bookkeeping_integration.py`, `central_user_integration.py`, `pm_integration.py`, `integration_cards.py`. Module doc: no module doc yet (see [Gaps](#gaps)); the sync log is in [central-clients](modules/central-clients.md#the-sync-log).

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/integrations/sync-logs` | `integration.read` or `settings.manage` | List the integration sync log |
| GET | `/subscription` | member; service rules | Get the practice's subscription status |
| POST | `/integrations/subscription/mock` | `integration.manage`; mock only, 404 in prod | Set the mock subscription status |
| GET | `/integrations/bookkeeping/mappings` | `integration.read` | Get the bookkeeping mappings |
| PUT | `/integrations/bookkeeping/mappings` | `integration.manage` | Replace the bookkeeping mappings |
| POST | `/integrations/central-user/events` | `integration.manage` | Receive a central user event |
| POST | `/integrations/central-user/process-changes` | `integration.manage` | Pull and apply the central user change feed |
| POST | `/integrations/central-user/mock/changes` | `integration.manage` | Push a change into the mock central user service |
| POST | `/integrations/practice-management/import-jobs` | `integration.manage` | Import and link jobs from practice management |
| POST | `/integrations/practice-management/events` | `integration.manage` | Receive a practice management event |
| POST | `/integrations/practice-management/process-changes` | `integration.manage` | Pull and apply the practice management change feed |
| POST | `/integrations/practice-management/mock/changes` | `integration.manage` | Make a change in the mock practice management system |
| DELETE | `/jobs/{job_id}/pm-link` | `integration.manage` | Unlink a job from practice management |
| GET | `/integrations/sync-items` | `integration.read` | List the sync items |
| POST | `/integrations/sync-items/{item_id}/retry` | `integration.manage` | Retry a sync item now |
| GET | `/integrations` | `integration.read` | List the integrations |
| PATCH | `/integrations/{key}` | `integration.manage` | Switch an integration on or off, or change its settings |
| POST | `/integrations/{key}/sync` | `integration.manage` | Run an integration now |
| POST | `/integrations/{key}/retry-failed` | `integration.manage` | Retry every failed sync item of an integration |
| POST | `/integrations/{key}/mock/availability` | `integration.manage` | Switch a mock service on or off |

## Invoice templates

Routers (9 routes): `invoice_templates.py`. Module doc: [invoice-templates](modules/invoice-templates.md).

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/invoice-templates` | any member | List invoice templates |
| POST | `/invoice-templates` | `settings.manage` | Create an invoice template |
| GET | `/invoice-templates/{template_id}` | any member | Get an invoice template |
| PATCH | `/invoice-templates/{template_id}` | `settings.manage` | Update an invoice template |
| DELETE | `/invoice-templates/{template_id}` | `settings.manage` | Delete an invoice template |
| POST | `/invoice-templates/{template_id}/make-default` | `settings.manage` | Make an invoice template the default |
| POST | `/invoice-templates/{template_id}/watermark-image` | `settings.manage` | Set the watermark picture of an invoice template |
| DELETE | `/invoice-templates/{template_id}/watermark-image` | `settings.manage` | Remove the watermark picture of an invoice template |
| GET | `/invoice-templates/{template_id}/watermark-image` | any member | Download the watermark picture of an invoice template |

## Jobs, tasks, budgets, calendar

Routers (25 routes): `job_budgets.py`, `job_calendar.py`, `jobs.py`, `job_tasks.py`. Module doc: [time](modules/time.md).

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/jobs/budgets` | `job.read` | Budget tracking |
| GET | `/jobs/{job_id}/budget` | `job.read` | A job's budget |
| POST | `/jobs/{job_id}/budget/evaluate` | `job.update` | Re-evaluate a job's budget alerts |
| GET | `/jobs/calendar` | `job.read` | Job calendar |
| GET | `/jobs` | `job.read` | List jobs |
| POST | `/jobs` | `job.create` | Create a job |
| POST | `/jobs/bulk` | `` | Bulk action on jobs |
| GET | `/jobs/{job_id}` | `job.read` | Get a job |
| PATCH | `/jobs/{job_id}` | `job.update` | Update a job |
| GET | `/clients/{client_id}/job-rates` | `job.read` | The rates of a client's jobs |
| POST | `/jobs/{job_id}/status` | `job.update` | Change a job's status |
| POST | `/jobs/{job_id}/copy` | `job.create` + `job.read` | Copy a job |
| PUT | `/jobs/{job_id}/assignees` | `job.update` | Set a job's assignees |
| DELETE | `/jobs/{job_id}` | `job.delete` | Delete a job |
| POST | `/tasks/bulk` | `job.read` | Change or delete several tasks |
| GET | `/tasks` | `job.read` | List tasks across jobs |
| GET | `/jobs/{job_id}/tasks` | `job.read` | List a job's tasks |
| POST | `/jobs/{job_id}/tasks/bulk` | `job.update` | Add several tasks to a job |
| POST | `/jobs/{job_id}/tasks/from-service` | `job.update` | Add the service's default tasks to a job |
| POST | `/jobs/{job_id}/tasks` | `job.update` | Add a task to a job |
| POST | `/jobs/{job_id}/tasks/reorder` | `job.update` | Reorder a job's tasks |
| PATCH | `/jobs/{job_id}/tasks/{task_id}` | `job.update` | Update a task |
| POST | `/jobs/{job_id}/tasks/{task_id}/status` | `job.read` | Change a task's status |
| PUT | `/jobs/{job_id}/tasks/{task_id}/assignees` | `job.update` | Set a task's assignees |
| DELETE | `/jobs/{job_id}/tasks/{task_id}` | `job.update` | Delete a task |

## Time and timesheets

Routers (32 routes): `time_views.py`, `timesheet_reminders.py`, `time_entries.py`, `timesheets.py`. Module doc: [time](modules/time.md).

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/time-entries/day` | member; service: `time.read_all` | Day view |
| GET | `/time-entries/calendar` | member; service: `time.read_all` | Calendar view |
| POST | `/time-entries/copy-week` | member; service: `time.manage_all`, `time.log` | Copy a week |
| POST | `/time-entries/{entry_id}/copy` | member; service: `time.log`, `time.manage_all` | Copy a time entry |
| POST | `/time-entries/{entry_id}/remove` | member; service: `time.manage_all`, `timesheet.approve`, `organization.update` | Remove approved time |
| POST | `/time-entries/{entry_id}/correct` | member; service: `time.manage_all`, `timesheet.approve`, `organization.update` | Correct approved time |
| GET | `/time-entries/{entry_id}/history` | member; service: `rate.read`, `time.read_all` | Corrections of a time entry |
| GET | `/timesheets/missing` | member; service: `time.read_all` | Missing timesheets |
| POST | `/timesheets/reminders/run` | member; service: `organization.update`, `timesheet.approve`, `time.read_all` | Send timesheet reminders |
| GET | `/time-entries` | member; service: `time.read_all` | List time entries |
| GET | `/time-entries/week` | member; service: `time.read_all` | Weekly time grid |
| POST | `/time-entries` | member; service: `time.log`, `time.manage_all`, `rate.read`, `time.read_all` | Log time |
| POST | `/time-entries/bulk` | member; service: `time.log`, `time.manage_all`, `rate.read`, `time.read_all` | Log many time entries |
| GET | `/time-entries/{entry_id}` | member; service: `time.read_all` | Get a time entry |
| PATCH | `/time-entries/{entry_id}` | member; service: `time.log`, `time.manage_all`, `rate.read`, `time.read_all` | Edit a time entry |
| DELETE | `/time-entries/{entry_id}` | member; service: `time.log`, `time.manage_all` | Delete a time entry |
| GET | `/timer` | `time.log` | Get my timer |
| POST | `/timer/start` | `time.log` | Start my timer |
| POST | `/timer/pause` | `time.log` | Pause my timer |
| POST | `/timer/resume` | `time.log` | Resume my timer |
| POST | `/timer/stop` | `time.log` | Stop my timer |
| DELETE | `/timer` | `time.log` | Discard my timer |
| GET | `/timesheets` | member; service: `time.read_all` | List timesheets |
| GET | `/timesheets/{timesheet_id}` | member; service: `time.read_all` | Get a timesheet |
| POST | `/timesheets/{timesheet_id}/submit` | member; service rules | Submit a timesheet |
| POST | `/timesheets/{timesheet_id}/recall` | member; service rules | Recall a timesheet |
| POST | `/timesheets/{timesheet_id}/approve` | `timesheet.approve` | Approve a timesheet |
| POST | `/timesheets/{timesheet_id}/reject` | `timesheet.approve` | Reject a timesheet |
| POST | `/timesheets/{timesheet_id}/reopen` | `timesheet.approve` | Reopen an approved timesheet |
| DELETE | `/timesheets/{timesheet_id}` | member; service: `time.log`, `time.manage_all` | Delete a timesheet |
| GET | `/approvals` | member; service: `time.read_all` | Approvals queue |
| POST | `/approvals/bulk-approve` | `timesheet.approve` | Approve many timesheets |

## Disbursements and WIP

Routers (27 routes): `disbursements.py`, `wip.py`, `wip_views.py`, `wip_adjustments.py`, `wip_transfers.py`. Module doc: [billing](modules/billing.md), [wip](modules/wip.md).

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/disbursements` | member; service: `wip.read`, `disbursement.log` | List disbursements |
| POST | `/disbursements` | member; service: `disbursement.log`, `disbursement.manage` | Record a disbursement |
| GET | `/disbursements/{disbursement_id}` | member; service: `wip.read` | Get a disbursement |
| PATCH | `/disbursements/{disbursement_id}` | member; service: `disbursement.log`, `disbursement.manage` | Update a disbursement |
| DELETE | `/disbursements/{disbursement_id}` | member; service: `disbursement.log`, `disbursement.manage` | Delete a disbursement |
| GET | `/wip` | `wip.read` | WIP by client |
| GET | `/clients/{client_id}/wip` | `wip.read` | A client's WIP items |
| GET | `/clients/{client_id}/billing-summary` | `wip.read` | A client's billing summary |
| GET | `/clients/{client_id}/recovery` | `wip.read` | A client's invoices and recovery |
| POST | `/wip/write-offs` | `wip.write_off` | Write off WIP |
| GET | `/wip/write-offs` | `wip.read` | List write-offs |
| GET | `/wip/write-offs/{write_off_id}` | `wip.read` | Get a write-off |
| DELETE | `/wip/write-offs/{write_off_id}` | `wip.write_off` | Reverse a write-off |
| GET | `/wip/summary` | `wip.read` | WIP grouped by client, job, partner, manager or service |
| GET | `/wip/items` | `wip.read` | WIP items |
| GET | `/wip/ledger` | `wip.read` | The WIP ledger |
| POST | `/wip/adjustments` | `wip.adjust` | Write WIP on or off |
| GET | `/wip/adjustments` | `wip.read` | List WIP adjustments |
| GET | `/wip/adjustments/counts` | `wip.read` | Adjustments by status |
| GET | `/wip/adjustments/{adjustment_id}` | `wip.read` | Get a WIP adjustment |
| POST | `/wip/adjustments/{adjustment_id}/approve` | `wip.approve` | Approve a WIP adjustment |
| POST | `/wip/adjustments/{adjustment_id}/reject` | `wip.approve` | Reject a WIP adjustment |
| POST | `/wip/adjustments/{adjustment_id}/reverse` | `wip.adjust` | Reverse a WIP adjustment |
| POST | `/wip/transfers` | `wip.transfer` | Transfer WIP between jobs |
| GET | `/wip/transfers` | `wip.read` | List WIP transfers |
| GET | `/wip/transfers/{transfer_id}` | `wip.read` | Get a WIP transfer |
| POST | `/wip/transfers/{transfer_id}/reverse` | `wip.transfer` | Reverse a WIP transfer |

## Invoices, credit notes, estimates, recurring

Routers (46 routes): `invoices.py`, `credit_notes.py`, `credit_note_wip.py`, `estimates.py`, `recurring.py`. Module doc: [billing](modules/billing.md), [billing overview](billing.md).

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/invoices` | `invoice.read` | List invoices |
| GET | `/invoices/summary` | `invoice.read` | Invoice counts per status |
| POST | `/invoices` | `invoice.create` | Draft an invoice from WIP |
| POST | `/invoices/bulk` | `` | Bulk action on invoices |
| GET | `/invoices/{invoice_id}` | `invoice.read` | Get an invoice |
| PATCH | `/invoices/{invoice_id}` | `invoice.create` | Update an invoice header |
| DELETE | `/invoices/{invoice_id}` | `invoice.create` | Delete a draft or a void invoice |
| POST | `/invoices/{invoice_id}/lines` | `invoice.create` | Add a manual line |
| PUT | `/invoices/{invoice_id}/lines` | `invoice.create` | Save a draft's lines |
| PATCH | `/invoices/{invoice_id}/lines/{line_id}` | `invoice.create` | Update a line |
| DELETE | `/invoices/{invoice_id}/lines/{line_id}` | `invoice.create` | Remove a line |
| POST | `/invoices/{invoice_id}/wip` | `invoice.create` | Add WIP to a draft |
| POST | `/invoices/{invoice_id}/lines/{line_id}/assign` | `invoice.create` | Assign time to fees billed in advance |
| POST | `/invoices/{invoice_id}/lines/{line_id}/unassign` | `invoice.create` | Take time off fees billed in advance |
| POST | `/invoices/{invoice_id}/submit` | `invoice.create` | Submit an invoice for approval |
| POST | `/invoices/{invoice_id}/approve` | `invoice.approve` | Approve an invoice |
| POST | `/invoices/{invoice_id}/reject` | `invoice.approve` | Reject an invoice |
| POST | `/invoices/{invoice_id}/issue` | `invoice.issue` | Issue an invoice |
| POST | `/invoices/{invoice_id}/void` | `invoice.issue` | Void an invoice |
| GET | `/invoices/{invoice_id}/pdf` | `invoice.read` | Invoice PDF |
| POST | `/invoices/{invoice_id}/send` | `invoice.send` | Email an invoice |
| POST | `/invoices/{invoice_id}/credit-notes` | `credit_note.create` | Raise a credit note |
| GET | `/credit-notes` | `invoice.read` | List credit notes |
| GET | `/credit-notes/{credit_note_id}` | `invoice.read` | Get a credit note |
| GET | `/credit-notes/{credit_note_id}/pdf` | `invoice.read` | Credit note PDF |
| POST | `/credit-notes/{credit_note_id}/wip` | `credit_note.create` | Return or write off the WIP of a credit note |
| GET | `/credit-notes/{credit_note_id}/wip` | `invoice.read` | What was done with the WIP of a credit note |
| GET | `/estimates` | `estimate.read` | List estimates |
| GET | `/estimates/summary` | `estimate.read` | Estimate counts per status |
| POST | `/estimates` | `estimate.manage` | Write an estimate |
| GET | `/estimates/{estimate_id}` | `estimate.read` | Get an estimate |
| PATCH | `/estimates/{estimate_id}` | `estimate.manage` | Change a draft estimate |
| DELETE | `/estimates/{estimate_id}` | `estimate.manage` | Delete a draft estimate |
| POST | `/estimates/{estimate_id}/send` | `estimate.manage` | Send an estimate |
| POST | `/estimates/{estimate_id}/accept` | `estimate.manage` | Record that the client accepted |
| POST | `/estimates/{estimate_id}/decline` | `estimate.manage` | Record that the client declined |
| POST | `/estimates/{estimate_id}/convert` | `estimate.manage` + `invoice.create` | Convert an estimate to a draft invoice |
| GET | `/recurring` | `recurring.read` | List recurring invoice schedules |
| POST | `/recurring` | `recurring.manage` | Create a recurring invoice schedule |
| POST | `/recurring/runs/{run_id}/retry` | `recurring.manage` | Retry a failed run |
| GET | `/recurring/{schedule_id}` | `recurring.read` | Get a recurring schedule |
| PATCH | `/recurring/{schedule_id}` | `recurring.manage` | Change a recurring schedule |
| DELETE | `/recurring/{schedule_id}` | `recurring.manage` | Delete a recurring schedule |
| POST | `/recurring/{schedule_id}/pause` | `recurring.manage` | Pause a schedule |
| POST | `/recurring/{schedule_id}/resume` | `recurring.manage` | Resume a schedule |
| GET | `/recurring/{schedule_id}/runs` | `recurring.read` | List the runs of a schedule |

## Payments, debtors, statements

Routers (15 routes): `payments.py`, `debtors.py`, `statement_sends.py`. Module doc: [billing](modules/billing.md).

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/payments` | `payment.read` | List payments |
| POST | `/payments` | `payment.record` | Record a payment |
| GET | `/payments/{payment_id}` | `payment.read` | Get a payment |
| POST | `/payments/{payment_id}/allocations` | `payment.record` | Allocate a payment |
| DELETE | `/payments/{payment_id}/allocations/{allocation_id}` | `payment.record` | Remove an allocation |
| POST | `/payments/{payment_id}/void` | `payment.record` | Void a payment |
| POST | `/payments/{payment_id}/reverse` | `payment.record` | Reverse a payment |
| GET | `/clients/{client_id}/statement` | `invoice.read` + `payment.read` | Client statement |
| GET | `/clients/{client_id}/statement/pdf` | `invoice.read` + `payment.read` | Client statement PDF |
| GET | `/debtors` | member; service: `payment.read`, `invoice.read`, `report.financial` | Aged debtors |
| GET | `/debtors/reminders` | member; service: `payment.read`, `invoice.read`, `report.financial` | Payment reminder history |
| POST | `/debtors/reminders` | `payment.record` | Send payment reminders |
| GET | `/debtors/{client_id}/invoices` | member; service: `payment.read`, `invoice.read`, `report.financial` | A debtor's open invoices |
| POST | `/clients/{client_id}/statement/send` | `invoice.read` + `payment.read` + `payment.record` | Email a client statement |
| GET | `/statements` | `invoice.read` + `payment.read` | Statements list |

## Reports

Routers (14 routes): `wip_reports.py`, `reports.py`. Module doc: [reports](modules/reports.md).

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/reports/wip` | `report.financial` | Work in progress |
| GET | `/reports/wip/items` | `report.financial` | The items behind the WIP report |
| GET | `/reports/aged-debtors` | `report.financial` | Aged debtors |
| GET | `/reports/time-summary` | `report.read` | Time summary |
| GET | `/reports/utilisation` | `report.read` | Utilisation |
| GET | `/reports/recoverability` | `report.financial` | Recoverability |
| GET | `/reports/billing-summary` | `report.financial` | Billing summary |
| GET | `/reports/job-profitability` | `report.financial` | Job profitability |
| GET | `/reports/vat-summary` | `report.financial` | VAT summary |
| GET | `/reports/expenses` | member; service: `report.read`, `report.financial`, `expense.read_all`, `expense.manage` … | Expenses |
| GET | `/reports/payments` | `report.financial` | Payments |
| GET | `/reports/missing-timesheets` | `report.read` | Missing timesheets |
| GET | `/reports/recovery-analysis` | `report.financial` | Recovery analysis |
| GET | `/reports/cost-to-business` | `report.financial` | Cost to Business |

## Dashboard

Routers (9 routes): `dashboard.py`, `dashboard_pages.py`. Module doc: [dashboard](modules/dashboard.md), [reports](modules/reports.md).

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/dashboard` | any member | Dashboard |
| GET | `/dashboard/cards` | member; service: `time.log`, `time.read_all`, `timesheet.approve`, `wip.read` … | Dashboard cards |
| GET | `/dashboard/my-actions` | member; service rules | My actions |
| GET | `/dashboard/my-timesheet` | member; service: `time.log` | My timesheet this week |
| GET | `/dashboard/pending-approvals` | member; service: `timesheet.approve`, `time.read_all`, `expense.approve`, `expense.read_all` … | Pending approvals |
| GET | `/dashboard/missing-timesheets` | member; service: `time.read_all`, `timesheet.approve` | Missing timesheets |
| GET | `/dashboard/top-clients-by-wip` | member; service: `wip.read`, `report.financial` | Top clients by WIP |
| GET | `/dashboard/billing-trend` | member; service: `invoice.read`, `report.financial` | Billing trend |
| GET | `/dashboard/debtor-ageing` | member; service: `payment.read`, `invoice.read`, `report.financial` | Debtor ageing |

## Notifications and email templates

Routers (13 routes): `notifications.py`, `email_templates.py`. Module doc: [notifications](modules/notifications.md).

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/notifications` | any member | List my notifications |
| GET | `/notifications/unread-count` | member; service rules | Count my unread notifications |
| POST | `/notifications/read-all` | member; service rules | Mark all my notifications as read |
| POST | `/notifications/{notification_id}/read` | member; service rules | Mark a notification as read |
| GET | `/notification-settings` | member; service rules | Get my notification settings |
| PUT | `/notification-settings` | member; service rules | Update my notification settings |
| GET | `/notification-settings/practice` | member; service: `organization.update`, `settings.manage` | Get the practice's notification settings |
| PUT | `/notification-settings/practice` | member; service: `organization.update`, `settings.manage` | Update the practice's notification settings |
| GET | `/email-templates` | member; service: `organization.update`, `settings.manage` | List the email templates |
| GET | `/email-templates/{key}` | member; service: `organization.update`, `settings.manage` | Get an email template |
| PUT | `/email-templates/{key}` | member; service: `organization.update`, `settings.manage` | Customise an email template |
| DELETE | `/email-templates/{key}` | member; service: `organization.update`, `settings.manage` | Reset an email template to its default |
| POST | `/email-templates/{key}/preview` | member; service: `organization.update`, `settings.manage` | Preview an email template |

## Tickets (Application Testing)

Routers (17 routes): `tickets.py`, `ticket_tags.py`. Module doc: [tickets](modules/tickets.md).

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/tickets/dashboard` | `ticket.read` | Application Testing dashboard |
| POST | `/tickets/bulk` | `ticket.update` | Bulk action on tickets |
| GET | `/tickets` | `ticket.read` | List tickets |
| POST | `/tickets` | `ticket.create` | Create a ticket |
| GET | `/tickets/{ticket_id}` | `ticket.read` | Get a ticket |
| PATCH | `/tickets/{ticket_id}` | `ticket.update` | Update a ticket |
| GET | `/tickets/{ticket_id}/timeline` | `ticket.read` | A ticket's timeline |
| GET | `/tickets/{ticket_id}/comments` | `ticket.read` | List a ticket's comments |
| POST | `/tickets/{ticket_id}/comments` | `ticket.update` | Add a comment |
| GET | `/tickets/{ticket_id}/attachments` | `ticket.read` | List a ticket's files |
| POST | `/tickets/{ticket_id}/attachments` | `ticket.update` | Upload a file |
| GET | `/tickets/{ticket_id}/attachments/{attachment_id}` | `ticket.read` | Download a file |
| DELETE | `/tickets/{ticket_id}/attachments/{attachment_id}` | `ticket.update` or `ticket.delete` | Delete a file |
| GET | `/ticket-tags` | `ticket.read` | List ticket tags |
| POST | `/ticket-tags` | `ticket.update` | Create a ticket tag |
| PATCH | `/ticket-tags/{tag_id}` | `ticket.update` | Update a ticket tag |
| DELETE | `/ticket-tags/{tag_id}` | `ticket.delete` | Delete a ticket tag |

## Expenses

Routers (25 routes): `expense_types.py`, `expenses.py`, `expense_receipts.py`. Module doc: [expenses](modules/expenses.md).

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/expense-types` | member; service: `expense.log`, `expense.read_all`, `expense.manage` | List expense types |
| POST | `/expense-types` | member; service: `expense.manage` | Create an expense type |
| GET | `/expense-types/{type_id}` | member; service: `expense.log`, `expense.read_all`, `expense.manage` | Get an expense type |
| PATCH | `/expense-types/{type_id}` | member; service: `expense.manage` | Update an expense type |
| DELETE | `/expense-types/{type_id}` | member; service: `expense.manage` | Delete an expense type |
| GET | `/expense-claims` | member; service: `expense.read_all`, `expense.manage`, `expense.approve` | List expense claims |
| POST | `/expense-claims` | member; service: `expense.log`, `expense.manage` | Create an expense claim |
| GET | `/expense-claims/summary` | member; service: `expense.approve` | Expense claim summary |
| GET | `/expense-claims/{claim_id}` | member; service: `expense.read_all`, `expense.manage`, `expense.approve` | Get an expense claim |
| PATCH | `/expense-claims/{claim_id}` | member; service: `expense.log`, `expense.manage` | Update an expense claim |
| DELETE | `/expense-claims/{claim_id}` | member; service: `expense.log`, `expense.manage` | Delete an expense claim |
| POST | `/expense-claims/{claim_id}/lines` | member; service: `expense.manage`, `expense.log` | Add a line to an expense claim |
| PATCH | `/expense-claims/{claim_id}/lines/{line_id}` | member; service: `expense.manage` | Update a line of an expense claim |
| DELETE | `/expense-claims/{claim_id}/lines/{line_id}` | member; service rules | Delete a line of an expense claim |
| POST | `/expense-claims/{claim_id}/submit` | member; service: `expense.approve`, `expense.read_all`, `expense.log`, `expense.manage` | Submit an expense claim |
| POST | `/expense-claims/{claim_id}/recall` | member; service: `expense.log`, `expense.manage` | Recall an expense claim |
| POST | `/expense-claims/{claim_id}/approve` | `expense.approve` | Approve an expense claim |
| POST | `/expense-claims/{claim_id}/return` | `expense.approve` | Return an expense claim |
| GET | `/expense-approvals` | `expense.approve` | Expense approvals queue |
| POST | `/expense-approvals/bulk-approve` | `expense.approve` | Approve many expense claims |
| GET | `/expenses` | member; service rules | List expense lines |
| POST | `/expenses/{line_id}/receipts` | member; service: `expense.log`, `expense.manage` | Upload a receipt |
| GET | `/expenses/{line_id}/receipts/{receipt_id}` | member; service rules | Download a receipt |
| DELETE | `/expenses/{line_id}/receipts/{receipt_id}` | member; service: `expense.log`, `expense.manage` | Delete a receipt |
| GET | `/expense-receipts` | member; service rules | List receipts |

## Gaps

* Onboarding (`onboarding.py`) and the integration routers (`integrations.py`,
  `integration_routes.py` and the five it includes) have no module doc. Code and tests refer to
  `docs/modules/onboarding.md` (`alembic/versions/036_onboarding_progress.py`,
  `tests/onboarding_helpers.py`) and `docs/modules/integrations.md` (`app/core/config.py`,
  `app/services/integration_events.py`, `.env.example`), which do not exist. Until they do, the
  route descriptions in `/docs` are the contract.
* Jobs are documented in [modules/time.md](modules/time.md); `app/services/time_jobs.py` refers to
  a `docs/modules/jobs.md` that does not exist.
