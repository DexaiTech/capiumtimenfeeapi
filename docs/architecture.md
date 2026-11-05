# Backend architecture

How a request travels through the API, and where each cross-cutting concern lives. Business
rules are in [domain.md](domain.md); endpoint contracts are in [modules/](modules/); the
endpoint list is in [api-reference.md](api-reference.md); the schema is in
[database.md](database.md).

## Processes

| Process | Entry point | What it does |
|---|---|---|
| API | `uvicorn app.main:app` (`app/main.py`, `create_app()`) | Serves `/health/*` and `/api/v1/*`. Stateless apart from the in-process caches listed under [Per-process state](#per-process-state). |
| Worker | `python -m app.workers.main` (`app/workers/main.py`) | Claims rows of `background_jobs`, runs the handlers in `app/workers/handlers.py`, schedules periodic jobs. |
| Migrations | `alembic upgrade head` (`alembic/env.py`) | The only way the schema changes. Uses `DB_DIRECT_URL`, falling back to `DB_URL`. |
| Maintenance | `python -m app.maintenance.bootstrap`, `python -m app.maintenance.reset_data` | First practice and Owner of a closed deployment; emptying a database. |

## Layers

```text
app/api/v1/<module>.py      router: HTTP only (paths, params, status codes, OpenAPI text)
        │  Depends(...)     app/dependencies/: session, caller, practice, permission
        ▼
app/services/<module>.py    rules, one transaction (unit_of_work), audit rows, job enqueue
        │
        ▼
app/repositories/<module>.py  SQLAlchemy queries, every one filtered by organization_id
        │
        ▼
app/db/models/<area>.py     declarative tables (Base in app/db/base.py)  →  PostgreSQL
```

| Layer | Folder | Rules evidenced in code |
|---|---|---|
| Router | `app/api/v1/` (63 modules, mounted in `app/api/router.py`; `integration_routes.py` only aggregates five integration routers) | Module docstrings say "HTTP only". A router builds its service with a `get_<x>_service(session)` dependency and returns `ok(...)` / `ok_page(...)`. |
| Schemas | `app/schemas/` | Request bodies subclass `RequestModel` (`app/schemas/common.py`: `extra="forbid"`, strings trimmed). Read models often have a `from_model(...)` classmethod (e.g. `GradeRead.from_model`). |
| Services | `app/services/` | Take an `AsyncSession`, an `OrgContext` and a `RequestContext`; raise `AppException` subclasses, never HTTP errors (`app/core/exceptions.py`). |
| Repositories | `app/repositories/` | One class per aggregate, methods take `organization_id` first; `lock=True` adds `FOR UPDATE` (e.g. `GradeRepository.get`). |
| Models | `app/db/models/` | Mixins in `app/db/base.py`: `IdMixin` (UUIDv7, `app/utils/ids.py`), `TimestampMixin`, `SoftDeleteMixin`, `OrgScopedMixin`. |
| Utilities | `app/utils/` | Pure helpers: money (`money.py`), cursors (`pagination.py`), practice dates (`practice_dates.py`), rules shared by several services (`job_rules.py`, `wip_rules.py`, `lock_periods.py`). |

Route order matters: `app/api/router.py` includes routers with static paths (`/jobs/budgets`,
`/time-entries/day`, `/wip/*`, `/reports/wip`, `/staff/export`) before the routers whose paths
take an id, and says so in comments. Keep that order when adding routes.

## Request lifecycle

Middleware is registered in `create_app()` (`app/main.py`); the last one added runs first.

| Order | Component | File | Effect |
|---|---|---|---|
| 1 | `RequestContextMiddleware` | `app/middleware/request_context.py` | Accepts or generates `X-Request-ID`, binds it to the structlog context and `request_id_var`, writes one access-log line with the route template (not the raw path). |
| 2 | `CORSMiddleware` | Starlette | Origins from `CORS_ORIGINS` (default `FRONTEND_URL`), credentials allowed. |
| 3 | `JsonGZipMiddleware` | `app/middleware/compression.py` | Gzips JSON answers of 1 KB or more. |
| 4 | `SecurityHeadersMiddleware` | `app/middleware/security_headers.py` | `nosniff`, `X-Frame-Options: DENY`, `Cache-Control: no-store`, a deny-all CSP (not on `/docs`), HSTS when cookies are secure. |
| 5 | `ApiRateLimitMiddleware` | `app/middleware/hardening.py` | Global ceiling per user (valid bearer token) or per address; see [Rate limits](#rate-limits-and-request-limits). |
| 6 | `RequestSizeLimitMiddleware` | `app/middleware/hardening.py` | 413 `REQUEST_TOO_LARGE` above `MAX_REQUEST_BODY_KB` on writes, except the upload paths in `_UPLOADS`. |

Then FastAPI resolves the route's dependencies (`app/dependencies/`):

1. `get_session` (`app/db/session.py`): one `AsyncSession` per request.
2. `get_principal` (`app/dependencies/auth.py`): bearer token → `decode_access_token`
   (`app/core/tokens.py`) → user and session in one query (`SessionRepository.get_caller`).
   Refuses inactive users, tokens issued before the last password change and ended sessions.
   Outside `/auth/*`, `GET /users/me` and `POST /users/me/password` it refuses a temporary
   password (403 `PASSWORD_CHANGE_REQUIRED`).
3. `get_verified_principal`: 403 `AUTH_EMAIL_NOT_VERIFIED` until the email is verified.
4. `get_member_context` (`app/dependencies/organization.py`): the practice comes from the URL
   path, never from the token or body; an unknown practice and "not a member" both answer 404.
   Builds the `OrgContext` (`app/services/policy.py`) with the effective permissions (role plus
   per-member grants minus denials, `app/core/permissions.py: effective_permissions`).
5. `get_org_context`: 403 `SUBSCRIPTION_UNAVAILABLE` unless the practice's subscription is
   active (`app/services/subscription.py`, cached per process). Only `GET /subscription` and the
   subscription mock switch skip this step.
6. `require("<code>", ...)`: 403 `PERMISSION_DENIED` unless the caller holds every listed code.
   It runs before the target is looked up.

The router calls the service. A write runs inside `unit_of_work(session)` (`app/db/session.py`):
commit on success, rollback on any exception; nested blocks join the outer one. Audit rows and
background jobs are added to the same session, so they commit or roll back with the change.

## Response envelope

Defined in `app/schemas/common.py`; documented for clients in the OpenAPI description
(`app/main.py: DESCRIPTION`).

```json
{"success": true, "data": {}, "message": "Grade created", "meta": null}
{"success": true, "data": [], "meta": {"next_cursor": "...", "has_more": true}}
{"success": false, "error": {"code": "PERMISSION_DENIED", "message": "...", "details": null, "request_id": "..."}}
```

* Lists are cursor-paginated (`limit` 1..`MAX_LIMIT`, `cursor` from `meta.next_cursor`;
  `get_page_params` in `app/dependencies/organization.py`, cursor encoding in
  `app/utils/pagination.py`) unless the route says it is a bounded list.
* `error_responses(*statuses)` declares the error envelope in OpenAPI for 401, 422, 429 plus the
  statuses given.

## Errors

Services raise subclasses of `AppException` (`app/core/exceptions.py`); `app/core/handlers.py`
turns every exception into the error envelope. Module-specific codes (for example
`GRADE_CODE_TAKEN`, `TIME_LOCKED`) are passed as `code=` and listed in each module doc's
"Error codes" section.

| Status | Code | Raised by |
|---|---|---|
| 400 | `FILE_INVALID` | `FileUploadError` |
| 401 | `AUTH_REQUIRED`, `AUTH_TOKEN_EXPIRED`, `AUTH_TOKEN_INVALID` | `AuthenticationError`, `app/core/tokens.py`, `app/dependencies/auth.py` |
| 403 | `PERMISSION_DENIED`, `PASSWORD_CHANGE_REQUIRED`, `AUTH_EMAIL_NOT_VERIFIED`, `SUBSCRIPTION_UNAVAILABLE`, `REGISTRATION_DISABLED` | `AuthorizationError` and subclasses; `forbid_when_closed` (`app/dependencies/common.py`) |
| 404 | `RESOURCE_NOT_FOUND` | `NotFoundError`; also another practice's ids |
| 405 | `METHOD_NOT_ALLOWED` | Starlette |
| 409 | `CONFLICT` (plus module codes such as `PERIOD_LOCKED`, `INVALID_STATUS_TRANSITION`) | `ConflictError`; unique violations; `RATE_OVERLAP` for the `ex_rate_rules_scope_period` exclusion constraint |
| 413 | `REQUEST_TOO_LARGE`; `FILE_TOO_LARGE` (any raw HTTP 413); module codes such as `ATTACHMENT_TOO_LARGE` | `RequestSizeLimitMiddleware`; `_STATUS_CODES` in `app/core/handlers.py`; upload services |
| 415 | `FILE_TYPE_NOT_ALLOWED` (any raw HTTP 415) | `_STATUS_CODES` in `app/core/handlers.py` |
| 422 | `VALIDATION_ERROR` | Pydantic (`details: [{field, code, message}]`), `ValidationError.for_field`, foreign-key and check violations |
| 429 | `RATE_LIMITED` (`Retry-After`) | `RateLimitError` |
| 500 | `INTERNAL_ERROR` | anything unhandled (stack only in the log) |
| 502 | `EXTERNAL_SERVICE_ERROR` | `ExternalServiceError` |
| 503 | `SERVICE_UNAVAILABLE` (`Retry-After: 5`) | database unreachable, pool timeout, serialization failure or deadlock; `/health/ready` |

Database errors map through the constraint name: `UNIQUE_MESSAGES` in `app/core/handlers.py`
names the field and message of each known unique constraint; constraint names never reach the
client.

## Authentication

| Piece | Where | Detail |
|---|---|---|
| Passwords | `app/core/security.py` | Argon2 via `pwdlib`, hashing off the event loop, a dummy hash for unknown emails (timing). |
| Access token | `app/core/tokens.py` | HS256 JWT with `sub` (user), `sid` (session), `iat`, `exp`, `typ=access`; 15 minutes by default (`ACCESS_TOKEN_EXPIRE_MINUTES`). Carries identity only, never permissions. |
| Refresh token | `app/api/v1/auth.py`, `app/services/auth.py` | Opaque token in an httpOnly cookie `ctf_refresh` (`__Secure-ctf_refresh` when secure) scoped to `/api/v1/auth`, `SameSite=Lax`, 30 days. Rotated on every refresh; reuse is detected and revokes the session (`auth.refresh_reuse_detected`). Stored hashed (`hash_token`) in `refresh_tokens`. |
| CSRF | `require_browser_origin` (`app/dependencies/auth.py`) | The cookie endpoints need `X-Requested-With: XMLHttpRequest` and an `Origin` in `CORS_ORIGINS`. |
| Sessions | `user_sessions` table | Logout, logout-all and `DELETE /users/me/sessions/{id}` end them; the next request with an old access token is refused. |
| Closed deployment | `CLOSED_DEPLOYMENT` (default true) | Registration and practice creation answer 403; staff accounts come from `POST /people`. |

## Multi-tenancy

* An *organization* is a practice. Every business route is under
  `/api/v1/organizations/{organization_id}/...` (58 of the 63 router modules; the others are `auth.py`, `users.py`, `organizations.py`,
  `system.py` and the aggregator `integration_routes.py`).
* `OrgScopedMixin` (`app/db/base.py`) adds `organization_id` with `ON DELETE CASCADE` to every
  practice table. Tables also carry `UNIQUE (organization_id, id)` so children reference them
  with composite foreign keys (`(organization_id, client_id) → clients`, see
  [database.md](database.md#tenancy)): a row cannot point into another practice.
* Repositories filter every query by `organization_id`. Another practice's id answers 404.
* Practices are soft-deleted (`organizations.deleted_at`).

## Permissions

| Piece | Where |
|---|---|
| Catalogue (65 codes, 15 groups) and the seven system roles | `app/core/permissions.py` (`CATALOGUE`, `SYSTEM_ROLES`); documented in [modules/permissions.md](modules/permissions.md), which `tests/test_permissions_doc.py` checks. |
| Route gate | `require(...)` in `app/dependencies/organization.py`, usually wrapped in an alias such as `GradeManager = Annotated[OrgContext, Depends(require("rate.manage"))]`. |
| Decisions inside services | `app/services/policy.py`: `has_permission`, `ensure`, `ensure_any`, `is_owner`, `ensure_can_grant`, `ensure_can_assign_role`. Used for own-versus-others rules (time, expenses, timesheets) and field masking (cost rates need `rate.read`). |
| Per-member overrides | `member_permission_overrides` (grant/deny), applied by `effective_permissions`. |
| Seeding | Migrations call `sync_permissions()` (`app/db/migration_helpers.py`) to upsert the catalogue. |

Routes check permission codes, never role names (stated in [domain.md](domain.md#roles)).

## Audit log

* Written with `record_audit(...)` / `record_audit_many(...)` (`app/services/common.py`), which
  call `app/services/audit.py`; the row joins the caller's transaction and records actor, IP,
  user agent and request id.
* Action names are `<entity>.<past-tense verb>` (`grade.created`, `grade.deactivated`); `meta`
  names the record and, on updates, `changes: {field: [old, new]}` from `apply_changes`.
* `audit_logs` is range-partitioned by month on `created_at` and append-only: migration `043`
  adds a trigger that refuses UPDATE and DELETE. The worker job `create_partitions` keeps
  partitions three months ahead.
* Presentation (labels, who may read a record's history, sensitive fields) is in
  `app/services/audit_catalogue.py`. Details: [modules/audit.md](modules/audit.md).

## Background jobs and the worker

| Piece | Where | Detail |
|---|---|---|
| Queue | `background_jobs` table (`app/db/models/platform.py`) | Status `queued → running → succeeded / failed / dead`; `dedupe_key` unique while live. |
| Enqueue | `app/services/jobs.py: enqueue` | In the caller's transaction; copies the request id into the payload. |
| Claim and run | `app/workers/runner.py: Worker` | Claims `WORKER_CONCURRENCY` jobs under a lease (`WORKER_LEASE_SECONDS`), commits the claim, runs each handler in its own session. Retries with exponential backoff (30 s doubling, capped at 1 h) up to `max_attempts`; `PermanentJobError` fails at once. Failed or dead jobs notify the administrators (`notification_events.failed_job`). |
| Scheduler | `Worker.tick` | Every 60 s, under a PostgreSQL advisory lock (one worker at a time): reaps expired leases and enqueues `PERIODIC` jobs with a slot dedupe key. |
| Handlers | `app/workers/handlers.py: HANDLERS` | `send_email`, `send_invoice_email`, `send_invoice_void_email`, `send_notification`, `send_payment_reminder_email`, `send_statement_email`; periodic: `create_partitions`, `purge_retention`, `send_timesheet_reminders`, `send_payment_reminders`, `run_recurring_invoices`, `create_engagement_jobs` (daily), `process_integration_items` (every minute). |

Handlers must be idempotent: delivery is at-least-once (module docstring of
`app/workers/handlers.py`). The API never sends mail itself; without a worker, emails stay queued.

## Messaging and notifications

* `app/services/messaging/`: `MessageGateway` interface; `SmtpGateway` (aiosmtplib) and
  `MockGateway` (records messages in `message_log`). `build_gateway` picks SMTP when
  `MESSAGING_BACKEND=smtp` or `SMTP_HOST` is set, else the mock. SMS always goes to the mock.
* Email templates: files in `app/templates/email/` (Jinja2) plus practice overrides in
  `email_templates` (`app/services/email_templates.py`).
* In-app notifications: `NotificationService.notify` (`app/services/notifications.py`) and the
  typed helpers in `app/services/notification_events.py`, which run in a savepoint so a failing
  notification never fails the business action. Types are in `app/core/notification_types.py`.
  Details: [modules/notifications.md](modules/notifications.md).

## Integrations

Every external system sits behind an interface with a `mock` implementation only (setting
values are `Literal["mock"]` in `app/core/config.py`).

| Integration | Interface and mock | Setting |
|---|---|---|
| Central client service | `app/services/central_clients/` (`base.py`, `mock.py`: 25 UK clients, switchable outage and change feed) | `CENTRAL_CLIENT_GATEWAY` |
| Central user directory | `app/services/integrations/central_user.py` | `CENTRAL_USER_GATEWAY` |
| Practice management | `app/services/integrations/practice_management.py` | `PRACTICE_MANAGEMENT_GATEWAY` |
| Bookkeeping | `app/services/integrations/bookkeeping.py` | `BOOKKEEPING_GATEWAY` |
| Subscription | `app/services/integrations/subscription.py` | `SUBSCRIPTION_GATEWAY` |

* Gateways are process-wide singletons (`lru_cache` factories in
  `app/services/central_clients/factory.py` and `app/services/integrations/factory.py`).
* Outbound and inbound work goes through an outbox: `integration_sync_items` rows with a
  per-practice idempotency key (`app/services/integration_outbox.py`), retried with backoff and
  logged to `integration_sync_logs`; the worker job `process_integration_items` runs them
  (`app/services/integration_runner.py: run_scheduled`).
* A practice switches an integration on or off in `integration_connections`
  (`app/services/integration_connections.py`); central-client routes refuse with 409
  `INTEGRATION_DISABLED` while it is off (`require_central` in `app/api/v1/central_clients.py`).
* Mock-only control endpoints answer 404 when `APP_ENV=production`.
* Details: [modules/central-clients.md](modules/central-clients.md). There is no
  `docs/modules/integrations.md` yet, although `app/core/config.py` and `.env.example` refer to it.

## Configuration

`app/core/config.py` is the only module that reads the environment (`Settings`, cached by
`get_settings()`); every variable is listed in the [README](../README.md#2-configure). Points
that matter when changing code:

* With `APP_ENV=test` no `.env` file is read (tests set the environment in `tests/conftest.py`).
* `APP_ENV=production` refuses debug, a JWT secret under 32 characters, SMTP without credentials
  and `AUTH_RATE_LIMIT_FACTOR != 1`, and hides `/docs` and `/openapi.json`.
* `DB_URL` is normalised for asyncpg (`normalise_db_url`: scheme, `sslmode`, encoding of special
  characters in the password).
* Settings are cached; tests change one with `monkeypatch.setattr(get_settings(), ...)`.

## Rate limits and request limits

| Limit | Where | Default |
|---|---|---|
| Global ceiling per signed-in user / per address | `ApiRateLimitMiddleware`, store `memory` (per process) or `database` (`API_RATE_LIMIT_STORE`) | 600 / 120 requests per minute |
| Sign-in, registration, reset and similar | `RateLimiter` (`app/services/rate_limit.py`), always in `rate_limit_counters` (UNLOGGED table), scaled by `AUTH_RATE_LIMIT_FACTOR` | per route, see `app/services/auth.py` |
| Request body on writes | `RequestSizeLimitMiddleware` | `MAX_REQUEST_BODY_KB=1024` |
| Upload endpoints (exempt from the body cap, own limits) | `_UPLOADS` regex in `app/middleware/hardening.py` | `imports/<kind>` (clients, staff, rates, jobs, opening balance time), ticket attachments, expense receipts, invoice template watermark, avatar |

Probes, `/docs`, `/openapi.json` and CORS preflights are not counted. A new upload endpoint must
be added to `_UPLOADS`, or bodies over 1 MB are refused before the route runs.

## File storage

`app/services/storage.py`: a `FileStorage` protocol with `LocalFileStorage`
(`STORAGE_DIR/<practice id>/<2 chars>/<key>`, atomic writes) and `S3FileStorage` (chosen by
`get_storage()` when `AWS_BUCKET_NAME` is set). Keys are random (`new_key`), never derived from
file names, and every call names the practice. Used by ticket attachments, expense receipts,
profile pictures and invoice template watermark images. Operations: [deployment.md](deployment.md#uploaded-files).

## Logging and health

* `app/core/logging.py`: structlog JSON to stdout with `request_id`, `user_id`,
  `organization_id` bound per request, and redaction of keys that look like secrets.
* `GET /health/live` (process up) and `GET /health/ready` (database reachable and
  `alembic_version` equals the head revision, else 503) in `app/api/health.py`.

## Per-process state

These live in the API process and are not shared between processes: the memory rate limiter
(`memory_rate_limiter`), the subscription cache (`app/services/subscription.py`), and every mock
gateway's state (catalogue, outage switch, change feed). With several API processes each has its
own copy; the mocks are meant for development and demonstrations.

## Related repository

The web app is `capiumtimenfeeui` (React). It calls this API under `/api/v1` from the same site,
so the refresh cookie is first-party ([deployment.md](deployment.md#request-path)).
