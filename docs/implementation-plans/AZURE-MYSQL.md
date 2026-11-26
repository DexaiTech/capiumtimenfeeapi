# AZURE-MYSQL: move hosting to Azure, replace S3 with Azure Blob, replace PostgreSQL with MySQL

Status: **Plan ready for review**. No code has changed. Branch: `azure-mysql-migration` (API repo).

## 1. Original requirement

> "add azure resources and remove the aws resources in the full repo and also change db from
> postgresql to mysql server"

Clarifications from the requester:

| Question | Answer as understood |
|---|---|
| Which database engine | **Azure Database for MySQL – Flexible Server** (MySQL, not Microsoft SQL Server) |
| Which Azure resources | Azure Blob Storage instead of S3, **and** infrastructure-as-code for the whole deployment |
| Existing data (database rows, S3 files) | Out of scope for now: handled in a later ticket |
| Ticket id | None yet: work goes on a new branch, `azure-mysql-migration` |

## 2. Interpretation

Three independent workstreams on one branch, delivered as separate commits so each one can be
reviewed (and, if needed, shipped) on its own:

* **A. Storage:** remove Amazon S3 and add Azure Blob Storage behind the existing `FileStorage`
  interface. Small.
* **B. Infrastructure:** add Azure infrastructure-as-code (Bicep) and a manual deploy workflow
  for the API, worker and UI containers. Moderate. Nothing in the repo deploys to Azure today.
* **C. Database:** make the API run on MySQL 8.4 instead of PostgreSQL 18. This is **very large**:
  it touches the models, the repositories, the error handling, the migrations, the test
  harness, CI and the docs.

"Remove AWS in the full repo" covers both repositories. The UI repo has no AWS code. Its only
reference is one line in `docs/system-overview.md` (identical in both repos), which gets updated.

## 3. Existing implementation

### 3.1 AWS today: S3 only

| File | What |
|---|---|
| `app/services/storage.py` | `S3FileStorage` (boto3 in worker threads, SSE AES256), `_s3_client`, `get_storage()` picks S3 when `AWS_BUCKET_NAME` is set, else `LocalFileStorage` |
| `app/core/config.py:111-117` | `aws_bucket_name`, `aws_region`, `aws_access_key_id`, `aws_secret_access_key` |
| `requirements.txt` | `boto3`, `botocore`, `s3transfer`, `jmespath` |
| `pyproject.toml` | mypy `ignore_missing_imports` for `boto3.*`, `botocore.*` |
| `tests/test_s3_storage.py` | Fake boto3 client: layout, encryption, missing file, failures, setting selection |
| `.env.example`, `README.md`, `docs/architecture.md`, `docs/deployment.md`, `docs/plan.md`, `docs/system-overview.md` (both repos), `alembic/versions/047_user_avatar.py` docstring | Docs |

The storage users are ticket attachments, expense receipts and avatars, all through `get_storage()`.
No SES, SQS, RDS or IAM code exists, and there is no infrastructure-as-code (the platform today is Coolify
or a plain Docker host, see `docs/deployment.md`).

### 3.2 PostgreSQL today

About 181k lines of Python, 81 linear migrations (`001`-`081`) and 194 test modules, all on
PostgreSQL through `asyncpg`. These are the PostgreSQL-only features in use (counts from `grep`):

