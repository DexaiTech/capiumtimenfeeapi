# Capium Time & Fees API

Backend of Capium Time & Fees: time recording, work in progress (WIP), billing and fee accounting
for UK accountancy practices. Staff record time on client jobs, managers approve timesheets,
partners bill WIP as VAT invoices, credit control records payments, reports show utilisation and
profitability. The React web app is the sister repository `capiumtimenfeeui` (reference only).

**Start with [docs/system-overview.md](docs/system-overview.md)**: the one-page map of the whole
product across both repositories (how they connect, cross-cutting rules, where each area lives).

## Stack

Python 3.14 · FastAPI · Pydantic 2 · SQLAlchemy 2 async (asyncpg) · Alembic · PostgreSQL 18 ·
structlog · Jinja2 · fpdf2 · pytest + pytest-asyncio + httpx · ruff · mypy (strict).
Pinned in `requirements.txt` / `requirements-dev.txt`; tool config in `pyproject.toml`.

## Repository map

| Path | What lives there |
|---|---|
| `app/main.py` | App factory: middleware, exception handlers, routers |
| `app/api/v1/` | Routers, HTTP only; mounted in `app/api/router.py` (order matters) |
| `app/api/health.py` | `/health/live`, `/health/ready` |
| `app/dependencies/` | Auth (`auth.py`), practice scope and `require(...)` permission gate (`organization.py`) |
| `app/services/` | Business rules, transactions, audit; `policy.py` permission decisions; gateways in `central_clients/`, `integrations/`, `messaging/` |
| `app/repositories/` | Queries, always filtered by `organization_id` |
| `app/schemas/` | Pydantic request/response models; envelope in `common.py` |
| `app/db/` | `base.py` (naming convention, mixins), `session.py` (`unit_of_work`), `models/` (one file per area) |
| `app/core/` | `config.py` (only reader of env vars), `exceptions.py`, `handlers.py`, `permissions.py`, `tokens.py`, `logging.py` |
| `app/middleware/` | Request id, rate limit, body size, security headers, gzip |
| `app/workers/` | Background job runner (`runner.py`) and handlers (`handlers.py`) |
| `app/utils/` | Money, pagination, practice dates, shared rules |
| `app/templates/email/` | Email templates |
| `app/maintenance/` | `bootstrap.py` (first practice), `reset_data.py` |
| `alembic/versions/` | Migrations `NNN_description.py` (001-079, linear) |
| `tests/` | pytest suite, `conftest.py`, factories, `*_helpers.py` |
| `docs/` | Documentation (index below); `docs/implementation-plans/` for ticket plans |

## Run locally

```bash
python3.14 -m venv .venv                                  # Windows: py -3.14 -m venv .venv
source .venv/bin/activate                                 # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
cp .env.example .env                                      # set DB_URL, JWT_SECRET_KEY, FRONTEND_URL, BACKEND_URL, SMTP_*
docker compose -f docker-compose.dev.yml up -d postgres mailpit   # optional: Postgres on :5433, Mailpit on :8025
alembic upgrade head                                      # migrate; alembic check = models match migrations
python -m app.maintenance.bootstrap --practice-name "Smith & Co" --admin-name "Ada Smith" --admin-email ada@smithco.example
uvicorn app.main:app --reload --port 8000                 # Swagger at http://localhost:8000/docs
python -m app.workers.main                                # worker: emails, PDFs, reminders, integrations
```

`CLOSED_DEPLOYMENT=true` (default) means one practice and no public sign-up: bootstrap creates
the practice and Owner and prints a temporary password once. Full setup and every env var:
[README.md](README.md).

## Test and check

* Tests need a local PostgreSQL database whose name contains the word `test`
  (`capium_test`, `capium_test_apt80`), passed as `TEST_DB_URL`. Default when unset:
  `postgresql://postgres@127.0.0.1:5432/capium_tf_test` (not created for you).
* Each session runs `alembic downgrade base` + `upgrade head`, and every test ends with a
  `TRUNCATE ... organizations CASCADE`. One database per pytest process (and per worktree).
* A DB left half-migrated by an interrupted run fails setup in `downgrade base`: drop and
  recreate it.
* Migration tests (`tests/test_*migration*.py`) are slow; run the touched module first.

```bash
$env:TEST_DB_URL = "postgresql://postgres@127.0.0.1:5432/capium_test_x"   # bash: export TEST_DB_URL=...
pytest tests/test_grades.py -q          # one module; pytest for everything
ruff check .
ruff format --check .
mypy app
```

