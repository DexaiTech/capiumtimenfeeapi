# Testing

The suite is pytest with `pytest-asyncio` against a real PostgreSQL database. Requests go
through the real application in-process (`httpx.AsyncClient` over `ASGITransport`), so a test
exercises routing, dependencies, services, SQL, constraints and the error envelope together.
There are 185 `tests/test_*.py` files and 20 `tests/*_helpers.py` modules.

## Requirements

* A PostgreSQL database you can drop the schema of. `TEST_DB_URL` points at it. The database
  name must contain the word `test` delimited by `_` or the ends of the name (`capium_test`,
  `capium_test_j`, `capium_tf_test`); otherwise `tests/conftest.py` raises
  `Refusing to run` before anything runs.
* Default when `TEST_DB_URL` is unset: `postgresql://postgres@127.0.0.1:5432/capium_tf_test`.
  That database is not created for you.
* With Docker: `docker compose -f docker-compose.dev.yml up -d postgres` creates `capium_test`
  (`docker/init-test-db.sql`), reachable as
  `postgresql://capium:capium@localhost:5433/capium_test`.
* Without Docker: create one with `psql`, e.g. `CREATE DATABASE capium_test_apt80;`.
* The real `.env` is never read: `APP_ENV=test` is set before the application is imported and
  `Settings` skips the env file in that mode (`app/core/config.py`).

## Commands

```bash
# Linux / macOS
export TEST_DB_URL=postgresql://capium:capium@localhost:5433/capium_test
# Windows (PowerShell)
$env:TEST_DB_URL = "postgresql://capium:capium@localhost:5433/capium_test"

pytest                                   # everything (addopts = -q, testpaths = tests)
pytest tests/test_grades.py              # one file
pytest tests/test_grades.py -k delete    # tests whose name matches
pytest tests/test_grades.py::test_create_read_update_and_clear_a_grade
pytest -x --lf                           # stop at the first failure; rerun last failures

ruff check .
ruff format --check .
mypy app                                 # strict, configured in pyproject.toml; tests/ and alembic/ excluded
```

## What `tests/conftest.py` sets up

| Fixture or step | Scope | What it does |
|---|---|---|
| Environment | import time | Sets `APP_ENV=test`, `DB_URL=TEST_DB_URL`, a test JWT secret, `SMTP_HOST=127.0.0.1` on a free port, `API_RATE_LIMIT_ENABLED=false`, `CLOSED_DEPLOYMENT=false`, `STORAGE_DIR` to a temporary directory; removes `DB_DIRECT_URL`. |
| `_migrated_database` | session, autouse | Runs `alembic downgrade base` then `alembic upgrade head` in a subprocess: the schema is built exactly as a deployment builds it. |
| `mailbox` | session | An in-process SMTP server (`aiosmtpd`) collecting every email; `mailbox.to(address)`, `Mailbox.text(message)`, `mailbox.token_for(address)`. |
| `_clean` | function, autouse | After each test: clears the mailbox, empties the storage directory and runs `TRUNCATE users, user_sessions, refresh_tokens, email_verification_tokens, password_reset_tokens, background_jobs, rate_limit_counters, audit_logs, organizations CASCADE` (every practice table cascades from `organizations`). |
| `_fresh_memory_rate_limiter` | function, autouse | Resets the in-process rate limiter. |
| `central_gateway` | function, autouse | A clean mock central client gateway (25 clients, switched on, empty change feed); tests call `central_gateway.set_available(False)` and push events through it. |
| `integration_gateways` | function, autouse | From `tests/integration_fixtures.py`: clean mock integration gateways and an empty subscription cache. |
| `client` | function | `httpx.AsyncClient` bound to `app.main:app` (`raise_app_exceptions=False`, so a 500 comes back as a response). |
| `db` | function | An `AsyncSession` for direct reads and writes. |
| `worker` | function | A `Worker` to run queued jobs in-process: `await run_jobs(worker)` (module-level helper in `conftest.py`). |
| `verified_user` | function | Registers, verifies and logs in through the real auth flow; returns headers, user and token. |

