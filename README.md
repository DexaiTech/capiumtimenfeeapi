# Capium Time & Fees — API

The backend of **Capium Time & Fees**: time recording, work in progress (WIP), billing and
fee accounting for UK accountancy practices. Staff record time against client jobs, managers
approve timesheets, partners turn WIP into VAT invoices, credit control records payments, and
everyone sees utilisation, recoverability and profitability.

This repository holds the FastAPI service, the background worker, the database migrations and
the documentation. The web app lives in the sibling repository `capiumtimenfeeui`.

- Business rules (the contract every module follows): [docs/domain.md](docs/domain.md)
- Product scope, architecture and roll-out plan: [docs/plan.md](docs/plan.md)
- Per-module endpoints, shapes and error codes: [docs/modules/](docs/modules/)

## What it does

| Area | Contents |
|---|---|
| Accounts | Registration (multi-practice mode), email verification, login, rotating refresh tokens with reuse detection, logout everywhere, password reset and change, sessions |
| Practice | Practice settings and billing settings (VAT registration, payment terms, default charge rate, time increment (6 minutes for a new practice), timesheet approval, address, bank details, invoice footer), the settings the expense, WIP, invoice-approval and rate modules read (fixed-fee WIP mode, WIP-adjustment, receipt and invoice-approval thresholds, mileage rate, expense markup, rate mode), staff with initials, manager, weekly capacity and chargeable target, seven system roles plus custom roles over 63 permissions, audit log - [docs/modules/foundation.md](docs/modules/foundation.md), [docs/modules/permissions.md](docs/modules/permissions.md) |
| Period lock | Lock a month, quarter or calendar year of time, expenses and WIP, and unlock any one of them with a reason, an append-only history, and 409 `PERIOD_LOCKED` on every write dated in the closed period - [docs/modules/period-locks.md](docs/modules/period-locks.md) |
| Set-up | UK VAT rates, activity codes, services, expense types and gapless number sequences (clients, jobs, invoices, credit notes, staff, tickets and estimates), seeded for every new practice |
| Rates | Staff charge/cost rate history, client and job rates, seven-level rate resolution (optionally by grade only, skipping the staff level) |
| Clients and jobs | Clients, contacts, jobs (hourly, fixed fee, non-billable), budgets and actuals |
| Clients 360 | The central client service behind an interface (a mock for now): search its catalogue, enable its clients (never a duplicate: a local client with the same company number is linked instead), link, unlink and synchronise the centrally managed name and address, apply central events (rename, archive, restore, merge two clients with all their work), a cached fallback while the service is down, the integration sync log; client billing settings (invoice email, default service, invoice template), invoice templates, and the one-call client overview behind the tabs - [docs/modules/central-clients.md](docs/modules/central-clients.md), [docs/modules/invoice-templates.md](docs/modules/invoice-templates.md), [docs/modules/clients.md](docs/modules/clients.md) |
| Time | Entries, timer, weekly timesheets with submit / approve / reject / recall / reopen |
| Billing | Disbursements, WIP and write-offs, invoices from WIP, credit notes, payments and allocation, invoice PDF and email |
| Notifications | In-app notifications (bell, unread count), notification settings per person and practice, editable email templates with merge fields and preview, the messaging gateway (SMTP, or a mock that records messages; SMS is always the mock) - [docs/modules/notifications.md](docs/modules/notifications.md) |
| Reports | WIP, aged debtors, time, utilisation, recoverability, billing, profitability, VAT; JSON and CSV |
| Application Testing | The practice's own tracker for bugs and enhancements: tickets (`APT271`) with status, priority, tags, assignee, comments and file attachments (images, PDF, Word, Excel, CSV, up to 2 MB; stored under `STORAGE_DIR`), bulk actions, an overview dashboard (KPIs against the previous period, status trend) and an audit trail - [docs/modules/tickets.md](docs/modules/tickets.md) |

Money is integer pence everywhere, VAT rates are basis points, durations are minutes.

## Stack

Python 3.14 · FastAPI · Pydantic 2 · SQLAlchemy 2 (async, asyncpg) · Alembic · PostgreSQL 18 ·
PyJWT · Argon2 · aiosmtplib · Jinja2 · fpdf2 · structlog · pytest · ruff · mypy (strict)

```text
Router (HTTP only) → Service (rules, transaction, audit) → Repository (queries, per practice) → PostgreSQL
                         └→ background_jobs table → Worker → SMTP (emails, invoice PDFs), partitions, retention
```

Every business route lives under `/api/v1/organizations/{organization_id}/…` (an
*organization* is a practice) and every query is filtered by `organization_id`; another
practice's ids answer 404. Responses use one envelope: `{success, data, message, meta}`, errors
`{success: false, error: {code, message, details, request_id}}`.

```text
app/
  main.py          application factory          api/v1/        routers (HTTP only)
  core/            settings, errors, permissions schemas/       request and response models
  db/              engine, session, models       services/      business rules
  repositories/    database queries              dependencies/  auth, practice scope
  middleware/      request id, rate and size     workers/       job runner and handlers
                   limits, security headers      templates/     email templates
  maintenance/     bootstrap, reset_data
alembic/           migrations                    tests/         test suite
```