Details: [docs/testing.md](docs/testing.md).

## Conventions in brief

* Router (HTTP only) → service (rules, one `unit_of_work` transaction, audit row, job enqueue)
  → repository (queries scoped by `organization_id`) → model.
* Practice routes live under `/api/v1/organizations/{organization_id}/...`; another practice's
  ids answer 404. Gate routes with `require("<code>")`; never check role names.
* Envelope `{success, data, message, meta}`; errors `{success: false, error: {code, message,
  details, request_id}}` from `AppException` subclasses with `UPPER_SNAKE` codes.
* Money is integer pence via `app/utils/money.py` (no floats); percentages and VAT are basis
  points; durations are minutes; dates in the practice time zone.
* Every business write records an audit row (`record_audit`) in the same transaction.
* Route `summary`/`description` name the permission and error codes; schema fields carry
  `Field(description=...)`. OpenAPI tests check this.
* Schema changes only through numbered migrations with a working `downgrade()`; new
  permissions also need `sync_permissions()` and a row in `docs/modules/permissions.md`.
* Commit subjects: `Fix ticket APTnn: <behaviour>`; follow-ups `APTnn: <what>`; name the
  migration when one is added.
* Full list: [docs/coding-standards.md](docs/coding-standards.md).

## Documentation index

| Doc | What it covers |
|---|---|
| [docs/system-overview.md](docs/system-overview.md) | The whole product across both repositories: runtime, cross-cutting rules, area map, doc map (identical copy in the UI repo) |
| [README.md](README.md) | Setup, every environment variable, running, bootstrap, troubleshooting |
| [docs/architecture.md](docs/architecture.md) | Layers, request lifecycle, envelope, errors, auth, tenancy, permissions, audit, worker, integrations, config, limits, storage |
| [docs/database.md](docs/database.md) | Schema by area, tenancy, units, naming, migration conventions |
| [docs/api-reference.md](docs/api-reference.md) | Every endpoint with its permission and module doc |
| [docs/testing.md](docs/testing.md) | Fixtures, factories, writing tests, slow areas, parallel runs, pitfalls |
| [docs/coding-standards.md](docs/coding-standards.md) | Conventions evidenced in the code, commit style |
| [docs/domain.md](docs/domain.md) | Business rules every module follows (the contract) |
| [docs/flow.md](docs/flow.md) | How the product fits together; status paths |
| [docs/billing.md](docs/billing.md) | WIP to invoice to payment, with a worked example |
| [docs/deployment.md](docs/deployment.md) | Containers, environment, uploaded files, releases |
| [docs/plan.md](docs/plan.md) | Product scope, production architecture, milestones, quality gates |
| [docs/modules/audit.md](docs/modules/audit.md) | Audit log endpoints, entries, append-only rule |
| [docs/modules/billing.md](docs/modules/billing.md) | Disbursements, invoices, credit notes, payments, statements, advances, estimates, recurring |
| [docs/modules/central-clients.md](docs/modules/central-clients.md) | Central client gateway, linking, merge, sync log |
| [docs/modules/clients.md](docs/modules/clients.md) | Settings, rates, clients, import, branches, Internal client, Client 360, leads |
| [docs/modules/dashboard.md](docs/modules/dashboard.md) | Dashboard cards, My Actions, widgets |
| [docs/modules/expenses.md](docs/modules/expenses.md) | Expense types, claims, receipts, approval, WIP |
| [docs/modules/foundation.md](docs/modules/foundation.md) | Accounts, practices, staff, roles, audit, worker |
| [docs/modules/import-export.md](docs/modules/import-export.md) | Staff, rates and jobs CSV import/export |
| [docs/modules/invoice-templates.md](docs/modules/invoice-templates.md) | Invoice templates |
| [docs/modules/notifications.md](docs/modules/notifications.md) | Notifications, settings, email templates, messaging gateway |
| [docs/modules/period-locks.md](docs/modules/period-locks.md) | Period locks and how they are enforced |
| [docs/modules/permissions.md](docs/modules/permissions.md) | Permission catalogue and system roles (test-checked) |
| [docs/modules/reports.md](docs/modules/reports.md) | Reports, downloads, recovery analysis |
| [docs/modules/team-rates.md](docs/modules/team-rates.md) | Staff, grades, rate levels, re-rating |
| [docs/modules/tickets.md](docs/modules/tickets.md) | Application Testing tickets, comments, files, tags |
| [docs/modules/time.md](docs/modules/time.md) | Jobs, tasks, time, timer, timesheets, reminders |
| [docs/modules/wip.md](docs/modules/wip.md) | WIP items, ledger, adjustments, transfers |