| Feature | Where | MySQL 8.4 situation |
|---|---|---|
| Driver `asyncpg`, URL normalisation, `sslmode` translation | `app/core/config.py:16-60, 200-225` | Replace with `aiomysql` |
| asyncpg exception classes → API error codes (`CONFLICT`, `RATE_OVERLAP`, FK, check, deadlock) | `app/core/handlers.py:5-12, 205-240`, `app/services/rate_rules.py:29,73` | Map MySQL error numbers 1062, 1451/1452, 3819, 1213, 1205 |
| Partial indexes (`postgresql_where`), mostly unique; they enforce business rules such as one live WIP item per time entry | 16 model files, about 70 indexes; about 100 in migrations | **No partial indexes in MySQL** |
| Exclusion constraints (`daterange … &&`) | `app/db/models/rates.py:137` (`ex_rate_rules_scope_period`), `app/db/models/overtime.py:61,102` | **No equivalent** |
| `pg_advisory_xact_lock` / `pg_try_advisory_xact_lock` + `hashtextextended` | `app/maintenance/bootstrap.py:113`, `app/workers/runner.py:142`, `app/repositories/{central_clients,overtime,rate_rules,statement_sends,wip}.py` | `GET_LOCK` is per session, not per transaction |
| `RETURNING` | 8 repositories (`client_merge`, `integration_items`, `jobs`, `job_budgets`, `notifications`, `payment_reminders`, `rate_limits`, `timesheet_reminders`) | Not supported |
| `INSERT … ON CONFLICT` | 16 files, 22 sites (incl. `app/db/migration_helpers.py`) | `ON DUPLICATE KEY UPDATE`, which fires on *any* unique key |
| `JSONB` (and `->>` in indexes and queries) | 34 sites in app (`platform.py:105-115`, `audit.py:87`) | `JSON`; no JSON index without a generated column |
| `ARRAY(Text)` / `ARRAY(SmallInteger)` with `@>` / `<@` CHECKs | `organization.py:71,121,153,234,245,286,543`, `billing_workflow.py:116,150` | No arrays: becomes `JSON` + `JSON_CONTAINS` |
| `UUID` native type, `gen_random_uuid()` defaults | Every primary key; 3 in app, 28 in migrations, 35 in tests | Generic `Uuid` (CHAR(32)) with Python-side defaults |
| `DateTime(timezone=True)` (`timestamptz`) | 93 columns in app, `app/db/base.py:33-36` mixins | `DATETIME(6)` has **no time zone**: store UTC and re-attach on read |
| `INET` + `func.host` | `user.py:71,103`, `platform.py:137`, `audit.py:136` | `VARCHAR(45)` |
| Range-partitioned `audit_logs` + daily `create_partitions` job | `platform.py:125`, `app/workers/handlers.py:200-258`, `alembic/env.py:16-26` | MySQL partitioning forbids foreign keys and needs the partition key in every unique key |
| `regexp_replace(…,'g')`, `~`, `lpad` | `app/repositories/central_clients.py:39-41` | `REGEXP_REPLACE` / `REGEXP` exist, with different syntax |
| `daterange(...) &&` in queries | `app/repositories/client_merge.py:242` | Rewrite as explicit interval overlap |
| `FOR UPDATE [SKIP LOCKED]` | 82 / 11 sites | Supported by MySQL 8: no change |
| Raw SQL `text(...)` | 25 app files, ~450 in migrations, 61 in tests (`::` casts, PG catalog) | Review each |
| `pg_class` catalog queries | `app/maintenance/reset_data.py:33` | `information_schema` |
| `TRUNCATE … CASCADE` between tests | `tests/conftest.py:165-172` | No `CASCADE`; `TRUNCATE` is DDL and slow per table |
| Transactional DDL | All migrations | MySQL commits each DDL statement: a failed migration is left half-applied |
| Case-sensitive text, `lower()` functional unique indexes (`uq_users_email_lower` and others) | 70 `func.lower` sites | MySQL default collation is case-**insensitive** |
| `listen_database_url` (asyncpg LISTEN) | `app/core/config.py:214` | No caller found: remove |

## 4. Root cause

Not a defect. This is a platform change requested by the project. The constraint driving the
design is that **the database enforces business rules** (unique partial indexes, exclusion
constraints, advisory locks, check constraints). Each one must keep the same guarantee and the
same API error code on MySQL. Otherwise billing, WIP and rates regress silently.

## 5. Affected screens and workflows

