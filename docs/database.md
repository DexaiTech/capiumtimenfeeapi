# Database

MySQL 8.4 (Azure Database for MySQL Flexible Server in production; `mysql:8.4` in Docker and
CI). 100 tables, defined as SQLAlchemy models in `app/db/models/` (one file per area, all imported
by `app/db/models/__init__.py`) and created only by the Alembic migrations in `alembic/versions/`.
Until migration `100` the schema lived on PostgreSQL; see [MySQL specifics](#mysql-specifics). Business meaning of the columns is
in [domain.md](domain.md) and the module docs; this page is what you need to change the schema
safely.

## Conventions

| Topic | Convention | Evidence |
|---|---|---|
| Primary keys | `id CHAR(36)` (ASCII, binary collation): the UUID's hyphenated text, UUIDv7 generated in Python (`new_id`) | `IdMixin` in `app/db/base.py`, `Uuid36` in `app/db/types.py`, `app/utils/ids.py` |
| Timestamps | `created_at`, `updated_at` (`DATETIME(6)` in UTC, default `CURRENT_TIMESTAMP(6)`); `updated_at` is kept by `ON UPDATE CURRENT_TIMESTAMP(6)`, not by the ORM | `TimestampMixin`; `UTCDateTime` in `app/db/types.py` (aware UTC datetimes in and out) |
| Tenancy | `organization_id UUID NOT NULL REFERENCES organizations ON DELETE CASCADE` | `OrgScopedMixin` |
| Soft delete | `deleted_at` on `organizations`, `users`, `roles`, `clients`, `jobs`, `invoices`, `invoice_templates`; `archived_at` on `vat_rates`, `leads` | `SoftDeleteMixin`; a staff member who is referenced is not hard-deleted, `organization_members.status` changes instead (`active`, `suspended`, `removed`, `inactive`, `left`, `deleted`) |
| Statuses and kinds | `VARCHAR` columns with a `CHECK (... IN (...))`, no `ENUM` types | e.g. `Timesheet.__table_args__` in `app/db/models/time.py` |
| Money | Integer pence (`BigInteger` for amounts and totals) | `app/utils/money.py`; `invoices.net_total`, `payments.amount` |
| Rates | Pence per hour (`charge_rate`, `cost_rate`, `default_charge_rate`, `hourly_rate`) | `TimeEntry` docstring |
| Percentages | Basis points in columns ending `_bp` (`vat_rate_bp` 2000 = 20%, `chargeable_target_bp`, `markup_bp`, `cost_plus_markup_bp`, `overtime_multiplier_bp`) | `app/utils/money.py: BASIS_POINTS` |
| Durations | Whole minutes (`minutes`, `budget_minutes`, `weekly_capacity_minutes`) | `ck_time_entries_minutes` (1..1440) |
| Quantities | `Numeric(12, 2)` on invoice, estimate and recurring lines | `app/db/models/billing.py`, `estimates.py` |
| Snapshots | Values copied at the time of the event, never recomputed: `time_entries.charge_rate/cost_rate/charge_value/cost_value/rate_source`, `invoice_lines.vat_rate_bp` | `TimeEntry` docstring, [domain.md](domain.md#conventions) |
| JSON | `JSON` for free-form payloads (`audit_logs.metadata`, `background_jobs.payload`, `integration_sync_items.payload`) and for lists (`organizations.working_days`, `wip_valuation_sources`, recipients); defaults are expressions (`(JSON_ARRAY())`) | `app/db/models/platform.py`, `integrations.py`, `organization.py` |
| Text | `VARCHAR(n)` where the column is indexed, defaulted or bounded by a CHECK (`n` from the CHECK), `TEXT` for long free text | e.g. `clients.name` `VARCHAR(200)` |

### Constraint and index names

`Base.metadata` has a naming convention (`app/db/base.py: NAMING_CONVENTION`):

| Kind | Pattern | Example |
|---|---|---|
| Primary key | `pk_<table>` | `pk_branches` |
| Foreign key | `fk_<table>_<columns>_<referred table>` | `fk_time_entries_organization_id_client_id_clients` |
| Unique | `uq_<table>_<columns>` | `uq_timesheets_organization_id_user_id_week_start` |
| Check | `ck_<table>_<name>` (models pass the short name: `CheckConstraint(..., name="status")`) | `ck_timesheets_status` |
| Index | `ix_<table>_<columns>` | `ix_time_entries_organization_id_user_id_entry_date` |
| Exclusion | written out by hand | `ex_rate_rules_scope_period` |

Other recurring patterns:

* Case-insensitive uniqueness through an expression index named `uq_<table>_..._name_lower`
  (e.g. `uq_branches_organization_id_name_lower` on `lower(name)`).
* "Only one live row" through partial unique indexes named `..._live`
  (`uq_wip_allocations_time_entry_live`, `WHERE ... reversed_at IS NULL`): an item can be billed
  once at a time.
* Constraint names matter to the API: `UNIQUE_MESSAGES` in `app/core/handlers.py` maps unique
  constraint names to a field and message, and `ex_rate_rules_scope_period` maps to
  409 `RATE_OVERLAP`. Renaming one of those constraints changes the API's error answer.

## Tenancy

Every practice table carries `organization_id`. Parent tables also declare
`UNIQUE (organization_id, id)` (`uq_<table>_organization_id_id`) so that children use composite
foreign keys that include the practice:

```python
ForeignKeyConstraint(
    ["organization_id", "client_id", "job_id"],
    [
        "jobs.organization_id",
        "jobs.client_id",
        "jobs.id",
    ],  # the job must belong to the entry's client
    name="fk_time_entries_organization_id_client_id_job_id_jobs",
)
```

* People are referenced through the membership, not the user: columns such as
  `time_entries.user_id`, `clients.partner_user_id` and `tickets.assignee_user_id` hold a
  `users.id` but the foreign key is `(organization_id, <col>) → organization_members
  (organization_id, user_id)` (`_member_fk` in `app/db/models/clients.py`).
* Tables without `organization_id`: `users`, `user_sessions`, `refresh_tokens`,
  `email_verification_tokens`, `password_reset_tokens`, `permissions`, `role_permissions`,
  `rate_limit_counters`. `audit_logs` and `background_jobs` have a nullable `organization_id`
  without a foreign key (audit rows outlive everything; jobs can be global).
* Deleting an `organizations` row cascades to every practice table; the application only
  soft-deletes practices.

## Tables by area

Counts and key relations below are generated from `Base.metadata` (96 tables). "Org" means the
table has `organization_id`.

| Area (model file) | Tables | Key relations and notes |
|---|---|---|
| Accounts (`user.py`) | `users`, `user_sessions`, `refresh_tokens`, `email_verification_tokens`, `password_reset_tokens` | Not org-scoped. Tokens stored as SHA-256 hashes. `refresh_tokens.replaced_by_id` chains rotations. |
| Platform (`platform.py`, `locks.py`) | `background_jobs`, `rate_limit_counters`, `audit_logs`, `advisory_locks` | `audit_logs`: PK `(id, created_at)`, append-only triggers, generated `client_ref` / `ticket_ref`. `advisory_locks`: the rows behind the named locks (`app/db/locks.py`). |
| Practice and access (`organization.py`) | `organizations`, `organization_members`, `roles`, `permissions`, `role_permissions`, `member_permission_overrides` | `organizations` holds the practice settings (time zone, working week, VAT, approval switches, `rate_mode`, period-lock and overtime settings, `branches_enabled`, ...). Member → role, grade, manager (self-reference). |
| Set-up (`practice.py`) | `vat_rates`, `activity_codes`, `services`, `service_activity_codes`, `service_task_templates`, `grades`, `document_sequences`, `segments`, `staff_segments`, `branches` | Seeded for each new practice (`app/services/practice_defaults.py`). `document_sequences` gives gapless numbers (`app/services/sequences.py`). |
| Rates (`rates.py`) | `rate_rules`, `rate_changes`, `rate_reruns`, `rate_rerun_items` | `rate_rules` has a `kind` (`charge`/`cost`; cost only at `staff` or `grade` level), a `level` and an effective period, with the trigger `ex_rate_rules_scope_period` against overlaps (`app/db/triggers.py`). [team-rates.md](modules/team-rates.md#rates) |
| Clients (`clients.py`) | `clients`, `client_contacts`, `client_services`, `engagement_job_runs`, `leads` | `clients` → partner/manager member, default VAT rate, default service, invoice template, branch, `merged_into_client_id`; `central_client_id` links to the central service; `is_internal` marks the built-in Internal client. |
| Jobs (`jobs.py`) | `jobs`, `job_assignees`, `tasks`, `task_assignees`, `job_budget_alerts` | `jobs` → client, service, partner/manager; `pm_job_id` links to practice management. |
| Time (`time.py`, `time_history.py`) | `timesheets`, `time_entries`, `timers`, `time_entry_history`, `timesheet_reminders` | Entry → timesheet, client, job (of that client), task (of that job), activity code, member. `billing_status` `unbilled → in_draft → billed / written_off`. |
| Billing (`billing.py`, `billing_workflow.py`) | `disbursements`, `wip_write_offs`, `wip_allocations`, `invoices`, `invoice_lines`, `credit_notes`, `credit_note_lines`, `payments`, `payment_allocations`, `invoice_approvals`, `payment_reminders`, `statement_sends` | `wip_allocations` ties one item (time entry, disbursement or expense) to one destination (invoice line or write-off). Totals guarded by checks (`ck_invoices_gross_total`, `ck_invoices_not_over_settled`). [billing.md](billing.md) |
| WIP ledger (`wip.py`) | `wip_movements`, `wip_adjustments`, `wip_transfers`, `credit_note_wip`, `wip_snapshots`, `wip_snapshot_items` | `wip_movements.reversal_of_id` self-reference; movements are reversed, not edited. [wip.md](modules/wip.md) |
| Estimates and recurring (`estimates.py`) | `estimates`, `estimate_lines`, `recurring_schedules`, `recurring_lines`, `recurring_runs` | Estimate → `converted_invoice_id`; run → invoice. |
| Expenses (`expenses.py`) | `expense_types`, `expense_claims`, `expense_claim_events`, `expenses`, `expense_receipts` | Expense line → claim, type, client, job, VAT rate; receipts are files in storage. [expenses.md](modules/expenses.md) |
| Period locks (`period_locks.py`) | `period_locks`, `locked_periods`, `period_lock_events` | [period-locks.md](modules/period-locks.md#data) |
| Notifications (`notifications.py`) | `notifications`, `notification_preferences`, `notification_settings`, `email_templates`, `message_log` | `message_log` is what the mock messaging gateway records. |
| Tickets (`tickets.py`) | `tickets`, `ticket_comments`, `ticket_attachments`, `ticket_tags`, `ticket_tag_links`, `ticket_status_history` | Attachments → ticket or comment; files in storage. |
| Invoice templates (`invoice_templates.py`) | `invoice_templates` | Soft-deleted; one default per practice. |
| Integrations (`integrations.py`) | `integration_connections`, `integration_sync_items`, `integration_sync_logs`, `integration_cursors`, `central_client_cache`, `bookkeeping_service_mappings`, `bookkeeping_vat_mappings` | Outbox items with an idempotency key per practice and integration. |
| Import, onboarding (`data_import.py`, `onboarding.py`) | `data_import_batches`, `data_import_rows`, `onboarding_progress` | |

## Core relationships

Only the main foreign keys; every table also references `organizations`.

```mermaid
erDiagram
    organizations ||--o{ organization_members : "staff"
    users ||--o{ organization_members : "login"
    roles ||--o{ organization_members : "role"
    grades ||--o{ organization_members : "grade"
    organizations ||--o{ clients : ""
    clients ||--o{ client_contacts : ""
    clients ||--o{ client_services : "engagements"
    services ||--o{ client_services : ""
    clients ||--o{ jobs : ""
    services ||--o{ jobs : ""
    jobs ||--o{ tasks : ""
    timesheets ||--o{ time_entries : ""
    organization_members ||--o{ time_entries : "user_id"
    jobs ||--o{ time_entries : ""
    tasks ||--o{ time_entries : ""
    jobs ||--o{ disbursements : ""
    expense_claims ||--o{ expenses : "lines"
    time_entries ||--o{ wip_allocations : ""
    disbursements ||--o{ wip_allocations : ""
    expenses ||--o{ wip_allocations : ""
    invoice_lines ||--o{ wip_allocations : ""
    wip_write_offs ||--o{ wip_allocations : ""
    clients ||--o{ invoices : ""
    invoices ||--o{ invoice_lines : ""
    invoices ||--o{ credit_notes : ""
    credit_notes ||--o{ credit_note_lines : ""
    clients ||--o{ payments : ""
    payments ||--o{ payment_allocations : ""
    invoices ||--o{ payment_allocations : ""
    clients ||--o{ rate_rules : ""
    grades ||--o{ rate_rules : ""
```

## Migrations

### Layout

* Files: `alembic/versions/NNN_description.py` (`file_template = %%(rev)s_%%(slug)s` in
  `alembic.ini`). The revision id is the three-digit number as a string (`revision = "101"`,
  `down_revision = "100"`). The chain is linear with one head.
* It starts at `100_mysql_baseline.py`, which creates the whole schema (equivalent to the last
  PostgreSQL migration, `082`), seeds the permission catalogue and creates the triggers. Its
  docstring lists every deliberate difference from the PostgreSQL schema. The next migration
  is `101`.
* `alembic/postgres_history/` keeps the PostgreSQL migrations `001`-`082` for reference and for
  the data move; Alembic does not read it.
* `alembic/env.py` runs online through an async engine on `DB_DIRECT_URL` (else `DB_URL`), with
  the application's connection settings, and spells `UTCDateTime` / `Uuid36` as plain MySQL
  types in generated revisions.
* A post-write hook formats new revision files with `ruff format` (`alembic.ini`).

### Adding a migration

1. Change the model in `app/db/models/` first.
2. Create the file with the next number:
   `alembic revision -m "short description" --rev-id 101` (the `--rev-id` makes the
   number the revision id and the file name prefix).
3. Write the operations by hand. No file in `alembic/versions/` carries Alembic's autogenerate
   markers; if you start from `--autogenerate`, review every line and remove the markers.
4. Docstring: the description line, `Revision ID` / `Revises`, then the ticket (`Ticket APT79: ...`)
   and what the upgrade and the downgrade do to existing data (examples in
   `alembic/postgres_history/078_internal_client.py`, `079_staff_only_rate_mode.py`).
5. Name constraints exactly as the convention would and wrap them in `op.f(...)` so the
   convention is not applied twice: `op.f("ck_organizations_rate_mode")`.
6. A new table: `organization_id` with `ON DELETE CASCADE`, `UNIQUE (organization_id, id)` if
   other tables will reference it, composite foreign keys to other practice tables, and
   `timestamps()` from `app/db/migration_helpers.py` (its `updated_at` has `ON UPDATE
   CURRENT_TIMESTAMP(6)`, or `updated_at` never changes).
7. A new permission: add it to `CATALOGUE` in `app/core/permissions.py`, call
   `sync_permissions()` in the migration, grant it to roles as needed, and update
   [modules/permissions.md](modules/permissions.md) (checked by `tests/test_permissions_doc.py`).
8. Data steps use `op.execute` with SQL, must be safe to run again, and must not import
   application services (the schema they assume may change later).
9. Check: `alembic upgrade head`, `alembic check` (models and migrations agree),
   `alembic downgrade -1`, `alembic upgrade head`.
10. MySQL commits every DDL statement on its own: a migration that fails half-way stays
    half-applied. Keep each revision small, make data steps re-runnable, and take a backup before
    a release with migrations ([infra/README.md](../infra/README.md#releases)).

### Downgrades

Every revision has a `downgrade()` that reverses the schema and says in the docstring what
happens to data the old schema cannot hold. The baseline's downgrade drops every table with
foreign-key checks off, so it cannot stop half-way. The test suite runs
`alembic downgrade base` then `upgrade head` at the start of every session
(`tests/conftest.py: _migrated_database`), so a broken downgrade breaks every test run.

### Migration tests

`tests/test_mysql_baseline_migration.py` checks the history (one head, starting at `100`), that
the models match the migrations, downgrade and upgrade, the permission seed and the triggers.
A migration with data steps gets a test that downgrades to the previous revision, checks the
data, and upgrades again in a `finally` block (helpers in `tests/migration_helpers.py`:
`migrate`, `head_revision`). These tests are slow; see [testing.md](testing.md#slow-areas).

## MySQL specifics

What differs from the PostgreSQL schema, and the rule to follow when changing it:

| PostgreSQL had | MySQL has | Where |
|---|---|---|
| Partial unique index `... WHERE deleted_at IS NULL` | Functional unique index on `(CASE WHEN <condition> THEN <column> END)` per column: rows outside the condition are NULL and never collide. Same index name, so `UNIQUE_MESSAGES` (`app/core/handlers.py`) still applies | e.g. `uq_clients_organization_id_code` |
| Exclusion constraint (overlapping periods) | `BEFORE INSERT` / `BEFORE UPDATE` triggers that `SIGNAL` the constraint's name; read as `overlap` by `app/db/errors.py` | `app/db/triggers.py: NO_OVERLAP` |
| Append-only trigger functions | `BEFORE UPDATE` / `BEFORE DELETE` triggers; MySQL fires no trigger for a foreign-key cascade, so a practice's rows still go with it | `app/db/triggers.py: APPEND_ONLY` |
| `pg_advisory_xact_lock` | A row of `advisory_locks` held `FOR UPDATE` until the transaction ends | `app/db/locks.py` |
| `timestamptz` | `DATETIME(6)` in UTC; every session runs with `time_zone = '+00:00'` | `app/db/types.py`, `app/db/session.py` |
| `uuid` | `CHAR(36)` ASCII text | `Uuid36` |
| `jsonb`, arrays | `JSON` | |
| Range-partitioned `audit_logs` | One table; generated columns `client_ref` / `ticket_ref` for the JSON lookups | `app/db/models/platform.py` |
| `INSERT ... ON CONFLICT` | `INSERT ... ON DUPLICATE KEY UPDATE`, which fires on *any* unique key of the table | repositories |
| `RETURNING` | Ids are made in Python; rows are read back in the same transaction | repositories |

The database runs `utf8mb4` with `utf8mb4_0900_as_cs` (case- and accent-sensitive, like
PostgreSQL), `READ COMMITTED` and `log_bin_trust_function_creators=ON` (needed to create the
triggers when binary logging is on); `infra/modules/mysql.bicep`, `docker-compose.dev.yml` and CI
set them.

### Deployment order

Migrations run once per release before the new API starts (`alembic upgrade head`, see
[deployment.md](deployment.md#releases)). `GET /health/ready` answers 503 until the database is at
the head revision. The API pull request, which carries the migrations, is deployed before the UI
pull request that depends on it.
