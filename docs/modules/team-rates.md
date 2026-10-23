# Team & Rates: staff, grades, rates, services and activity types

Code: `app/api/v1/{staff,grades,rate_rules,rates,settings}.py` → `app/services/{staff,staff_login,grades,rate_rules,rate_preview,rerate,rates,settings,charge_rates}.py`
→ `app/repositories/{staff,grades,rate_rules,rate_history,rate_reruns,rates,settings,time_rates}.py`;
schemas in `app/schemas/{staff,grades,rate_rules,rates,settings}.py`; models `app/db/models/{organization,practice,rates}.py`.
Tests: `tests/test_staff.py`, `test_staff_login.py`, `test_grades.py`, `test_rates_v2.py`, `test_rerate.py`,
`test_services_activities.py`, `test_team_rates_openapi.py` (helpers in `tests/team_helpers.py`).
Migrations: `010` (grades, staff profile), `011` (rate rules, history, re-rating), `012` (services, activity types, links).
**No permission was added**: everything uses the existing codes (see [Permissions](#permissions)).

All paths are under `/api/v1/organizations/{organization_id}`. Another practice's ids answer 404. The rules
behind the endpoints are in `docs/domain.md` (Staff and grades, Rates, Practice settings). The interactive
schema is `/openapi.json`; every route there has a summary, a description and its error responses.

## Conventions

| Thing | Representation |
|---|---|
| Envelope | Success `{"success": true, "data": …, "message": "…", "meta": …}`. Error `{"success": false, "error": {"code", "message", "details", "request_id"}}`. |
| `error.details` | A list of `{field, code, message}` (validation, duplicates), an object for some conflicts (`RATE_OVERLAP`, `STAFF_IN_USE`, …) or `null`. Field names are request field names; items of a batch are `entries.2.field`. |
| Lists | Cursor-paginated: query `limit` (1–100, default 25) and `cursor`; response `meta {next_cursor, has_more}`. Use a cursor only with the filters, `sort` and `order` it came from; an invalid cursor is 422 on `cursor`. Services, activity codes and the V1 rate lists are bounded and not paginated. |
| Ids | UUIDs. A staff member's id **is** their user id: it is the `user_id` of rates, jobs, time entries and timesheets. |
| Money | Integer pence. Rates are pence per hour (`rate`, `charge_rate`, `cost_rate`, `default_*_rate`). |
| Percentages | Basis points: `7500` = 75% (`chargeable_target_bp`, `default_chargeable_target_bp`). |
| Dates | `YYYY-MM-DD`. "Today", rate periods and `status` of a rate use the practice's time zone. Timestamps are UTC. |
| Cost masking | A caller without `rate.read` gets `null` for every cost figure and for the staff `charge_rate`/`cost_rate` and their sources (listed per endpoint). `rate.manage` alone does not show costs. |
| Write responses | Creates answer 201 with the record, updates 200 with the record, deletes 200 with `data: null` (grades and services: `{outcome, id}`). |
| Body required | POSTs that take a body need a JSON body even when every field is optional: send `{}` (`deactivate`, `invite`). |

## Endpoint index

| Method | Path | Permission |
|---|---|---|
| GET | `/staff` | `member.read` |
| POST | `/staff` | `member.invite` |
| GET | `/staff/{staff_id}` | `member.read` |
| PATCH | `/staff/{staff_id}` | `member.update` |
| DELETE | `/staff/{staff_id}` | `member.remove` |
| POST | `/staff/bulk` | `member.update` (`deactivate` and `delete`: `member.remove`) |
| POST | `/staff/{staff_id}/deactivate` | `member.remove` |
| POST | `/staff/{staff_id}/reactivate` | `member.update` |
| POST | `/staff/{staff_id}/invite` | `member.invite` |
| POST | `/staff/{staff_id}/link-login` | `member.invite` |
| POST | `/staff/{staff_id}/unlink-login` | `member.remove` |
| GET | `/grades` | `member.read` |
| POST | `/grades` | `rate.manage` |
| GET | `/grades/{grade_id}` | `member.read` |
| PATCH | `/grades/{grade_id}` | `rate.manage` |
| DELETE | `/grades/{grade_id}` | `rate.manage` |
| POST | `/grades/{grade_id}/activate` · `/deactivate` | `rate.manage` |
| GET | `/rates` | `rate.read` |
| POST | `/rates` | `rate.manage` |
| GET | `/rates/{rule_id}` | `rate.read` |
| PATCH | `/rates/{rule_id}` | `rate.manage` |
| DELETE | `/rates/{rule_id}` | `rate.manage` |
| GET | `/rates/resolve` | `rate.read` |
| GET | `/rates/history` | `rate.read` |
| POST | `/rates/rerate/preview` | `rate.manage` |
| POST | `/rates/rerate` | `rate.manage` |
| GET | `/rates/reruns` | `rate.read` |
| GET | `/rates/reruns/{run_id}` | `rate.read` |
| GET | `/rates/reruns/{run_id}/items` | `rate.read` |
| POST | `/rates/revalue` (V1) | `rate.manage` |
| GET, POST | `/members/{user_id}/rates` (V1) | `rate.read`, `rate.manage` |
| DELETE | `/staff-rates/{rate_id}` (V1) | `rate.manage` |
| GET | `/staff-rates/current` (V1) | `rate.read` |
| GET, POST | `/clients/{client_id}/rates` (V1) | `rate.read`, `rate.manage` |
| PATCH, DELETE | `/clients/{client_id}/rates/{rate_id}` (V1) | `rate.manage` |
| GET | `/services`, `/services/{service_id}` | any member |
| POST | `/services` | `settings.manage` |
| PATCH, DELETE | `/services/{service_id}` | `settings.manage` |
| POST | `/services/{service_id}/activate` · `/deactivate` | `settings.manage` |
| GET | `/services/{service_id}/activity-codes` | any member |
| PUT | `/services/{service_id}/activity-codes` | `settings.manage` |
| GET | `/activity-codes`, `/activity-codes/{activity_id}` | any member |
| POST | `/activity-codes` | `settings.manage` |
| PATCH, DELETE | `/activity-codes/{activity_id}` | `settings.manage` |
| POST | `/activity-codes/{activity_id}/activate` · `/deactivate` | `settings.manage` |
| GET, PATCH | `/sequences`, `/sequences/{kind}` | any member, `organization.update` (new kind `staff`) |

`/rates/resolve`, `/rates/history` and `/rates/reruns…` are declared before `/rates/{rule_id}`, so those
words are never read as an id.

---

# Staff

A staff member is a member of the practice (`organization_members`) with a **staff code**, a **grade**, an
**approver**, working hours and days, a chargeable target, a start and a leaving date and a **status**.
Staff are people the practice records time for; a *login* is optional.

* **Without a login** a person is a *placeholder*: the API creates an inactive account for them behind the
  scenes (non-routable email, unusable password) so time, rates, jobs and timesheets can refer to them like to
  anyone else. `has_login=false`, `login_status="none"`. They cannot sign in or be an approver, and an owner
  without a login does not count towards the last-owner guard. The UI never sees the placeholder account;
  `email` is their **work email**.
* **Giving a login**: `invite` (they choose a password through an emailed link), `link-login` with
  `create_account` (account made now, temporary password shown once) or `existing_user` (merge into a
  person who already signs in). **Removing it**: `unlink-login`.
* **Status**: `active`; `inactive` (paused, can come back); `left` (no longer works for the practice, leaving
  date set). The V1 members endpoints call them `suspended` and `removed`; both vocabularies are stored and
  accepted: `suspended` reads as `inactive`, `removed` as `left` on every `/staff` response and filter.
* `is_selectable` = `active` **and** not past `leaving_date`: the people the Time module accepts time for.
  `GET /staff?selectable=true` is the picker for "log time for", assignees and approvers (add
  `has_login=true` for approvers).
* Everything recorded for a person (time, expenses, invoices, rates, history) is kept after they are
  deactivated or leave. A record nothing refers to can be deleted.
* The practice always keeps one **active owner who can sign in**; only an owner changes another owner; nobody
  deactivates, deletes or un-logs themselves.
* The practice's founder and people created through the V1 `/people` endpoints are staff too: they have a
  code from the `staff` sequence, working hours from the practice defaults, and **no grade and no start
  date** (`grade: null`, `start_date: null`) until someone sets them (`PATCH /staff/{id}`).

## GET /staff

Permission `member.read`. Query:

| Param | Type | Meaning |
|---|---|---|
| `q` | string ≤ 200 | Case-insensitive substring of name, staff code, work email and (when they have a login) sign-in email. |
| `status` | `active` / `inactive` / `left`, repeatable | Default `active` and `inactive` (people who left are hidden). |
| `grade_id` | uuid | Only this grade. |
| `approver_id` | uuid | Only staff whose approver is this person. |
| `role_id` | uuid | Only this role. |
| `has_login` | bool | `true` can sign in, `false` placeholders. |
| `selectable` | bool | `true`: active and not past the leaving date; `false`: the others. |
| `sort` | `name` (default), `code`, `email`, `grade`, `status`, `start_date`, `leaving_date` | `name`, `code` and `email` ignore case; `grade` follows the grades' `sort_order` (no grade last); `status` orders active, inactive, left; a null `start_date` sorts first, a null `leaving_date` last. |
| `order` | `asc` (default), `desc` | |
| `limit`, `cursor` | | See Conventions. |

Response: `StaffRead[]` with `meta`. `charge_rate`, `charge_rate_source`, `cost_rate`, `cost_rate_source` are
null without `rate.read`. Staff with no login are included. One page costs a fixed number of queries,
whatever its size.

## POST /staff

Permission `member.invite`. Creates a staff member **without a login**. Response 201: `StaffDetail`.

| Field | Type | Required | Notes |
|---|---|---|---|
| `full_name` | string 1–120 | yes | |
| `grade_id` | uuid | yes | An **active** grade of the practice (422 on `grade_id` otherwise). |
| `start_date` | date | yes | |
| `code` | string 1–20 | no | Upper-cased; letters, digits, `_`, `-`. Default: the next number of the `staff` sequence (`S001`, `S002` …). Unique in the practice, including people who left: 409 `STAFF_CODE_TAKEN`. |
| `email` | email ≤ 254 | no | Work email. Not shared by another **active** staff member (409 `STAFF_EMAIL_TAKEN`, also when it is somebody's sign-in address). Blank = null. |
| `approver_id` | uuid | no | Must be active **and able to sign in**, and not the person themself (422 on `approver_id`). |
| `job_title` | string ≤ 120 | no | |
| `initials` | string | no | Upper-cased, unique in the practice (409 `INITIALS_TAKEN`). |
| `working_minutes_per_day` | int 1–1440 | no | Default: the practice's minutes per day. |
| `working_days` | int[] 1–7 | no | ISO weekdays (1 = Monday), unique, 1–7 items. Default: the practice's working days. |
| `chargeable_target_bp` | int 0–10000 | no | Default: the grade's `default_chargeable_target_bp`, else 7500. |
| `leaving_date` | date | no | Not before `start_date` (422). |
| `role_id` | uuid | no | The role they get once they have a login. Default: the Staff role. 422 when unknown. |
| `cost_rate` | integer pence/hour | no | Also creates the person's own **cost** rate (a `staff`-level rule) from `rate_effective_from`. Needs `rate.manage` besides `member.invite` (403 otherwise, and nothing is created). |
| `charge_rate` | integer pence/hour | no | Same for the person's own **charge-out** rate. |
| `rate_effective_from` | date | no | When those rates start; default `start_date`. A date in the future leaves the grade or practice default in force until then. |

The person and their rates are written in **one transaction**; each rate writes the usual `rate.created` audit entry and a rate-history row (source `staff_rates`).

`weekly_capacity_minutes` = `working_minutes_per_day` × number of working days (used by the utilisation
reports). Example response (shortened, see `StaffDetail`):

```json
{
  "id": "01a14bbf-f0e0-7362-8665-0360fdaecf59", "code": "S002", "full_name": "Sam Staff",
  "email": "sam@example.com", "has_login": false, "login_email": null, "login_status": "none",
  "invited_at": null, "status": "active", "is_selectable": true,
  "grade": {"id": "01a14bbf-ef02-7007-99e0-0c58d5c0124c", "code": "SNR", "name": "Senior"},
  "approver": null, "role": {"id": "…", "name": "Staff", "system_key": "staff"},
  "job_title": "Accountant", "initials": "SS", "working_minutes_per_day": 450,
  "working_days": [1, 2, 3, 4], "weekly_capacity_minutes": 1800, "chargeable_target_bp": 7500,
  "start_date": "2026-01-05", "leaving_date": null,
  "charge_rate": 0, "charge_rate_source": "default", "cost_rate": 0, "cost_rate_source": "none",
  "joined_at": "2026-10-17T21:23:52.627354Z", "updated_at": "2026-10-17T21:23:52.627354Z",
  "usage": [], "can_delete": true
}
```

## GET /staff/{staff_id}

Permission `member.read`. Any status. Response `StaffDetail`: `StaffRead` plus `usage` (what refers to the
person, for example `[{"kind": "time_entries", "label": "Time entries", "count": 12}]`, empty when nothing)
and `can_delete` (`usage` is empty). 404 for an unknown id.

## PATCH /staff/{staff_id}

Permission `member.update`. Every field optional; send only what changes. Response `StaffDetail`.

| Field | Notes |
|---|---|
| `full_name`, `code`, `grade_id`, `working_minutes_per_day`, `working_days`, `chargeable_target_bp` | Cannot be null. `grade_id` must be an active grade (the grade a person already holds may be inactive and stays). |
| `email`, `approver_id`, `job_title`, `initials`, `start_date`, `leaving_date` | Null clears them. `email` is the **work** email; the sign-in address is not changed here. |

* Changing hours or days recalculates `weekly_capacity_minutes`.
* `leaving_date` must not be before `start_date` (422 on `leaving_date`). A past leaving date stops new time
  after it (the Time module answers 422 `after_leaving_date`) but does not change the status: use
  `deactivate` to mark someone as left.
* `full_name` lives on the person's account: 409 `ACCOUNT_SHARED` when they can sign in and also belong to
  another practice.
* 409 `STAFF_CODE_TAKEN`, `STAFF_EMAIL_TAKEN`, `INITIALS_TAKEN` for duplicates; 422 for unknown ids.
* Their **role** and V1 status are changed with `PATCH /members/{user_id}` (`member.update`).

## POST /staff/bulk

Permission `member.update`; `deactivate` and `delete` also need `member.remove` (403 otherwise). One action on 1-200 staff members
(`staff_ids`, duplicates ignored), all or nothing. Body: `{"staff_ids": [...], "action": ..., "value": ..., "leaving_date": ...}`.

| `action` | `value` | What it does |
|---|---|---|
| `set_grade` | grade id | `PATCH /staff/{id}` with that grade |
| `set_approver` | staff id (someone who can sign in) | `PATCH` with that approver |
| `clear_approver` | none | `PATCH` with `approver_id: null` |
| `deactivate` | `inactive` (default) or `left` | as `POST /staff/{id}/deactivate`; `left` takes an optional `leaving_date` (default today) |
| `reactivate` | none | as `POST /staff/{id}/reactivate` |
| `delete` | none | as `DELETE /staff/{id}` |

Each person goes through the **same service call** as the single endpoint, so every rule holds (the practice keeps an administrator, only an owner changes an
owner, nobody deactivates or deletes themselves, a person that records refer to cannot be deleted). Response `{action, affected, changed}`: `affected` is the number
of distinct people, `changed` those that actually changed (a person already in the wanted state is skipped). If any person is refused the whole batch answers
**409 `BULK_REFUSED`** with `details = [{staff_id, number (the staff code), name, error_code, reason}]` for every one and **nothing changes**; an unknown or foreign id answers
404 with `details.staff_ids`. Besides the usual per-person entries one `staff.bulk_changed` audit entry records `action`, `count`, `codes` (at most 100) and `bulk: true`.

## POST /staff/{staff_id}/deactivate

Permission `member.remove`. Body (send `{}` for the default): `status` `inactive` (default; paused) or `left`;
`leaving_date` (for `left`: default the existing leaving date, else today; ignored for `inactive`).
Response `StaffDetail`.

* `left` sets `leaving_date` (422 when before `start_date`) and clears the approver of everyone they
  approved.
* A person with a login is signed out everywhere (unless their account also belongs to another practice) and
  cannot sign in again until reactivated.
* Idempotent: asking for the state they are already in succeeds without changes.
* 403 for yourself; 403 when the person is an owner and you are not; 409 (`CONFLICT`, `details.code` =
  `last_owner`) for the last active owner who can sign in; 409 `STAFF_LEFT` when asking `inactive` for someone
  who has left (reactivate first).

## POST /staff/{staff_id}/reactivate

Permission `member.update`. No body. Sets the status to `active` and clears the leaving date; idempotent. 409
`STAFF_EMAIL_TAKEN` when another active staff member now uses their work email; 403 for an owner when you are
not one. Response `StaffDetail`. A person who could sign in before can sign in again.

## DELETE /staff/{staff_id}

Permission `member.remove`. Deletes a record nothing refers to (`can_delete`). Otherwise 409 `STAFF_IN_USE`:

```json
{"code": "STAFF_IN_USE", "message": "…Deactivate them instead.",
 "details": {"usage": [{"kind": "time_entries", "label": "Time entries", "count": 1},
                       {"kind": "timesheets", "label": "Timesheets", "count": 1}]}}
```

`usage` kinds: `time_entries, timesheets, timers, disbursements, invoices, credit_notes, payments, jobs,
job_assignees, clients, rate_rules, rate_changes, rate_reruns, rate_rerun_items, wip_allocations,
wip_write_offs, organizations`. 403 for yourself or (as a non-owner) an owner; 409 `CONFLICT`
(`details.code` = `last_owner`) for the last owner who can sign in. Response 200, `data: null`. People who
could sign in are signed out and their account is closed unless it also belongs to another practice.

## POST /staff/{staff_id}/invite

Permission `member.invite`. Body (send `{}` for defaults): `email` (sign-in address; default the work
email), `role_id` (the role they get; first invitation only). Response `StaffInvited`:

```json
{"staff": { …StaffDetail with "has_login": true, "login_email": "sam.login@example.com",
            "login_status": "invited", "invited_at": "…" },
 "invited_at": "2026-10-17T21:23:53.132368Z", "expires_at": "2026-10-24T21:23:53.152189Z", "resent": false}
```

* The person gets a pending login and an email (template `staff_invitation`, through the job queue) with a
  one-time link, valid 7 days, to the frontend's `/reset-password?token=…` page; they choose a password there
  and then sign in at `/login`. Until they do, `login_status` is `invited`; afterwards `active`.
* Calling it again for someone still `invited` re-issues the link (`resent: true`; earlier links stop
  working). The role and email of a pending login cannot be changed this way.
* 409 `EMAIL_TAKEN` (the address already belongs to an account), `STAFF_EMAIL_TAKEN`, `LOGIN_ALREADY_ACTIVE`
  (they already chose a password), `STAFF_NOT_ACTIVE` (reactivate first); 422 `email` (code `required`) when no
  email is known; 403 when the caller may not hand out the role (same rule as `POST /people`).

## POST /staff/{staff_id}/link-login

Permission `member.invite`. For a staff member **without** a login (409 `LOGIN_ALREADY_LINKED` otherwise;
409 `STAFF_NOT_ACTIVE`). Body:

| Field | Type | Notes |
|---|---|---|
| `mode` | `create_account` or `existing_user` | Required. |
| `email` | email | `create_account`: the sign-in address (default the work email; 422 when none, 409 `EMAIL_TAKEN`/`STAFF_EMAIL_TAKEN`). `existing_user`: **required**, the address of the practice member who already signs in. |
| `role_id` | uuid | `create_account`: the role (default the one recorded on the staff member); 403 when the caller may not hand it out. |
| `send_email` | bool, default true | `create_account`: also email a link to choose a password (template `account_created`). |

Response `StaffLinked`, with the header `Cache-Control: no-store`:

```json
{"staff": { …StaffDetail… }, "temporary_password": "6N6sta69wCxoUTpH", "email_sent": false, "merged_from": null}
```

* `create_account`: the account exists at once (`login_status` `active`); `temporary_password` is returned
  **once** and must be changed at first sign-in. Show it to the administrator; it cannot be read again.
  `merged_from` is null.
* `existing_user`: the placeholder record is **merged into** the active staff member of this practice who signs
  in with `email` (422 on `email` when there is none). That member keeps their account and role and takes over
  the placeholder's code, grade, working hours and dates, plus its job title, initials and approver when they
  have none; everything recorded for the placeholder (time, timesheets, rates, jobs, approvals, history) moves
  to them. The response's `staff.id` is the **login member's id**; `merged_from` is the placeholder's old id
  (any link to it is now dead). `temporary_password` is null, `email_sent` false. 409 `LINK_CONFLICT`
  (`details.table`) when both have a record that cannot be combined (for example a timesheet for the same
  week); nothing is changed then.

## POST /staff/{staff_id}/unlink-login

Permission `member.remove`. No body. The person keeps their record and history but cannot sign in: sessions
are revoked, pending password links stop working, the account becomes a placeholder, their sign-in address
becomes the work email when they had none, and everyone they approved loses their approver. Response
`StaffDetail` (`has_login: false`). 409 `NO_LOGIN`; 409 `ACCOUNT_SHARED` when the account also belongs to
another practice; 403 for yourself or (as a non-owner) an owner; 409 `CONFLICT` (`last_owner`) for the last
owner who can sign in.

---

# Grades

A grade is a level of staff (Partner, Manager, Senior, Junior seeded) with default rates and a default
chargeable target. The default rates are the **baseline of the grade's rate level**: a dated grade rate (see
Rates) overrides them while it is in force. Changing a default never rewrites recorded time and is appended to
the rate history (`source` `grade_default`). A new practice's grades have targets 5000, 7000, 7500 and 8000
and no default rates.

| Method, path | Notes |
|---|---|
| `GET /grades` | Query `q` (name, code, description), `is_active`, `sort` (`sort_order` default, `name`, `code`), `order`, `limit`, `cursor`. `GradeRead[]` + `meta`. |
| `POST /grades` | Body `code` (1–12, upper-cased, letters/digits/`_`/`-`), `name` (1–80), `description` (≤ 500, blank = null), `default_cost_rate`, `default_charge_rate` (pence per hour ≥ 0 or null), `default_chargeable_target_bp` (0–10000 or null), `sort_order` (0–10000, default 0), `is_active` (default true). 201 `GradeRead`. 409 `GRADE_CODE_TAKEN`, `GRADE_NAME_TAKEN` (case-insensitive). |
| `GET /grades/{grade_id}` | `GradeRead`. |
| `PATCH /grades/{grade_id}` | Every field optional. Null clears `description` and the three defaults; `code`, `name`, `sort_order` and `is_active` cannot be null. Same 409s. |
| `POST /grades/{grade_id}/deactivate`, `/activate` | `GradeRead`. A deactivated grade cannot be given to staff; staff who hold it keep it. |
| `DELETE /grades/{grade_id}` | Deletes an unused grade (`{"outcome": "deleted", "id"}`); a grade held by any staff member (whatever their status) or named by a rate is deactivated instead (`{"outcome": "deactivated", "id"}`). |

Read needs `member.read`, writes `rate.manage`. `default_cost_rate` is null without `rate.read` (it is also
null when the grade sets none). `GradeRead.staff_count` counts staff who have not left; `can_delete` is false
once staff or rates refer to the grade.

---

# Rates

Two kinds: **charge** (what the practice bills per hour) and **cost** (what the person costs per hour). A rate
is a *rule*: a kind, a scope, a rate in pence per hour and a period `effective_from`–`effective_to`
(inclusive, open-ended when `effective_to` is null).

## Levels and precedence

The scope (which of `client_id`, `service_id`, `user_id` = staff member, `grade_id` are sent) decides the
rule's `level`:

| `level` | Sent | Charge precedence | Cost |
|---|---|---|---|
| `client_service_staff` | client + service + staff | 1 | no |
| `client_service` | client + service | 2 | no |
| `client_staff` | client + staff | 3 | no |
| `client` | client alone (everyone) | 3, after `client_staff` | no |
| (job rate) | `jobs.hourly_rate` | 4 | no |
| `service_grade` | service + grade | 5 | no |
| `staff` | staff alone | 6 | yes |
| `grade` | grade alone | 7 | yes |

Any other combination is 422 `invalid_scope` (field `scope`). Cost rates exist only for `staff` and `grade`
(422 `invalid_kind`, field `kind`).

The **charge-out rate** of a time entry is the first that exists of: level 1 (client, the job's service and the
person), 2, 3 (the client's rate for the person, else the client's rate for everyone), 4 (the job's own hourly
rate; undated), 5 (the job's service and the person's *current* grade), 6, 7 (a grade rule in force, else the
grade's `default_charge_rate`), and finally the practice's `default_charge_rate`. A rule counts only on the days
inside its period. The **cost rate** is the person's `staff` cost rule, else the grade's cost rule, else the
grade's `default_cost_rate`, else 0.

The result and its source are saved on each entry when it is saved (`charge_rate`, `cost_rate`, values and
`rate_source`, one of `client_service_staff, client_service, client_staff, client, job, service_grade, staff,
grade, default`). A rate change never touches recorded entries. An unbilled entry is re-priced only when it
is itself changed (the Time module's rule: an edit re-takes the rate from the ladder as it is then) or by
re-rating.

**Rate mode.** The practice setting `rate_mode` (`PATCH /organizations/{id}`, `organization.update`, default
`staff_first`) can be `grade_only`: level 6 (a `staff` charge rule) is then **skipped**, so a person's own rate
never prices time and their grade's rate does. Levels 1–5, 7 and the practice default are unchanged, and so is the
**cost** rate (a person's cost rule still comes first). The `staff` rules are kept: they can still be listed,
created and edited, and they price time again as soon as the mode is switched back. One resolver
(`app/services/charge_rates.py`) serves a time entry that is saved, `GET /rates/resolve` and re-rating, so the
preview always shows what saving or re-rating will do: in `grade_only` mode the `staff` step of `charge_ladder`
comes back with `applicable: false` and the reason "This practice prices by grade only: staff rates are not
used.". Changing the mode never touches recorded entries; they follow it only when they are edited or re-rated.

## Period rules

* Rules of one kind and scope never overlap: 409 `RATE_OVERLAP` with `details.conflicts` = the rules in the
  way (`{id, effective_from, effective_to}`). The database enforces it with an exclusion constraint, so two
  concurrent requests cannot both succeed.
* `effective_to` must not be before `effective_from` (422).
* `close_previous: true` on create ends the earlier open-ended rule of the same scope the day before the new
  one starts. It only applies when that is a plain shortening (one earlier rule that started before the new
  period); otherwise 409 `RATE_OVERLAP`.
* A rule is **used** (`is_used`) once any time entry falls inside its scope and period (a rule that *could*
  have priced an entry counts: its empty dimensions match anything). A used rule cannot be deleted
  (409 `RATE_IN_USE`) and cannot start on a different day (409 `RATE_IN_USE`), but it can be **end-dated**
  (`effective_to`) and its amount can change; entries already recorded keep their rates.
* A person who has left cannot get new rules (409 `MEMBER_REMOVED`).

## GET /rates

Permission `rate.read`. Query:

| Param | Meaning |
|---|---|
| `kind` | `charge` or `cost`. |
| `level` | Repeatable: `client_service_staff, client_service, client_staff, client, service_grade, staff, grade`. |
| `client_id`, `service_id`, `user_id`, `grade_id` | Rules that name that client, service, staff member or grade. |
| `on_date` | Only rules in force that day. |
| `status` | `current`, `future` or `ended`, as of today (practice zone). |
| `q` | Substring of client, service, staff and grade names and codes (and staff codes). |
| `sort` | `effective_from` (default), `rate`, `created_at`; `order` `asc`/`desc` (default `desc`). |
| `limit`, `cursor` | |

Response `RateRuleRead[]` + `meta`.

## POST /rates

Permission `rate.manage`. Body:

| Field | Type | Notes |
|---|---|---|
| `kind` | `charge` / `cost` | Required. |
| `client_id`, `service_id`, `user_id`, `grade_id` | uuid | Which are sent decides the level. Each must exist in this practice (422 on that field). |
| `rate` | int ≥ 0 | Pence per hour. Required. |
| `effective_from` | date | Required. |
| `effective_to` | date or null | Default null (open-ended). |
| `close_previous` | bool, default false | See Period rules. |

Response 201 `RateRuleRead`. 409 `RATE_OVERLAP`, `MEMBER_REMOVED`; 422 `invalid_scope`, `invalid_kind`. Without
`rate.read` (a custom role with `rate.manage` only) a cost rule is returned with `rate: null`.

## GET /rates/{rule_id}, PATCH, DELETE

`GET` (`rate.read`) returns `RateRuleRead`. `PATCH` (`rate.manage`) body `rate`, `effective_from`,
`effective_to` (null = open-ended again); kind and scope never change; 409 `RATE_OVERLAP` when the new period
meets another rule of the scope; 409 `RATE_IN_USE` when a used rule's start would move. `DELETE`
(`rate.manage`) answers 200 `data: null`, or 409 `RATE_IN_USE`. All changes are kept in the history.

## GET /rates/resolve

Permission `rate.read`. "What rate would this person get?" Nothing is written.

| Param | Meaning |
|---|---|
| `user_id` | Required: the staff member. |
| `date` | Default today (practice zone). |
| `client_id` | The client. |
| `job_id` | The job; supplies the client and service. A job of another client than `client_id` is 422. |
| `service_id` | The service; overrides the job's. |

Unknown ids answer 404. Response `ResolveRead`: the applied `charge_rate` / `charge_source` /
`charge_rule_id` and `cost_rate` / `cost_source` / `cost_rule_id`, plus **both ladders** for the UI to explain
the choice. Each `LadderStep` has `applicable` (false when the query lacks what the level needs, with a `reason`),
`exists`, `rate`, `origin` (`rule`, `job`, `grade_default`, `practice_default`, `none`), the `rule`
(`{id, effective_from, effective_to}`) and `selected` (exactly one step per ladder). The charge ladder is levels
1 to 7 (the client's rate for everyone is `source: "client"`, level 3) followed by the practice default
(`level: null`, `source: "default"`); the cost ladder is `staff`, `grade`, `none`.

```json
{"on_date": "2026-09-15", "staff": {"id": "…", "code": "S002", "full_name": "Sam Staff"},
 "client": {"id": "…", "code": "C0001", "name": "Acme Ltd"},
 "service": {"id": "…", "code": "ACCS", "name": "Year-end accounts"}, "job_id": "…",
 "grade": {"id": "…", "code": "SNR", "name": "Senior"},
 "charge_rate": 20000, "charge_source": "client_service_staff", "charge_rule_id": "…",
 "cost_rate": 3000, "cost_source": "staff", "cost_rule_id": "…",
 "charge_ladder": [
   {"level": 1, "source": "client_service_staff", "label": "Client + service + staff", "applicable": true,
    "reason": null, "exists": true, "rate": 20000, "origin": "rule",
    "rule": {"id": "…", "effective_from": "2026-09-01", "effective_to": null}, "selected": true},
   {"level": 2, "source": "client_service", "label": "Client + service", "applicable": true, "reason": null,
    "exists": false, "rate": null, "origin": null, "rule": null, "selected": false},
   "… levels 3 (client), 4 (job), 5 (service_grade), 6 (staff), 7 (grade) …",
   {"level": null, "source": "default", "label": "Practice default rate", "applicable": true, "reason": null,
    "exists": true, "rate": 0, "origin": "practice_default", "rule": null, "selected": false}],
 "cost_ladder": [
   {"level": null, "source": "staff", "label": "Staff cost rate", "applicable": true, "reason": null,
    "exists": true, "rate": 3000, "origin": "rule",
    "rule": {"id": "…", "effective_from": "2026-09-01", "effective_to": null}, "selected": true},
   {"level": null, "source": "grade", "…": "…"}, {"level": null, "source": "none", "…": "…"}]}
```

## GET /rates/history

Permission `rate.read`. Every rate change, newest first, cursor-paginated. Filters `user_id`, `grade_id`,
`client_id`, `service_id`, `kind`, `action` (`created`, `updated`, `end_dated`, `deleted`), `date_from`,
`date_to` (inclusive, practice zone). `RateChangeRead[]`: `old` and `new` are `{rate, effective_from,
effective_to}` (null on create / on delete); a change of a **grade default** has `rule_id: null`, `source`
`grade_default` and `old`/`new` `{rate, default: true}`. `source` is `rates` (this API), `staff_rates` or
`client_rates` (the V1 endpoints) or `grade_default`. References carry names, not codes. Rates migrated from V1
have no history rows.

---

# Re-rating unbilled time

Changing a rate never touches recorded time. Re-rating re-takes the charge-out and cost rate of **unbilled**
entries from the ladder as it is now, in two steps:

1. `POST /rates/rerate/preview`: nothing changes. Returns every affected entry with old and new rate and
   value, the totals, what is left alone and a `preview_token`.
2. `POST /rates/rerate`: send the **same scope** and the token. The result is computed again under a lock and
   must equal the preview.

Left alone, whatever the scope: billed, in-draft and written-off time, and entries in a locked period
(`app/services/period_locks.py`; no lock exists yet, so `period_locked` is 0). Timesheet status does not
matter. Applying writes one run (`rate_reruns`), one item per changed or skipped entry with before and after,
and one audit row `rate.rerun`.

## Scope

`RerateScope`: `client_ids[]`, `job_ids[]`, `user_ids[]` (each ≤ 200), `date_from`, `date_to`. Every filter
given narrows the result; at least clients, jobs or a date range (**both** dates) is required (422 on
`scope`), and `date_to` must not be before `date_from`. More than 20,000 unbilled entries in scope is 422
`scope_too_large` (field `scope`).

## POST /rates/rerate/preview

Permission `rate.manage`. Body `{scope, item_limit (0–5000, default 500), include_unchanged (default false)}`.
Response `ReratePreview` (cost values null without `rate.read`):

```json
{"preview_token": "eyJ…", "expires_at": "2026-10-17T21:53:54.352962Z",
 "scope": {"client_ids": ["…"], "job_ids": [], "user_ids": [], "date_from": "2026-09-01", "date_to": "2026-09-30"},
 "totals": {"entries_examined": 1, "entries_to_change": 1, "entries_unchanged": 0, "entries_skipped": 1,
            "charge_value_before": 6000, "charge_value_after": 14500, "charge_value_delta": 8500,
            "cost_value_before": 1000, "cost_value_after": 1000, "cost_value_delta": 0},
 "skipped": {"billed": 1, "in_draft": 0, "written_off": 0, "period_locked": 0},
 "items": [{"entry_id": "…", "entry_date": "2026-09-07", "minutes": 60,
            "staff": {"id": "…", "code": "S002", "full_name": "Sam Staff"},
            "client": {"id": "…", "code": "C0001", "name": "Acme Ltd"},
            "job": {"id": "…", "code": "J00002", "name": "Year-end accounts"},
            "outcome": "change", "skip_reason": null,
            "old": {"charge_rate": 6000, "charge_value": 6000, "cost_rate": 1000, "cost_value": 1000, "source": null},
            "new": {"charge_rate": 14500, "charge_value": 14500, "cost_rate": 1000, "cost_value": 1000, "source": "client"}}],
 "items_truncated": false}
```

`entries_examined` counts the unbilled entries that can be re-priced (so it excludes the skipped ones);
`entries_skipped` = the sum of `skipped`. `items` is sorted by date, lists entries that change or are skipped
for being in a locked period (`outcome` `change` or `skipped`; `include_unchanged` adds `unchanged`), at most
`item_limit` of them; the totals always cover everything. The token is valid 30 minutes, tied to the caller,
the practice and the exact result. Values are `round_half_up(minutes × rate / 60)` per entry.

## POST /rates/rerate

Permission `rate.manage`. Body `{scope, preview_token, note (≤ 500, optional)}`. Response 201 `RerunRead`:

```json
{"id": "…", "created_at": "…", "source": "rerate",
 "scope": {"client_ids": ["…"], "job_ids": [], "user_ids": [], "date_from": "2026-09-01", "date_to": "2026-09-30"},
 "entries_examined": 1, "entries_changed": 1, "entries_skipped": 1,
 "charge_value_before": 6000, "charge_value_after": 14500, "charge_value_delta": 8500,
 "cost_value_before": 1000, "cost_value_after": 1000, "cost_value_delta": 0,
 "requested_by": "…", "requested_by_name": "Alex Owner", "note": "September correction"}
```

Errors (all 409): `PREVIEW_REQUIRED` (token missing, invalid, expired or issued to someone else),
`PREVIEW_MISMATCH` (the scope is not the previewed one), `PREVIEW_STALE` (rates or time changed since the
preview; `details {entries_to_change, charge_value_after}` describe the new result; preview again). Applying
the same preview twice answers `PREVIEW_STALE` the second time (nothing is left to change).

## GET /rates/reruns, GET /rates/reruns/{run_id}, GET /rates/reruns/{run_id}/items

Permission `rate.read`. Runs are listed newest first (cursor-paginated), including V1 `revalue` calls
(`source` `revalue`); `RerunRead` as above (cost fields null without `rate.read`). Items: filter `outcome`
(`changed` or `skipped`), ordered by entry date, cursor-paginated; `RerunItemRead` = `{id, entry_id,
entry_date, minutes, staff, client, job, outcome, skip_reason, old, new}` (`old`/`new` as in the preview).
Unknown run ids answer 404.

## POST /rates/revalue (V1)

Permission `rate.manage`. Body `{date_from, date_to, user_id?, client_id?}` (range at most five years).
Re-prices every unbilled entry in the range straight away, **without a preview**; recorded as a run with
`source: "revalue"` and the audit action `time_entries.revalued`. Response
`{entries_examined, entries_changed, charge_value_before, charge_value_after, value_delta, cost_value_delta}`
(`cost_value_delta` null without `rate.read`). New UIs should use preview and apply.

---

# V1 rate endpoints (kept)

They still work and are views of the dated rules, so both models show the same data.

| Endpoint | Behaviour |
|---|---|
| `GET /members/{user_id}/rates` (`rate.read`) | `StaffRateRead[]`, newest first. Each row is a `staff` **charge** rule plus the `staff` **cost** rule that starts the same day. |
| `POST /members/{user_id}/rates` (`rate.manage`) | `{effective_from, charge_rate, cost_rate = 0}` → 201 `StaffRateRead`. Keeps V1's timeline: the rate in force the day before is ended and the new one runs until the person's next rate starts. 409 `STAFF_RATE_EXISTS` when a rate already starts that day; 409 `MEMBER_REMOVED`. |
| `DELETE /staff-rates/{rate_id}` (`rate.manage`) | Removes the row (charge and cost rule); the previous row takes over the days. **409 `RATE_IN_USE` once time falls under it** (V1 allowed this). |
| `GET /staff-rates/current?on_date=` (`rate.read`) | `CurrentStaffRate[]`: every **active** staff member by name with the staff rule in force on `on_date` (default today); rate fields null when none. Bounded. |
| `GET /clients/{client_id}/rates` (`rate.read`) | `ClientRateRead[]`: the everyone rule first, then per person. |
| `POST /clients/{client_id}/rates` (`rate.manage`) | `{user_id: uuid or null, hourly_rate, effective_from?, effective_to?}`. A `client_staff` rule (person) or `client` rule (everyone) from `effective_from` (default 2000-01-01, so it applies to every day). 409 `CLIENT_RATE_EXISTS` when that scope already has a rule in the period. |
| `PATCH /clients/{client_id}/rates/{rate_id}` (`rate.manage`) | `{hourly_rate}`. |
| `DELETE /clients/{client_id}/rates/{rate_id}` (`rate.manage`) | 409 `RATE_IN_USE` once used. |

`StaffRateRead`: `{id, user_id, effective_from, effective_to (the day before the next rate; null for the
latest), charge_rate, cost_rate (null without rate.read), created_by, created_at}`. `CurrentStaffRate`:
`{user_id, full_name, email (work email without a login), initials, job_title, on_date, rate_id,
effective_from, charge_rate, cost_rate}`. `ClientRateRead`: `{id, client_id, user_id, user_name,
hourly_rate, effective_from, effective_to, created_at, updated_at}`.

---

# Services and activity types

Both lists are bounded and not paginated; any member reads them. Writes need `settings.manage`.

## Services

`GET /services?include_inactive=&q=`, `GET /services/{service_id}`: `ServiceRead`.
`POST /services` (201), `PATCH /services/{service_id}`, `POST …/activate`, `POST …/deactivate`,
`DELETE /services/{service_id}`.

Create body: `code` (1–12, upper-cased, `[A-Z0-9_-]`), `name` (1–120), `description` (≤ 1000), `billing_basis`
(`hourly` default or `fixed`), `default_fee` (pence), `default_billable` (default true), `vat_rate_id` (a live
VAT rate of the practice, 422 otherwise), `invoice_description` (≤ 500; the wording suggested for invoice
lines), `sort_order` (0–10000), `is_active` (default true). `PATCH` takes any of them; null clears
`description`, `default_fee`, `vat_rate_id` and `invoice_description` (a blank `description` or
`invoice_description` clears it too); `code`, `name`, `billing_basis`, `default_billable`, `sort_order`,
`is_active` cannot be null.

* Names and codes are unique in the practice, ignoring case, **inactive services included**:
  409 `SERVICE_CODE_TAKEN`, `SERVICE_NAME_TAKEN`.
* `default_billable=false`: a job created for the service without an explicit `billing_basis` starts as
  `non_billable` (an explicit basis wins; true keeps following the service's basis). Time on a non-billable job
  defaults to non-billable.
* `status` is `active` or `inactive` (same as `is_active`).
* **Used services are deactivated, never deleted**: `DELETE` removes an unused service
  (`{"outcome": "deleted", "id"}`) and deactivates one that a job, a client engagement, an invoice line or a
  rate rule refers to (`{"outcome": "deactivated", "id"}`). `is_used` / `can_delete` say which in advance.
  Deactivated services are hidden from pickers but existing jobs and engagements keep them.

## Activity types (activity codes)

`GET /activity-codes?include_inactive=&q=` (chargeable first, then by code; `q` searches code and name),
`GET /activity-codes/{activity_id}`, `POST /activity-codes` (201), `PATCH`, `POST …/activate`,
`POST …/deactivate`, `DELETE`: `ActivityCodeRead`.

Create body: `code` (1–12, upper-cased), `name` (1–120), `is_chargeable` (default true: the **default billable
status** of time recorded against it; false = internal time), `vat_rate_id`, `invoice_description` (≤ 500),
`is_active`. Names and codes are unique, ignoring case: 409 `ACTIVITY_CODE_TAKEN`, `ACTIVITY_NAME_TAKEN`.
`DELETE` deactivates (`outcome` `deactivated`) a code that time entries or timers use or that a service lists;
otherwise it deletes. A VAT rate used by an activity code is archived, not deleted, when its `DELETE` is called.

## Service ↔ activity links

`GET /services/{service_id}/activity-codes` → `ServiceActivityCodesRead`
`{service_id, restricted, activity_codes: ActivityCodeRead[]}`. `PUT` (`settings.manage`) body
`{"activity_code_ids": [uuid, …]}` (≤ 200; duplicates collapse) **replaces** the list and answers the new one.
An empty list removes the limit (`restricted: false`: every code is allowed). 422 for an unknown id, 404 for a
service of another practice. Deactivated codes may stay linked. Saving the same set changes and logs nothing;
a change writes the audit row `service.activity_codes_changed` (`added`, `removed` codes). `ServiceRead` also
carries `activity_code_ids`.

When a service has a list, time on **its jobs** may only use those activity codes: `POST /time-entries`,
`POST /time-entries/bulk` (error field `entries.N.activity_code_id`), `PATCH /time-entries/{id}` (changing the
activity or moving the entry to such a job) and `POST /timer/start` answer 422 with `details[0]`
`{field: "activity_code_id", code: "activity_not_allowed"}`. No activity code is always fine, and jobs without
a service are unrestricted. Time already recorded keeps its code and stays editable when the list changes
later.

## Number sequences

`GET /sequences` returns seven entries (`client`, `job`, `invoice`, `credit_note`, `staff`, `ticket` from the Application
Testing module, `docs/modules/tickets.md`, and `estimate`, prefix `EST`, padding 5); `PATCH
/sequences/staff` works like the others (prefix `S`, padding 3 by default). Staff codes come from it when no
`code` is given: gaps are fine and a generated code skips codes in use. 409 `SEQUENCE_INVALID` when the format
would produce an invalid code.

---

# Shapes

`RefRead` `{id, code (null in history), name}`; `StaffRef` `{id, code, full_name}`; `RoleSummary`
`{id, name, system_key}`.

**StaffRead**

```
id, code, full_name,
email (work email), has_login, login_email (null without a login),
login_status: "none" | "invited" | "active", invited_at,
status: "active" | "inactive" | "left", is_selectable,
grade: RefRead | null, approver: {id, full_name} | null, role: RoleSummary,
job_title, initials, working_minutes_per_day, working_days (ISO, 1 = Monday), weekly_capacity_minutes,
chargeable_target_bp, start_date, leaving_date,
charge_rate, charge_rate_source, cost_rate, cost_rate_source   // the standard rates that apply today (no
                                                              // client, job or service); null without rate.read
joined_at, updated_at
```

`charge_rate_source` is one of the charge sources (`staff`, `grade`, `default`, …); `cost_rate_source` is
`staff`, `grade` or `none`. **StaffDetail** = StaffRead + `usage: [{kind, label, count}]` + `can_delete`.

**GradeRead**: `id, code, name, description, default_cost_rate, default_charge_rate,
default_chargeable_target_bp, is_active, sort_order, staff_count, can_delete, created_at, updated_at`.

**RateRuleRead**

```
id, kind: "charge" | "cost", level, level_label, precedence (1-7; the job rate is 4),
client: RefRead | null, service: RefRead | null, staff: StaffRef | null, grade: RefRead | null,
rate (pence per hour; null for a cost rule without rate.read),
effective_from, effective_to, status: "current" | "future" | "ended",
is_used, can_delete (= not is_used), created_by, created_at, updated_at
```

**ServiceRead**: `id, code, name, description, billing_basis, default_fee, default_billable, vat_rate_id,
invoice_description, is_active, status, sort_order, activity_code_ids, is_used, can_delete, created_at,
updated_at`. **ActivityCodeRead**: `id, code, name, is_chargeable, vat_rate_id, invoice_description, is_active,
status, is_used, can_delete, created_at, updated_at`. **DeleteOutcome**: `{outcome: deleted|deactivated, id}`.

Time entries (Time module) gained `rate_source` (see Levels and precedence; null for entries saved before this
module until they are edited or re-rated).

# Error codes

All errors use the envelope above. 401 `AUTH_REQUIRED`, 403 `PERMISSION_DENIED`, 404 `RESOURCE_NOT_FOUND`
(also for another practice's ids), 422 `VALIDATION_ERROR` and 429 `RATE_LIMITED` apply everywhere.

| Code | Status | When |
|---|---|---|
| `STAFF_CODE_TAKEN` | 409 | Another staff member (any status) has the code. `details[0].field` = `code`. |
| `STAFF_EMAIL_TAKEN` | 409 | Another **active** staff member has the work email or signs in with it. Field `email`. |
| `INITIALS_TAKEN` | 409 | Another non-removed member has the initials. |
| `EMAIL_TAKEN` | 409 | The sign-in address already belongs to an account (invite, link-login). |
| `ACCOUNT_SHARED` | 409 | The person's account also belongs to another practice (rename, unlink). |
| `STAFF_LEFT` | 409 | Marking someone who has left as `inactive`. |
| `STAFF_IN_USE` | 409 | Deleting staff with records. `details.usage`. |
| `STAFF_NOT_ACTIVE` | 409 | Giving a login to someone who is not active. |
| `LOGIN_ALREADY_LINKED` | 409 | `link-login` for someone who has a login. |
| `LOGIN_ALREADY_ACTIVE` | 409 | `invite` for someone who already chose a password. |
| `LINK_CONFLICT` | 409 | `existing_user` merge where both have an uncombinable record. `details.table`. |
| `NO_LOGIN` | 409 | `unlink-login` for someone without a login. |
| `CONFLICT` | 409 | `details {"code": "last_owner"}`: the practice must keep one active owner who can sign in. |
| `SEQUENCE_INVALID` | 409 | The staff number format would produce an invalid code. |
| `GRADE_CODE_TAKEN`, `GRADE_NAME_TAKEN` | 409 | Duplicate grade code or name (case-insensitive). |
| `SERVICE_CODE_TAKEN`, `SERVICE_NAME_TAKEN` | 409 | Duplicate service code or name. |
| `ACTIVITY_CODE_TAKEN`, `ACTIVITY_NAME_TAKEN` | 409 | Duplicate activity code or name. |
| `RATE_OVERLAP` | 409 | Another rule of the kind and scope covers part of the period. `details.conflicts`. |
| `RATE_IN_USE` | 409 | Deleting a used rule, or moving its start. |
| `MEMBER_REMOVED` | 409 | A rate for someone who has left. |
| `STAFF_RATE_EXISTS`, `CLIENT_RATE_EXISTS` | 409 | V1 duplicates (same start day; same client scope). |
| `PREVIEW_REQUIRED`, `PREVIEW_MISMATCH`, `PREVIEW_STALE` | 409 | Re-rating without, with another or with an out-of-date preview. |
| `VALIDATION_ERROR` | 422 | Field validation. `details[].code` includes `invalid_scope`, `invalid_kind` (rates), `scope_too_large`, `after_leaving_date` (time entry after the person left), `activity_not_allowed` (time entry or timer), `required` (invite without an email), `duplicate`. |

# Permissions

No code was added. Staff: read `member.read`; create, invite, link `member.invite`; update, reactivate
`member.update`; deactivate, delete, unlink `member.remove`. Grades and rates: read `rate.read` (grades:
`member.read`); write `rate.manage`; re-rating needs `rate.manage` (preview and apply). Services, activity
types and their links: any member reads, `settings.manage` writes.

| Role | Staff | Grades | Rates | Services, activities |
|---|---|---|---|---|
| Owner, Admin, Partner | read and manage | read and manage | read and manage | manage |
| Manager | read, sees rates | read, sees cost default | read only (`rate.read`) | read |
| Staff, Billing, Viewer | read; rates and costs hidden (null) | read; cost default hidden | no access (403) | read |

Custom roles combine the same codes: for example `rate.manage` without `rate.read` can add rates but sees
cost rates as `null`.

# Effects on other modules

* **Time**: entries are priced by the ladder and store `rate_source`; `activity_not_allowed` and
  `after_leaving_date` (422); `GET /staff?selectable=true` lists who time can be recorded for. Non-login
  staff are priced and can be recorded for by someone with `time.manage_all`; they cannot submit timesheets.
  Editing an unbilled entry still re-takes its rate (V1 rule, unchanged), so a rate change reaches an old
  entry only when the entry is edited or re-rated.
* **Jobs**: a service with `default_billable=false` gives new jobs `billing_basis = non_billable`. A job's
  `hourly_rate` is level 4 of the ladder. Jobs and clients may name staff without a login.
* **V1 `/members`, `/people`**: unchanged, still list everyone (staff without a login too, `email` = work
  email) with the V1 statuses `active`, `suspended`, `removed`, and now `inactive`, `left`.

# Audit actions

`staff.created|updated|deactivated|left|reactivated|deleted|invited|login_linked|login_unlinked`,
`grade.created|updated|deactivated|deleted`, `rate.created|updated|deleted` (rate rules; V1 endpoints keep
`staff_rate.created|deleted`, `client_rate.created|updated|deleted`), `rate.rerun`, `time_entries.revalued`,
`service.activity_codes_changed`, plus the existing settings actions. Each has a `meta` naming the record and,
for updates, `changes: {field: [old, new]}`.

# Rule decisions and limitations

* **Job rate is undated** (level 4, `jobs.hourly_rate`): a job rule has no period.
* **Grade defaults are the baseline** of the grade level: a grade rule *in force* overrides them; outside its
  period the default applies again. The client's "everyone" rule sits inside level 3, after the client's
  person rule.
* **Used** is scope-based, not entry-based: any entry that falls in the scope and period (even one priced by
  another rule) makes the rule used.
* The **grade of the person at the time** of resolution is their current grade (grade changes are not
  historised).
* Re-rating covers charge **and** cost; rates of entries in a locked period are never changed (the lock hook
  is a no-op until Period Lock exists).
* `existing_user` link merges only into an **active login member of the same practice**; merging across
  practices is not offered.
* V1 `PATCH /members/{id}` still edits `weekly_capacity_minutes` directly and does not touch working hours;
  `PATCH /staff` keeps capacity and hours in step.
* Staff without a login cannot submit timesheets or sign in; their time is entered for them (Time module
  follow-up for a manager-submits flow).
* The temporary password is returned only by `link-login` and is never stored in clear text or logged.

# For other modules

* `app.services.charge_rates.resolve_many(session, organization, [RateQuery(user_id, client_id, job_id,
  entry_date)])` → `ResolvedRate(charge_rate, cost_rate, source, cost_source, charge_rule_id,
  cost_rule_id)` for many entries with four queries; `resolve(...)` for one; `explain(...)` for both ladders.
* `app.services.rates.rate_in_force(session, organization_id, user_id, on_date)` and `current_rates(...)`
  keep the V1 contract (the person's own staff rates).
* `app.services.period_locks.locked_dates(session, organization_id, days)` → `{day: reason}`: implement it
  when period locks are added; re-rating already calls it.
* `app.repositories.staff_references.usage(session, organization_id, user_id)` lists what refers to a person;
  add a table there when a module introduces a new reference to `users` (the staff delete and the
  `existing_user` merge depend on it).