Every test shares one event loop (`asyncio_default_fixture_loop_scope = "session"` and
`asyncio_default_test_loop_scope = "session"` in `pyproject.toml`) and `asyncio_mode = "auto"`
(no `@pytest.mark.asyncio` needed).

## Factories and helpers

| Module | Use it for |
|---|---|
| `tests/factories.py` | `create_user` (a user with a live session and a bearer token, inserted directly), `create_org` / `create_user_with_org` (practice through the API), `add_member(org_id, role, ...)` (membership inserted directly, any role and status), `role_id`, `audit_entries(org_id, action)`, `error(response)`, `API = "/api/v1"`, the `Actor` dataclass (`id`, `email`, `full_name`, `headers`). |
| `tests/domain_factories.py` | Direct inserts for the domain, bypassing services: `practice(client)` (Owner and seeded practice), `staff(org, role)`, `make_client`, `make_job`, `make_staff_rate`, `make_timesheet`, `make_time_entry`, `make_disbursement`, `vat_rate_id`, `activity_code_id`, `service_id`, `week_start_of`. Values are computed with `app/utils/money.py`, as the services do. |
| `tests/clients_helpers.py` | `org_url(org, path)`, `create_client` (through the API), `sql(statement, **params)` and `scalar(...)` for raw SQL checks, `count_queries()` to assert how many statements a request sends. |
| `tests/<area>_helpers.py` | One per area (`billing`, `time`, `team`, `expense`, `ticket`, `audit`, `period_lock`, `central`, `merge`, `notification`, `report(s)`, `dashboard_page`, `job`, `import_export`, `foundation`, `onboarding`, `time_gap`). Read the module docstring first; most offer API-level actions (`log_time`, `draft`, `issued`, `pay`, `lock_period`, ...). |
| `tests/migration_helpers.py` | `migrate(*args)` (runs Alembic in a subprocess, then disposes the engine), `head_revision()`. |

Rule from the `domain_factories.py` docstring: use direct inserts to set up data owned by
another module; use the API in tests of the module that owns the data.

## Writing a new test

```python
from tests.domain_factories import practice, staff
from tests.factories import audit_entries
from tests.time_helpers import ok_data, org_url


async def test_staff_cannot_create_a_grade(client) -> None:
    owner, org = await practice(client)
    member = await staff(org)                     # role "staff"
    response = await client.post(
        org_url(org, "/grades"), headers=member.headers, json={"code": "LEAD", "name": "Lead"}
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"
    assert await audit_entries(org["id"], "grade.created") == []
```

Checklist, taken from what existing tests cover (`tests/test_grades.py` is a compact example):

* The happy path through the API, asserting the envelope (`data`, `error.code`) and status.
* Permissions: a role without the permission gets 403; another practice's id gets 404.
* Validation and conflicts: 422 `details[].field`, 409 with the module's code.
* Audit: the action is written once with the right `target_type` and `meta`
  (`audit_entries`, or `tests/test_audit_coverage.py` for the cross-module check).
* Money: exact pence, never approximate.
* Background work: run `await run_jobs(worker)` and read `mailbox` or `message_log`.
* Settings: `monkeypatch.setattr(get_settings(), "closed_deployment", True)` (settings are cached).
* Time: pin the clock with `monkeypatch` (see `pin_clock` in `tests/dashboard_page_helpers.py`)
  or use dates relative to the practice's today (`app/utils/practice_dates.py: today_in`).
* OpenAPI: if the route is new, check whether an `tests/test_*_openapi.py` file for the module
  asserts summaries, descriptions, permissions and error codes, and add the route there.

## Tests that check documentation and contracts