Not yet documented: onboarding and the integrations (code refers to
`docs/modules/onboarding.md` and `docs/modules/integrations.md`, which do not exist).

## Before any code change (mandatory)

Applies to every ticket, feature, bug fix, refactor or other code change, however small.
Follow it in order; never start coding just because a ticket was given.

1. Read this file completely, then the docs relevant to the change (index above, the module
   docs, `docs/system-overview.md`) and any `docs/implementation-plans/*.md` on the same area.
   Don't treat a doc as outdated without checking it against the code.
2. Inspect the graphify knowledge graph of both repositories if one is available
   (`graphify-out/`, built with the graphify skill): related modules, shared components, the
   UI → API links, impact areas. It never replaces reading the source: verify findings in code.
   If it is missing, outdated or incomplete, say so and use the code and docs. Don't regenerate,
   overwrite or modify graphify outputs without approval.
3. Review the CEO / project lead's instructions and earlier agreed decisions; don't switch
   approach without explaining why and getting approval; ask when something is unclear.
4. Investigate the source: for each file to change, know why it changes, how it connects to
   the request, whether a shared component exists, who depends on it, which screens and
   workflows it affects, and what the docs say about it. Don't touch unrelated files; don't
   delete, rename, restructure or overwrite without a requirement and approval; never overwrite
   uncommitted changes.
5. Trace dependencies and workflows, then find the root cause.
6. Post a **pre-coding checkpoint report**: docs reviewed, graphify information reviewed,
   instructions reviewed, architecture understood, affected modules and dependencies, existing
   implementation, impact and regression risks, proposed approach, files expected to change.
7. Write the implementation plan (below) and **wait for approval. Only then modify code.**

## Engineering workflow (every ticket)

1. **Understand.** Read the whole ticket, including screenshots and comments. Restate it in your
   own words. No code yet.
2. **Investigate the whole application,** not only the named screen: find every screen and
   module that shares the behaviour, the shared component or service behind it, and the
   correct app-wide behaviour.
3. **Root cause** with real code references, tracing the full flow UI → API → DB, and note what
   must be preserved. Never invent a cause.
4. **Plan** in `docs/implementation-plans/<TICKET_ID>.md`: original requirement,
   interpretation, existing implementation, root cause, affected screens, files, proposed
   solution, UI changes, API changes, DB changes, edge cases, regression risks, testing
   strategy, acceptance criteria. **Wait for approval before implementing.**
5. **Implement per the plan:** reuse shared components and services, no hardcoding, no
   unnecessary refactoring, stay backward compatible. If the plan must change, explain why
   before proceeding.
6. **Cross-screen verification** (mandatory): check every screen and module found in step 2.
7. **Regression:** unit, integration and API tests, type checks, lint, format, builds and the
   relevant full suites. Never weaken or remove a test. Classify every failure as pre-existing
   or new.
8. **Manual verification** locally where applicable; say clearly what was verified locally
   versus in production.
9. **Final report** as a table: Ticket ID, Root cause, Implementation, Screens affected, Files
   modified, API changes, Database changes, Tests executed, Tests passed, Tests failed, Manual
   verification, Remaining issues, Commit, PR. Status is one of: Investigated / Plan ready for
   review / Implementation in progress / Ready for review / Verified / Blocked.

Rules:

* Never close tickets: the developer updates the Application Testing dashboard.
* Never merge or deploy without explicit approval.
* Delivery: one branch per ticket batch, one PR per repository, rebase merge. The API PR (with
  migrations) deploys before the UI PR.
* CI (`.github/workflows/ci.yml`, GitHub Actions) runs on every PR and every push to `main`:
  ruff check, ruff format --check, mypy, migrations and the full pytest suite (~17 min). Check it
  with `gh pr checks <n>`; a PR is not ready while it is red. The local full suite takes hours on
  a laptop, so run the affected tests locally and let CI run the rest.
* A migration runs before the new API starts, so the API still live must keep working on the
  migrated database. Only add tables and columns in the release. Drop or rename them a release
  later, once no live code reads them.
* After every merge, check the live site before calling the work done:
  - `/api/v1/system` answers 200;
  - a new route answers 401 instead of 404, which shows the new API is live;
  - the changed screens work.

  If the site is down or answering 500, tell the lead at once. On 6 Nov 2026 the live database
  was missing migration 080's column, and the site was down until someone fixed it on the
  server.
