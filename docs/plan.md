# Capium Time & Fees — production plan

Status as of 17 October 2026. Owner: DexaiTech engineering.
Repositories: [`capiumtimenfeeapi`](https://github.com/DexaiTech/capiumtimenfeeapi) (API, worker,
migrations, docs) and [`capiumtimenfeeui`](https://github.com/DexaiTech/capiumtimenfeeui) (web app).

## 1. Product

Time recording and fee accounting for UK accountancy practices: staff record time against
client jobs, managers approve timesheets, partners turn work in progress (WIP) into VAT
invoices, credit control records payments and chases debt, and everyone sees utilisation,
recoverability and profitability.

| Persona | Needs |
|---|---|
| Staff / accountant | Fast weekly timesheet and timer, their jobs and deadlines |
| Manager | Job budgets, approvals, WIP of their clients |
| Partner | Billing WIP, write-offs, recoverability, client profitability |
| Billing / credit control | Invoices, credit notes, payments, aged debtors, statements |
| Practice admin / owner | Staff, roles, rates, services, VAT, numbering, audit |

### Scope of version 1 (this build)

| Area | Included |
|---|---|
| Practice | Settings, staff (grades, approvers, working hours, optional logins and invitations), roles and 41 permissions, audit log, closed (single-practice) or multi-practice SaaS mode |
| Set-up | VAT rates (UK seeded), services and activity types (with service-to-activity links), staff grades, gapless numbering |
| Rates | Dated charge and cost rate rules (client, service, staff, grade, job), 7-level rate resolution with a preview, rate history, previewed re-rating of unbilled time |
| Clients | Clients with UK identifiers (company no., UTR, VAT no., year end), segment, health, AML status, contacts, engagements (services), CSV import/export; list matching the Capium Figma design |
| Jobs | Jobs per client and service, hourly / fixed fee / non-billable, budgets, assignees, status workflow, actuals |
| Time | Entries, weekly grid, timer, timesheets with submit / approve / reject / recall / reopen, approvals queue |
| Billing | Disbursements, WIP ledger with ageing, write-offs, invoices from WIP with write-up/off apportionment, manual lines and discounts, VAT per line, issue / void, PDF, email with PDF, credit notes, payments with allocation and on-account cash, client statements |
| Reports | WIP, aged debtors, time summary, utilisation, recoverability, billing summary, job profitability, VAT summary; JSON and CSV |
| Dashboard | Personal time and deadlines; practice financial KPIs |

### Later versions (backlog, prioritised)

1. Recurring billing and recurring jobs from client engagements (`client_services.frequency`). Done so far: default tasks per service (`service_task_templates`), added to a new job on request; still to do: creating the jobs themselves on a schedule, per-client task overrides, tasks in the job import.
2. Xero / QuickBooks / Sage integration: push invoices, credit notes and payments; contact sync (OAuth per practice, outbox table for retries).
3. Online payments: GoCardless direct debit, Stripe card links on invoices; automatic receipt matching.
4. Deadlines from Companies House and HMRC (accounts, CT600, confirmation statement, VAT quarters) feeding jobs.
5. Client portal: invoices, statements, payment links, document requests.
6. Automated credit control: reminder sequences by days overdue, statement runs.
7. Receipts and documents on disbursements (S3 storage behind the existing storage interface).
8. Budgets and capacity planning, WIP forecasting; engagement-letter fees vs actuals.
9. Multi-currency (schema already stores `currency` on invoices), multi-entity practices.
10. Mobile-first timer (PWA), calendar import (Outlook/Google) to suggest time.

## 2. Architecture

```text
Browser (React SPA, Vite build) ──HTTPS──► Reverse proxy / TLS (same site for UI and API)
                                              ├── /            static UI (nginx)
                                              └── /api/v1      FastAPI (N stateless processes)
                                                                 │
                     Worker process(es) ◄── jobs table ──────────┤
                     (email, invoice email + PDF,                ▼
                      partitions, retention)              PostgreSQL 18
                                │                         (all data, job queue,
                                ▼                          rate limits, audit)
                              SMTP
```

* Layering: router (HTTP) → service (rules, one transaction) → repository (queries scoped to
  the practice) → PostgreSQL. Background work is enqueued in the same transaction.
* Tenancy: `organization_id` on every row, composite foreign keys so no row can reference
  another practice, practice id taken from the URL and checked against the caller's active
  membership on every request.
* Money: integer pence everywhere, half-up rounding in one module, VAT per line in basis
  points, totals enforced by database check constraints (`gross = net + vat`,
  `paid + credited ≤ gross`).
* Numbering: per-practice sequences locked `FOR UPDATE` in the issuing transaction: gapless
  invoice and credit note numbers.
* WIP integrity: one live allocation per time entry or disbursement (partial unique
  indexes), so an item can never be billed twice; voids reverse allocations, never delete
  history.
* Auth: 15-minute JWT access token in memory, rotating httpOnly refresh cookie with reuse
  detection, Argon2 passwords, per-IP and per-account rate limits.

Stack: Python 3.14, FastAPI, Pydantic 2, SQLAlchemy 2 (async, asyncpg), Alembic,
PostgreSQL 18, fpdf2, structlog · React 19, TypeScript, Vite, Tailwind 4, TanStack Query,
react-router 7, react-hook-form + zod, Vitest + MSW, Playwright.

Details: [domain.md](domain.md) (business rules), `docs/modules/*.md` (endpoint contracts per
module), [billing.md](billing.md) (WIP → invoice → payment with a worked example).

## 3. Delivery milestones

| # | Milestone | Content | Exit criteria | Status |
|---|---|---|---|---|
| M0 | Design | Domain rules, schema, permissions, plan | Reviewed docs | Done |
| M1 | Foundation | Auth, practices, staff, roles, audit, worker, migrations 001–006 | Foundation tests green, `alembic check` clean | Done (tests being adapted) |
| M2 | Clients and set-up | Settings, rates, clients, contacts, engagements, import/export | Module tests, docs/modules/clients.md | In progress (agent) |
| M3 | Jobs and time | Jobs, rate resolution, entries, timer, timesheets, approvals | Module tests incl. locking rules | In progress (agent) |
| M4 | Billing | Disbursements, WIP, write-offs, invoices, PDF, email, credit notes, payments, statements | Full reconciliation test, concurrency tests | In progress (agent) |
| M5 | Reports and dashboard | 8 reports + CSV, dashboard | Exact-number tests on fixed datasets | In progress (agent) |
| M6 | UI foundation | Shell in the Capium design, auth, account, settings, staff, roles, audit | typecheck, lint, unit tests, build | In progress (agent) |
| M7 | UI features | Clients (Figma), jobs, time and timer, approvals, billing, reports, dashboard | Component tests per page, e2e happy paths | Next |
| M8 | Integration and hardening | Full suite on one DB, OpenAPI contract check UI↔API, security review, load test | All gates green; p95 targets met | Next |
| M9 | Staging on the hosted database | Deploy containers, run migrations, bootstrap the practice, UAT with real users | UAT sign-off, backup restore rehearsed | Next |
| M10 | Go-live | Production cut-over, monitoring, support rota | Go-live checklist complete | Planned |

Indicative effort after this build: M7 2–3 weeks, M8 1–2 weeks, M9 1–2 weeks (UAT time
dominated by the practice), M10 1 week. Backlog items are 1–4 weeks each.

## 4. Environments

| Environment | Database | Purpose | Deploys |
|---|---|---|---|
| Local | Local PostgreSQL 18 (`capium_tf_dev`), per-module `*_test` databases | Development, tests | — |
| CI | Postgres 18 service container | Every push and pull request | — |
| Staging | Hosted PostgreSQL (`capium-db` on the provided server) until a separate production database exists | UAT, demos | Merge to `main` |
| Production | Managed PostgreSQL with point-in-time recovery (separate from staging) | Live practices | Tagged release |

The provided server (`51.195.252.84:5423`, PostgreSQL 18.6, empty) is reachable and the
`capium` user can create objects. It is configured as `HOSTED_DB_URL` in the API's local
`.env` (git-ignored) and is **not** used by tests. Before it holds real data:

1. Rotate the `capium` password (it was shared in chat) and store it in the deployment's
   secret store, never in git.
2. Restrict port 5423 to the application servers' IPs (firewall / `pg_hba.conf`), require
   TLS (`sslmode=require` in `DB_URL`), and disable password auth from the internet.
3. Split roles: `capium_migrator` (owns the schema, used only by `alembic upgrade head`) and
   `capium_app` (DML only on application tables, `EXECUTE` on the partition function).
4. Turn on automated base backups + WAL archiving (PITR), and rehearse a restore.
5. Run `alembic upgrade head` as a release step, then
   `python -m app.maintenance.bootstrap --org-name "<practice>" --admin-name ... --admin-email ...`.

## 5. Non-functional requirements

| Area | Target / measure |
|---|---|
| Availability | 99.9% monthly for the API in business hours; health probes `/health/live`, `/health/ready` (DB + migrations at head) |
| Performance | p95 < 300 ms for list and detail endpoints, < 800 ms for reports over 12 months, at 5,000 clients, 100 staff, 1M time entries; invoice PDF < 1 s |
| Scalability | Stateless API processes behind the proxy; worker scales horizontally (leased jobs); read replicas for reports when needed |
| Security | OWASP ASVS L2: RBAC on every route, tenant isolation tests, rate limits, security headers, CSRF defence on cookie routes, Argon2, secrets only from environment, dependency and container scanning in CI, annual pen test |
| Data protection | UK GDPR: lawful basis documented, DPA with practices, subject access export per client/person, erasure that keeps legally required financial records (HMRC: 6 years) |
| Audit | Every financial create/update/void and every permission change in `audit_logs` (monthly partitions, never deleted by the app) |
| Backups / DR | RPO 15 minutes (WAL archiving), RTO 4 hours; monthly restore drill |
| Observability | JSON logs with request id, user and practice; error tracking (Sentry); metrics (request rate, latency, error rate, job queue depth and age); alerts on 5xx rate, readiness failures, dead jobs |
| Accessibility | WCAG 2.2 AA for the UI (keyboard, contrast, labels), tested with axe in component tests |
| Browsers | Last two versions of Chrome, Edge, Safari, Firefox; responsive down to 1280 px for the full grid, usable on tablets |

## 6. Quality gates (CI, required to merge)

* API: `ruff check`, `ruff format --check`, `mypy --strict app`, `alembic upgrade head` +
  `alembic check` (models and migrations in sync), `pytest` on PostgreSQL (unit +
  integration; billing reconciliation and concurrency tests included).
* UI: `tsc -b`, `eslint --max-warnings 0`, `vitest --run`, `vite build`; Playwright smoke on
  the built app against the API in CI.
* Contract: the UI's API types are generated from / checked against `openapi.json`; a schema
  diff fails the build when an endpoint changes without the UI being updated.
* Review: one approving review; migrations reviewed for locking and reversibility; any change
  touching money needs a test with exact expected pence.

## 7. Testing strategy

| Level | What |
|---|---|
| Unit | Money and VAT rounding, apportionment, rate resolution, date bucketing |
| Integration (API) | Every endpoint against a real PostgreSQL: permissions per role, tenant isolation, validation, business rules, error codes |
| Concurrency | Parallel issue (no duplicate or skipped numbers), parallel allocation (no over-allocation), parallel billing of the same WIP |
| Reconciliation | End-to-end: time + disbursement → draft with write-off → issue → part payment → credit note → payment → zero balance; reports agree with ledgers |
| UI component | Pages with MSW-mocked API: rendering, forms, permissions, error states |
| End-to-end | Playwright against a seeded stack: sign in, log a week, submit, approve, bill, pay |
| Load | k6 / Postman performance run on staging with production-sized synthetic data |
| UAT | Scripted scenarios with the practice's own month of data before go-live |

## 8. Release and operations

1. Trunk-based on `main`; short-lived feature branches; conventional commits.
2. CI builds two images (`api`, `ui`) tagged with the commit SHA.
3. Release: run migrations (one job, `capium_migrator`), then roll the API and worker,
   then the UI. Migrations are backwards compatible with the previous release
   (expand → migrate → contract), so a rollback is an image rollback.
4. Runbooks: dead jobs, SMTP failures, readiness failing, slow reports, restoring a deleted
   draft (drafts are not restorable; issued documents are never deleted).
5. Support: error tracking alerts to the on-call channel in business hours; monthly
   dependency updates; quarterly access review of practice admins.

## 9. Go-live checklist

- [ ] Hosted database hardened (section 4) and restore rehearsed
- [ ] `APP_ENV=production`, strong `JWT_SECRET_KEY`, SMTP credentials, `FRONTEND_URL` on HTTPS
- [ ] UI and API served from the same site behind TLS; HSTS on
- [ ] Practice bootstrapped; VAT number, bank details, invoice footer and numbering set
- [ ] Staff created with roles, managers and rates; services and activity codes reviewed
- [ ] Opening balances: unpaid invoices from the previous system imported as issued
      invoices (or one opening-balance invoice per client) and agreed to the old aged debtors
- [ ] Opening WIP imported or billed out in the old system
- [ ] A test invoice issued, emailed, paid and voided in staging
- [ ] Monitoring, alerts and backups verified
- [ ] Users trained; support contact published

## 10. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Rounding or allocation bugs | Wrong invoices, VAT errors | Integer pence, one money module, DB constraints, exact-value tests, reconciliation report |
| Duplicate or missing invoice numbers | HMRC compliance | Locked gapless sequences, unique indexes, concurrency tests |
| Cross-practice data leak | Severe | Composite foreign keys, practice-scoped repositories, isolation tests on every module |
| Exposed database port and shared password | Breach | Section 4 hardening before real data |
| Migration of legacy data | Wrong opening balances | Import tools with dry run, agreed control totals, sign-off |
| Report performance at scale | Slow month-end | Partial WIP indexes, aggregate queries, read replica option, load test in M8 |
| Scope creep (integrations) | Delay | Version 1 scope fixed above; integrations behind an outbox in later versions |