No UI behaviour changes. Every screen goes through the API, so a database regression would show
up anywhere. The highest-risk workflows are the ones whose rules live in the database:

* Time entry → WIP item (one live WIP atom per entry), billing and fixed-fee billing locks, credit notes, invoice numbering
* Rate rules and overtime rates (no overlapping periods: `RATE_OVERLAP`)
* Central client link/merge (advisory locks, merges overlapping periods)
* Statements and payment reminders, integration outbox, recurring and engagement jobs (worker queue, `SKIP LOCKED`, upserts)
* Uniqueness messages (email, slug, role name, client code, …) via `UNIQUE_MESSAGES`
* Uploads: ticket attachments, expense receipts, avatars (Blob Storage)
* Audit log search (JSON metadata, IP address text search)
* Bootstrap, `reset_data`, worker scheduler (single scheduler lock)

## 6. Proposed solution

### A. Azure Blob Storage (replaces S3)

* New `AzureBlobFileStorage` in `app/services/storage.py` with the **same contract and blob
  layout** as `S3FileStorage`: `<practice id>/<key[:2]>/<key>`, the key regex check, a private
  container, downloads streamed by the API in 64 KB chunks, and every failure raised as `StorageError`.
  A missing blob on `open` raises `StorageError`; a missing blob on `delete` is not an error.
* Use the sync `azure-storage-blob` client in `asyncio.to_thread`, the same pattern as today.
  That avoids an `aiohttp` dependency.
* Authentication: a **managed identity** (`DefaultAzureCredential` from `azure-identity`) when
  `AZURE_STORAGE_ACCOUNT_URL` is set, or `AZURE_STORAGE_CONNECTION_STRING` for local Azurite or
  key-based access. Server-side encryption is always on in Azure Storage (Microsoft-managed keys).
* New settings `AZURE_STORAGE_ACCOUNT_URL`, `AZURE_STORAGE_CONNECTION_STRING` (SecretStr) and
  `AZURE_STORAGE_CONTAINER`. `get_storage()` picks Blob when a container plus one credential is
  set, otherwise `LocalFileStorage` (unchanged). Remove the four `AWS_*` settings.
* Remove `S3FileStorage`, `_s3_client`, the boto3 packages and their mypy override.
  Add `azure-storage-blob==12.31.1` and `azure-identity==1.27.0`, pinned.
* Replace `tests/test_s3_storage.py` with `tests/test_azure_blob_storage.py`. It uses a fake
  container client and covers the same cases (layout, round trip, missing file, delete
  idempotent, failures → `StorageError`, invalid key, setting selection).
* `docker-compose.dev.yml`: add **Azurite** (the Blob emulator) for local runs.

### B. Azure infrastructure (Bicep, new `infra/` folder)

I recommend Bicep over Terraform because it is Azure-native and needs no state backend.

| Resource | Purpose |
|---|---|
| Log Analytics workspace | Container logs (structlog JSON) |
| Virtual network (2 subnets: Container Apps, MySQL delegated) + private DNS zone | MySQL reachable only privately |
| Azure Container Registry (Basic) | `api`, `worker`, `ui` images |
| User-assigned managed identity | AcrPull, Storage Blob Data Contributor, Key Vault Secrets User |
| Container Apps environment (VNet-integrated) | Hosts the apps |
| Container App `api` | Port 8000, internal ingress, probes `/health/live` and `/health/ready` |
| Container App `worker` | No ingress, min 1 replica (`python -m app.workers.main`) |
| Container App `ui` | Port 80, external ingress (HTTPS), `API_UPSTREAM` = the API's internal FQDN |
| Container Apps Job `migrate` | `alembic upgrade head`, run manually before each release |
| Container Apps Job `bootstrap` | First practice and Owner, run once by hand |
| Azure Database for MySQL Flexible Server 8.4 | Burstable B1ms (dev) / General Purpose (prod), `require_secure_transport=ON`, database `capium`, charset `utf8mb4`, backups 7/35 days |
| Storage account (StorageV2, TLS 1.2, public blob access off, shared-key access off in prod) + private container `uploads` | Uploaded files |
| Key Vault (RBAC) | `JWT_SECRET_KEY`, DB password, SMTP password; Container Apps secrets reference it |
| Azure Communication Services Email | **Optional module, off by default**. The existing SMTP gateway works with any SMTP relay |