## Setup

Requirements: Python 3.14, PostgreSQL 15 or newer (18 recommended), an SMTP server (Mailpit
for development). Docker is optional.

### 1. Install

With [uv](https://docs.astral.sh/uv/):

```bash
uv venv --python 3.14 .venv
# Linux / macOS
source .venv/bin/activate
# Windows (PowerShell)
.venv\Scripts\Activate.ps1
uv pip install -r requirements-dev.txt
```

With pip:

```bash
python3.14 -m venv .venv          # Windows: py -3.14 -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
```

`tzdata` is a dependency, so time zones work on Windows and in slim containers too.

### 2. Configure

```bash
cp .env.example .env              # Windows: Copy-Item .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"   # a value for JWT_SECRET_KEY
```

`.env` is ignored by git and must never be committed. The settings (all read in
`app/core/config.py`):

| Variable | Required | Meaning |
|---|---|---|
| `APP_ENV` | | `development` (default), `test` or `production`. Production refuses debug, a short JWT secret, missing SMTP credentials and a scaled auth limit; it also hides `/docs`. |
| `APP_DEBUG` | | Verbose logs. Must be false in production. |
| `DB_URL` | yes | PostgreSQL URL (`postgresql://user:password@host:5432/db`, `?sslmode=require` for hosted databases). Converted for asyncpg; special characters in the password are encoded for you. |
| `DB_DIRECT_URL` | | Non-pooled URL for migrations, when `DB_URL` goes through a pooler. Defaults to `DB_URL`. |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | | Connection pool per process (10 and 5). |
| `JWT_SECRET_KEY` | yes | At least 32 random characters in production. |
| `JWT_ALGORITHM`, `ACCESS_TOKEN_EXPIRE_MINUTES`, `REFRESH_TOKEN_EXPIRE_DAYS` | | HS256, 15 minutes, 30 days. |
| `FRONTEND_URL` | yes | The web app's origin; email links are built from it. |
| `BACKEND_URL` | yes | The API's public origin. |
| `CORS_ORIGINS` | | Comma-separated allowed origins. Defaults to `FRONTEND_URL`. |
| `SMTP_HOST`, `SMTP_PORT` | yes | Port 465 uses implicit TLS; other ports STARTTLS when offered. |
| `SMTP_USER`, `SMTP_PASSWORD` | prod | Required in production. |
| `SMTP_FROM_ADDRESS` | yes | Sender address; `SMTP_FROM_NAME` defaults to "Capium Time & Fees". |
| `MESSAGING_BACKEND` | | `smtp` or `mock`. `mock` records every message in the `message_log` table instead of sending it. Default: `smtp` when `SMTP_HOST` is set, else `mock`. SMS is always the mock. |
| `CLOSED_DEPLOYMENT` | | `true` (default): one practice, no public sign-up (see below). `false`: multi-practice SaaS. |
| `CENTRAL_CLIENT_GATEWAY` | | The central client service: `mock` (default, and the only gateway so far) is an in-memory catalogue of 25 UK clients that can be switched off and fed events, for development and demonstrations. Its switches (`/central-clients/mock/...`) answer 404 when `APP_ENV=production` ([docs/modules/central-clients.md](docs/modules/central-clients.md)). |
| `API_RATE_LIMIT_ENABLED`, `API_RATE_LIMIT_USER_PER_MINUTE`, `API_RATE_LIMIT_IP_PER_MINUTE` | | Global request ceiling: 600 per signed-in user, 120 per address otherwise. |
| `MAX_REQUEST_BODY_KB` | | Body cap for writes (1024), except bulk imports and ticket file uploads (which have their own 2 MB file limit). |
| `STORAGE_DIR` | | Where uploaded files (ticket attachments) are kept. Empty: `storage` next to `app/` (git-ignored). A relative path is resolved against the project root. In Docker it is `/var/lib/capium/storage`: mount a persistent volume there and back it up with the database ([docs/deployment.md](docs/deployment.md)). |
| `AWS_BUCKET_NAME` | | Amazon S3 bucket for every uploaded file: ticket attachments, expense receipts and profile pictures. When set, `STORAGE_DIR` is not used. The bucket stays private (the API checks permissions and streams each download) and the IAM user needs `s3:PutObject`, `s3:GetObject` and `s3:DeleteObject` on it. Objects are stored as `<practice id>/<2 characters>/<random key>` (profile pictures under the user's id) with server-side encryption. Files already in `STORAGE_DIR` are not copied: copy them into the bucket with the same paths before switching an existing deployment. |
| `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | | The bucket's region and the IAM user's key pair. Leave the key pair empty to use the AWS default credential chain (an instance or task role). |
| `AUTH_RATE_LIMIT_FACTOR` | | Multiplies the sign-in/registration limits for scratch stacks only; must be 1 in production. |
| `WORKER_CONCURRENCY`, `WORKER_POLL_INTERVAL_SECONDS`, `WORKER_LEASE_SECONDS` | | Worker tuning (5, 2.0, 300). |

### 3. Database and migrations

Create an empty database, point `DB_URL` at it, then:

```bash
alembic upgrade head          # apply every migration
alembic check                 # fail if the models and the migrations have drifted
alembic downgrade -1          # undo the last migration
```

The schema changes only through migrations (`alembic/versions/NNN_description.py` with both
`upgrade` and `downgrade`). The permission catalogue is seeded by migration.

A local PostgreSQL 18 and Mailpit are available through Docker:

```bash
docker compose -f docker-compose.dev.yml up -d postgres mailpit
# Postgres on localhost:5433 (database "capium", test database "capium_test",
# user/password "capium"/"capium" - local only); Mailpit inbox on http://localhost:8025
```

## Running

```bash
uvicorn app.main:app --reload --port 8000     # API, Swagger UI at http://localhost:8000/docs
python -m app.workers.main                    # worker: sends every email, invoice emails with PDF
```

The API never sends mail itself: without a worker, verification, password and invoice emails
stay queued in `background_jobs`. Health probes: `GET /health/live`, `GET /health/ready` (503
when the database is unreachable or the migrations are not at head).

Everything in containers (Postgres, Mailpit, migrations, API, worker):

```bash
docker compose -f docker-compose.dev.yml --profile app up --build
```

### Closed deployment (one practice)

With `CLOSED_DEPLOYMENT=true` (the default) registration and practice creation answer 403, and
staff accounts are made by an administrator (`POST /organizations/{id}/people`, which returns a
temporary password that must be changed at first sign-in). Create the practice and its Owner
once, after the migrations:

```bash
python -m app.maintenance.bootstrap --practice-name "Smith & Co Accountants" \
    --admin-name "Ada Smith" --admin-email ada@smithco.example
python -m app.maintenance.bootstrap --help
```

It prints a generated 16-character password once (or use `--password`), refuses when a
practice already exists and never prints the database URL. The practice is seeded with the UK
VAT rates, activity codes, services and number sequences, exactly like one created through the
API.

`python -m app.maintenance.reset_data` shows what it would delete; with `--yes` it empties
every table except the schema version and the permission catalogue. It does not touch the
uploaded files: empty `STORAGE_DIR` yourself when you start over.

## Tests

The suite runs against a disposable PostgreSQL database whose name **must end in `_test`**; it
rebuilds the schema from the migrations at the start, truncates after every test and never
reads `.env`. Email goes to an in-process SMTP server.

```bash
# Linux / macOS
export TEST_DB_URL=postgresql://capium:capium@localhost:5433/capium_test
# Windows (PowerShell)
$env:TEST_DB_URL = "postgresql://capium:capium@localhost:5433/capium_test"

pytest                                      # everything
pytest tests/test_members.py -q             # one file
ruff check . && ruff format --check . && mypy app
```

Without `TEST_DB_URL` the default is `postgresql://postgres@127.0.0.1:5432/capium_tf_test`.
`scripts/dev_mail_sink.py` is a Mailpit stand-in (SMTP on :1025, a small HTTP API on :8025)
for browser end-to-end runs without Docker.

## Deployment

- Build one image (`Dockerfile`: the API; `--target worker` or `Dockerfile.worker`: the
  worker). Both run as a non-root user.
- Release step: `alembic upgrade head` (with `DB_DIRECT_URL` when the runtime URL is pooled),
  then start the API (`uvicorn app.main:app --proxy-headers`, several processes behind the
  reverse proxy) and at least one worker from the same image.
- Serve the web app and the API from the same site (for example `/` and `/api/v1` behind one
  TLS proxy): the refresh token is an httpOnly cookie scoped to `/api/v1/auth`.
- Set `APP_ENV=production`, a strong `JWT_SECRET_KEY`, real SMTP credentials and the hosted
  `DB_URL` in the platform's secret store, never in the repository.
- Closed deployment: run `python -m app.maintenance.bootstrap` once after the first migration.
- CI (`.github/workflows/ci.yml`) runs ruff, the format check, mypy, `alembic upgrade head` and
  `alembic check`, and the tests against a PostgreSQL 18 service.

The production architecture and roll-out plan are in [docs/plan.md](docs/plan.md).

## Troubleshooting

| Symptom | Cause |
|---|---|
| Start-up fails naming a variable | A required setting is missing or invalid. The value is never printed. |
| No email arrives | The worker is not running, or the SMTP settings are wrong (`email_failed` in the worker log). |
| Signed out every 15 minutes | The web app and the API are on different sites, so the refresh cookie is not sent. |
| `403 PASSWORD_CHANGE_REQUIRED` | The account still has its temporary password: change it via `POST /api/v1/users/me/password`. |
| `/health/ready` answers 503 | The database is unreachable or the migrations are behind. |
| Tests refuse to start | `TEST_DB_URL` does not end in `_test`. |