| Test | Fails when |
|---|---|
| `tests/test_permissions_doc.py` | `docs/modules/permissions.md` and `app/core/permissions.py` disagree (codes, groups, holders, counts). |
| `tests/test_*_openapi.py` (audit, bulk, central, clients, dashboard pages, expense, foundation, import-export, team-rates, tickets, time gaps) | A route loses its summary, permission or error-code text, or a schema field loses its description. |
| `tests/test_audit_coverage.py` | An important action stops writing exactly one audit entry. |
| `tests/test_audit_append_only.py` | `audit_logs` accepts an UPDATE or DELETE. |

## Slow areas

* Session start: every run (even of one test) downgrades to base and upgrades to head.
* Migration tests: `tests/test_*migration*.py` (for example `test_foundation_migration.py`,
  `test_time_migrations.py`, `test_central_migrations.py`,
  `test_staff_only_rate_mode_migration.py`) and a few others that call Alembic
  (`test_audit_append_only.py`, `test_bootstrap.py`, `test_foundation_maintenance.py`,
  `test_segments.py`). Each round trip runs many migrations in a subprocess. On the team's
  Windows machine the foundation migration set alone has taken about an hour (observed, not
  measured in CI).
* Run the module you changed first, then the related modules, then the full suite.

## Running in parallel

`pytest-xdist` is not a dependency, and every process rebuilds and truncates its database, so
two processes must never share a database. To split the suite:

1. Create one database per process (`capium_test_a`, `capium_test_b`, ...).
2. Start each process with its own `TEST_DB_URL` and its own list of test files:

   ```powershell
   $env:TEST_DB_URL = "postgresql://postgres@127.0.0.1:5432/capium_test_a"; pytest tests/test_a*.py tests/test_b*.py
   ```

3. Keep the migration tests in one process, or spread them evenly: they dominate the run time.

The same applies across git worktrees: give each worktree its own test database.

## Sample data for manual checks

For clicking through a local or staging database (never production; the command refuses when
`APP_ENV=production`), `python -m app.maintenance.seed_leads --practice-slug <slug> --yes` adds
twelve sample leads (open, converted and archived) as the practice's Owner. Without `--yes` it
only shows what it would add; a second run adds nothing. See README "Closed deployment" and
`tests/test_seed_leads.py`.

## Known pitfalls

| Symptom | Cause and fix |
|---|---|
| `RuntimeError: Refusing to run: the test database name must contain the word 'test'` | `TEST_DB_URL` names a database without a `test` word. |
| Session setup fails in `alembic downgrade base` (`CalledProcessError`) | The database was left half-migrated by an interrupted run (or by a branch with migrations that this checkout does not have). Drop and recreate the test database, then rerun. |
| Connection refused / database does not exist | The default `capium_tf_test` on port 5432 is not there; set `TEST_DB_URL`. |
| A rate-limit test sees no 429 | The global limiter is off in the suite (`API_RATE_LIMIT_ENABLED=false`); the tests of the limiter turn it on themselves (`tests/test_hardening.py`, `tests/test_request_overhead.py`). |
| Statement errors after a migration run inside a test | Pooled connections hold prepared statements of the old schema; `migrate()` disposes the engine for that reason (its docstring). Use it instead of calling Alembic yourself. |
| Two runs interfere | Two processes on one database: each needs its own `TEST_DB_URL`. |
| Test passes alone, fails in the suite | State outside the database: a cached setting, a mock gateway, a module-level counter. Use `monkeypatch` and the autouse fixtures; never mutate `get_settings()` without it. |

## Before you push

Run, and report each failure as pre-existing or new:

```bash
ruff check .
ruff format --check .
mypy app
alembic upgrade head && alembic check      # when models or migrations changed
pytest <the modules you touched>           # then the related modules, then everything
```

`.github/workflows/ci.yml` runs the same gates (lint, format, mypy, migrations up/check/
down/up, pytest on PostgreSQL 18) on every pull request and every push to `main`, in about 17
minutes. Check the result with `gh pr checks <n>`.