Files: `infra/main.bicep`, `infra/modules/*.bicep`, `infra/env/dev.bicepparam`,
`infra/env/prod.bicepparam`, `infra/README.md`.

A new `.github/workflows/deploy-azure.yml` runs only on **`workflow_dispatch`** (never on push,
because deploying needs explicit approval). It logs in with GitHub OIDC (federated credential,
no stored secret), builds and pushes images to ACR, runs the `migrate` job, updates `api` and
`worker`, then checks `/api/v1/system`. The UI image is built from the UI repo with the same
pattern. A UI workflow follows only if you want one.

### C. PostgreSQL → MySQL 8.4

Foundation:
* **Driver:** `aiomysql` + `PyMySQL` (pure Python, so they work on Python 3.14). `asyncmy` needs
  compiled wheels that may lag behind 3.14. `DB_URL` accepts `mysql://` and normalises it to
  `mysql+aiomysql://`. TLS (required by Azure) comes from new settings `DB_SSL_MODE`
  (`disabled|required|verify_identity`) and `DB_SSL_CA`, built into an `ssl.SSLContext` in
  `connect_args`. Every connection sets `time_zone='+00:00'`, `sql_mode` (strict,
  `ONLY_FULL_GROUP_BY`, `NO_ZERO_DATE`) and `transaction_isolation='READ-COMMITTED'`. That keeps
  PostgreSQL's default isolation; MySQL's default `REPEATABLE READ` adds gap locks and changes
  what concurrent writers see.
* **Collation:** `utf8mb4_0900_as_cs` (case- and accent-sensitive) as the database and table default,
  so comparisons and uniqueness behave like PostgreSQL today. The `lower()` functional unique
  indexes stay (MySQL 8 supports functional key parts). `icontains` already compiles to
  `lower(x) LIKE lower(y)`.
* **Types** (in `app/db/base.py` / a new `app/db/types.py`): generic `Uuid` with
  `default=uuid.uuid4` (the `gen_random_uuid()` server defaults go); a `UTCDateTime` type
  decorator (`DATETIME(6)`, converts to UTC on write and attaches `timezone.utc` on read, so every
  `datetime` in the app stays aware); `JSON` for `JSONB`; `JSON` lists for `ARRAY`, with the
  `@>` / `<@` CHECKs rewritten as `JSON_CONTAINS`; `String(45)` for `INET`.

Rules the database enforces, each one keeping its constraint name or error code:
* **Unique partial indexes → unique functional indexes** on
  `(CASE WHEN <condition> THEN <col> END)` parts. MySQL unique indexes allow many NULLs, so rows
  outside the condition don't collide. The index name stays the same, so `UNIQUE_MESSAGES` keeps
  working. Non-unique partial indexes become plain indexes.
* **Exclusion constraints → service checks under a lock:** rates and overtime lock the scope
  (see the lock helper below), look for an overlapping period and raise the same
  `RATE_OVERLAP` / `CONFLICT`. Three sites, each with an existing concurrency test to keep.
* **Advisory locks → a transaction-scoped lock table** `advisory_locks(lock_key VARCHAR(64) PRIMARY KEY)`:
  `INSERT … ON DUPLICATE KEY UPDATE` and then `SELECT … FOR UPDATE` (or `FOR UPDATE NOWAIT` for
  the scheduler's try-lock). The lock is released by commit or rollback, the same semantics as
  `pg_advisory_xact_lock`. I chose this over `GET_LOCK`, which is held per session and leaks
  if a release is missed. One helper, `app/db/locks.py`, replaces the 8 call sites.
* `RETURNING` → select or update in the same transaction (ids are generated in Python, so they are
  already known). `ON CONFLICT` → `mysql.insert().on_duplicate_key_update()`, checked per site, because
  MySQL matches any unique key and not a named one.
* `audit_logs`: **drop partitioning** (MySQL partitioned tables can't have foreign keys).
  It becomes a plain table with the existing `(organization_id, created_at)` indexes. The
  `create_partitions` worker job and the `alembic/env.py` partition filter go. The JSON-path
  indexes become generated columns `client_ref` / `ticket_ref` with indexes.
* `central_clients` regex → MySQL `REGEXP_REPLACE` / `REGEXP`; `client_merge` `daterange &&` →
  `a.from <= coalesce(b.to, max) AND b.from <= coalesce(a.to, max)`.
* `app/core/handlers.py` maps PyMySQL `IntegrityError` / `OperationalError` by error number:
  1062 → `CONFLICT`, with the key name parsed from the message to look up `UNIQUE_MESSAGES`;
  1451/1452 → FK `VALIDATION_ERROR`; 3819 → check `VALIDATION_ERROR`; 1213/1205 → 503.
  Connection errors still map to 503.
* `reset_data.py` reads `information_schema`. Bootstrap and the worker scheduler use the lock helper.

Migrations:
* PostgreSQL migrations 001-081 can't run on MySQL, and rewriting 81 of them for a database that
  starts empty has no value. **Proposed (needs approval, see D3):** move them to
  `alembic/postgres_history/` (kept for reference and as the source for the later data-migration
  ticket, not run), and add a hand-written `alembic/versions/001_mysql_baseline.py` that creates
  the full schema, with a working `downgrade()`. From then on, numbering continues linearly.
* `alembic check` must report no difference against the models on MySQL. That is a CI gate.
* Because MySQL DDL isn't transactional, the release checklist in `docs/deployment.md` adds:
  back up before migrating, and restore if `alembic upgrade` fails part-way.

Tests and tooling:
* `tests/conftest.py`: the test database is a MySQL database whose name contains `test`.
  Cleaning between tests uses `SET FOREIGN_KEY_CHECKS=0` and then `DELETE` from the tables that
  have rows, in `metadata.sorted_tables` order (`TRUNCATE` per table is DDL and far slower on MySQL).
* Rewrite the PostgreSQL SQL in tests (61 raw `text()`, 35 `gen_random_uuid`, `::` casts) for MySQL.
* `docker-compose.dev.yml`: `mysql:8.4` on port 3307 (plus a `capium_test` database via
  `docker/init-test-db.sql`), Azurite and Mailpit. `.github/workflows/ci.yml`: a `mysql:8.4`
  service instead of `postgres:18`.
* Docs: README, CLAUDE.md (stack line, test DB instructions), `architecture.md`, `database.md`,
  `deployment.md` (rewritten for Azure), `testing.md`, `system-overview.md` (both repos).

## 7. Files expected to change

| Area | Files |
|---|---|
| Storage (A) | `app/services/storage.py`, `app/core/config.py`, `requirements.txt`, `pyproject.toml`, `tests/test_s3_storage.py` → `tests/test_azure_blob_storage.py`, `.env.example` |
| Infra (B) | new `infra/**`, new `.github/workflows/deploy-azure.yml` |
| DB foundation (C) | `app/core/config.py`, `app/db/session.py`, `app/db/base.py`, new `app/db/types.py`, new `app/db/locks.py`, `app/core/handlers.py`, `app/db/migration_helpers.py`, `requirements.txt`, `pyproject.toml` |
| Models (C) | All files in `app/db/models/` (UUID/datetime types everywhere; indexes, arrays, JSON, INET in about 20) |
| Repositories/services (C) | The ~25 files listed in §3.2 (locks, RETURNING, upserts, regex, ranges, raw SQL), `app/services/rate_rules.py`, `app/services/overtime*.py`, `app/workers/runner.py`, `app/workers/handlers.py`, `app/maintenance/{bootstrap,reset_data}.py`, `app/api/health.py`, `app/main.py` |
| Migrations (C) | `alembic/env.py`, `alembic/versions/*` → `alembic/postgres_history/`, new `alembic/versions/001_mysql_baseline.py` |
| Tests (C) | `tests/conftest.py`, factories and helpers with raw SQL, the 13 `tests/test_*migration*.py` (see D4) |
| Tooling/docs | `docker-compose.dev.yml`, `docker/init-test-db.sql`, `.github/workflows/ci.yml`, `Dockerfile` (drop the storage volume note), `README.md`, `CLAUDE.md`, `docs/{architecture,database,deployment,testing,plan,system-overview}.md`; UI repo `docs/system-overview.md` only |

## 8. UI changes

None in behaviour. UI repo: `docs/system-overview.md` (S3 → Azure Blob, PostgreSQL → MySQL).
The UI's `Dockerfile` and nginx template already take `API_UPSTREAM` / `API_HOST`, which Container
Apps can supply. If a UI deploy workflow is wanted, it's a separate UI-repo commit.

## 9. API changes

No endpoint, schema or error-code changes. Only environment variables change:
removed `AWS_BUCKET_NAME`, `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`; added
`AZURE_STORAGE_ACCOUNT_URL`, `AZURE_STORAGE_CONNECTION_STRING`, `AZURE_STORAGE_CONTAINER`,
`DB_SSL_MODE`, `DB_SSL_CA`; `DB_URL` becomes `mysql://user:pass@host:3306/capium`.

## 10. Database changes

A new engine (MySQL 8.4) with a new baseline schema that is functionally the same as migration
081, apart from these deliberate differences: no `audit_logs` partitioning, a new
`advisory_locks` table, JSON instead of arrays, `VARCHAR(45)` IP addresses, `CHAR(32)` UUIDs and
`DATETIME(6)` UTC timestamps. **No data is copied** (a later ticket).

## 11. Edge cases

* Datetimes: naive values must never reach the database. `UTCDateTime` rejects naive values on
  write (a test for it), and practice-date logic keeps using `app/utils` time-zone helpers.
* Microseconds: `DATETIME(6)` keeps them; ordering by `created_at, id` stays stable.
* Many NULLs in unique functional indexes: soft-deleted and reversed rows must not collide (a
  test per rule).
* `ON DUPLICATE KEY` hitting an unexpected unique key: each site's conflict target is reviewed
  and tested.
* Lock table: deadlock order. Multi-key locks (central clients, WIP per job) keep their existing
  sorted-id order.
* MySQL's 3072-byte index key limit with utf8mb4 and long `Text` columns: index prefixes or
  `VARCHAR` where text columns are indexed.
* `JSON` columns can't have literal defaults in MySQL, only expression defaults `(JSON_ARRAY(...))`.
* A migration that fails part-way can't roll back (non-transactional DDL).
* Storage: a blob container that is missing or a role assignment that hasn't propagated yet gives
  `STORAGE_UNAVAILABLE` (503), the same as an unwritable `STORAGE_DIR` today.

## 12. Regression risks

| Risk | Level | Mitigation |
|---|---|---|
| A business rule enforced by a partial index or exclusion constraint is lost | **High** | One functional index or service check per rule, each with a test; the existing tests for duplicates and overlaps must pass unchanged |
| Lock semantics differ (deadlocks, a lock held too long) | **High** | One lock helper; the existing concurrency tests; `READ COMMITTED` |
| Timezone drift on timestamps | **High** | `UTCDateTime`, UTC session time zone, type tests |
| Upsert conflicts on the wrong key | Medium | Per-site review and tests |
| Collation changes uniqueness or ordering | Medium | `utf8mb4_0900_as_cs`; the existing ordering tests |
| Test suite much slower on MySQL; CI over ~17 min | Medium | `DELETE` of non-empty tables only; parallel jobs if needed |
| Migration history tests removed | Medium | Needs approval (D4); replaced by baseline upgrade/downgrade/`alembic check` tests |
| Live site breaks if this branch deploys before the data migration | **High** | **Do not merge or deploy** C until the data-migration ticket is ready (D5) |

## 13. Testing strategy

* Locally: this machine has no Python 3.14, Docker or database, so I can't run tests here yet.
  I'll run `ruff` and `mypy` once a 3.14 environment exists. **CI with a MySQL 8.4 service is the
  test environment**, run on a draft PR (opened only with your approval).
* New tests: Blob storage (fake client); `UTCDateTime` round trip; the lock helper (two sessions,
  blocking and `NOWAIT`); every converted unique rule (duplicate → `CONFLICT` with the same field
  message); rate and overtime overlap → `RATE_OVERLAP` under concurrency; the MySQL error
  mapping in `handlers.py`; baseline migration upgrade → downgrade → upgrade, plus `alembic check`.
* The full existing suite must pass on MySQL with no test weakened. Every failure is classified
  as pre-existing or new.
* Infra: `az bicep build` / `bicep lint` in CI, then `az deployment group what-if` against a dev
  resource group before any real deployment (needs an Azure subscription and your approval).

## 14. Acceptance criteria

1. No AWS code, dependency, setting or doc reference remains in either repo.
2. With the Azure storage settings, uploads, downloads and deletes go to Blob Storage with the
   same layout and error behaviour. Without them, local storage works as before.
3. `infra/` deploys a working dev environment (API, worker, UI, MySQL, Blob, Key Vault, logs), and
   `/api/v1/system` answers 200 there.
4. The API, worker, `alembic upgrade head`, bootstrap and `reset_data` work on MySQL 8.4. No
   PostgreSQL code, driver or doc reference remains (except `alembic/postgres_history/`, if approved).
5. CI is green on MySQL: ruff, ruff format, mypy, `alembic check` and the full pytest suite.
6. Every API error code for duplicates, overlaps and invalid references is unchanged.

## 15. Delivery order (commits on `azure-mysql-migration`)

1. `AZURE-MYSQL: Azure Blob Storage replaces S3` (A). This can ship on its own against PostgreSQL.
2. `AZURE-MYSQL: Azure infrastructure (Bicep) and manual deploy workflow` (B)
3. `AZURE-MYSQL: MySQL driver, types, locks and error mapping` (C, foundation)
4. `AZURE-MYSQL: models on MySQL (functional unique indexes, JSON, no partitions)`
5. `AZURE-MYSQL: repositories and services on MySQL`
6. `AZURE-MYSQL: migration 001_mysql_baseline (PostgreSQL history archived)`
7. `AZURE-MYSQL: test harness, CI and docs on MySQL`

## 16. Decisions needed before implementation

* **D1.** Database is MySQL 8.4 on Azure Database for MySQL Flexible Server (not SQL Server). Confirm?
* **D2.** IaC tool is Bicep, with private networking (VNet + private MySQL). Or Terraform / public access?
* **D3.** Archive migrations 001-081 to `alembic/postgres_history/` and start a MySQL baseline.
  This restructures existing files, which CLAUDE.md says needs approval.
* **D4.** Retire the 13 PostgreSQL migration tests (`tests/test_*migration*.py`), which test history that
  no longer runs, and replace them with baseline migration tests. CLAUDE.md forbids removing tests without approval.
* **D5.** Ship A (Blob) on its own first, and hold C (MySQL) unmerged until the data-migration
  ticket exists? Recommended: merging C before the data move would point the live site at an
  empty database.
* **D6.** Azure Communication Services Email: include the optional module, or keep the current SMTP provider?
