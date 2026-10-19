# Permissions

Every action the API protects is guarded by one permission code, checked by the
`require("<code>")` dependency of the route before the target of the request is looked up (so a
caller without the permission gets 403 whatever the id). The codes live in one catalogue,
`app/core/permissions.py` (`CATALOGUE`), are seeded into the `permissions` table by migrations,
and are granted to a practice's roles: the seven system roles below (created with every practice,
not editable) and any custom role a practice builds from the catalogue (`PUT
/organizations/{id}/roles/{role_id}/permissions`; nobody can grant a permission they do not hold
themselves).

There are **63** permissions in **15** groups. `GET /permissions` returns the same catalogue by
group then code; `GET /organizations/{id}` and `GET /users/me/organizations` return the caller's
own permission codes.

> This page is checked against the code by `tests/test_permissions_doc.py`: adding, renaming or
> regranting a permission in `app/core/permissions.py` without changing the matching row below
> fails that test.

## The system roles

| Role | Key | Permissions | Purpose |
|---|---|---|---|
| Owner | `owner` | 63 | Full control, including deleting the practice. |
| Admin | `admin` | 62 | Runs the practice: staff, roles, settings and billing. |
| Partner | `partner` | 58 | Owns client relationships, billing and write-offs. |
| Manager | `manager` | 36 | Runs jobs, approves time and prepares bills. |
| Staff | `staff` | 11 | Records time and disbursements on their work. |
| Billing | `billing` | 28 | Credit control: invoices, credit notes and payments. |
| Viewer | `viewer` | 12 | Read-only access to clients, jobs, time and billing. |

- **Owner** holds every permission, always (including `organization.delete`), and a practice
  always has at least one active Owner.
- **Admin** holds every permission except `organization.delete`.
- **Partner** holds every permission except `data.import_export`, `integration.manage`,
  `organization.delete`, `period_lock.reopen`, `role.manage`.
- **Manager**, **Staff**, **Billing** and **Viewer** hold only the permissions ticked below; Staff
  and Viewer have no period-lock permission at all.

## The catalogue

A tick means the system role holds the permission. Custom roles hold exactly what the practice
gave them.

### Audit

| Code | Description | Owner | Admin | Partner | Manager | Staff | Billing | Viewer |
|---|---|---|---|---|---|---|---|---|
| `audit_log.export` | Export the audit log | ✓ | ✓ | ✓ |  |  |  |  |
| `audit_log.read` | View the audit log | ✓ | ✓ | ✓ |  |  |  |  |

### Billing

| Code | Description | Owner | Admin | Partner | Manager | Staff | Billing | Viewer |
|---|---|---|---|---|---|---|---|---|
| `credit_note.create` | Raise credit notes | ✓ | ✓ | ✓ |  |  | ✓ |  |
| `estimate.manage` | Create, send, convert and delete estimates | ✓ | ✓ | ✓ | ✓ |  | ✓ |  |
| `estimate.read` | View estimates | ✓ | ✓ | ✓ | ✓ |  | ✓ | ✓ |
| `invoice.approve` | Approve invoices awaiting approval | ✓ | ✓ | ✓ |  |  |  |  |
| `invoice.create` | Prepare draft invoices | ✓ | ✓ | ✓ | ✓ |  | ✓ |  |
| `invoice.issue` | Issue, send and void invoices | ✓ | ✓ | ✓ |  |  | ✓ |  |
| `invoice.read` | View invoices and credit notes | ✓ | ✓ | ✓ | ✓ |  | ✓ | ✓ |
| `opening_balance.import` | Import and manage opening balances | ✓ | ✓ | ✓ |  |  | ✓ |  |
| `payment.read` | View payments received | ✓ | ✓ | ✓ | ✓ |  | ✓ | ✓ |
| `payment.record` | Record and allocate payments | ✓ | ✓ | ✓ |  |  | ✓ |  |
| `recurring.manage` | Manage recurring invoice schedules and retry runs | ✓ | ✓ | ✓ |  |  | ✓ |  |
| `recurring.read` | View recurring invoice schedules and their runs | ✓ | ✓ | ✓ | ✓ |  | ✓ | ✓ |
| `wip.adjust` | Write work in progress on or off and reverse it | ✓ | ✓ | ✓ | ✓ |  |  |  |
| `wip.approve` | Approve WIP adjustments above the threshold | ✓ | ✓ | ✓ |  |  |  |  |
| `wip.read` | View work in progress | ✓ | ✓ | ✓ | ✓ |  | ✓ | ✓ |
| `wip.transfer` | Transfer WIP between jobs of a client | ✓ | ✓ | ✓ | ✓ |  |  |  |
| `wip.write_off` | Write off work in progress | ✓ | ✓ | ✓ |  |  |  |  |

### Clients

| Code | Description | Owner | Admin | Partner | Manager | Staff | Billing | Viewer |
|---|---|---|---|---|---|---|---|---|
| `client.create` | Create clients | ✓ | ✓ | ✓ | ✓ |  |  |  |
| `client.delete` | Archive and delete clients | ✓ | ✓ | ✓ |  |  |  |  |
| `client.read` | View clients and contacts | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `client.update` | Edit clients and contacts | ✓ | ✓ | ✓ | ✓ |  | ✓ |  |

### Disbursements

| Code | Description | Owner | Admin | Partner | Manager | Staff | Billing | Viewer |
|---|---|---|---|---|---|---|---|---|
| `disbursement.log` | Record disbursements | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| `disbursement.manage` | Edit and delete any disbursement | ✓ | ✓ | ✓ | ✓ |  | ✓ |  |

### Expenses

| Code | Description | Owner | Admin | Partner | Manager | Staff | Billing | Viewer |
|---|---|---|---|---|---|---|---|---|
| `expense.approve` | Approve and return expense claims | ✓ | ✓ | ✓ | ✓ |  |  |  |
| `expense.log` | Record and submit own expense claims | ✓ | ✓ | ✓ | ✓ | ✓ |  |  |
| `expense.manage` | Manage expense types and edit any claim | ✓ | ✓ | ✓ |  |  |  |  |
| `expense.read_all` | View everyone's expense claims and receipts | ✓ | ✓ | ✓ | ✓ |  | ✓ |  |

### Integrations

| Code | Description | Owner | Admin | Partner | Manager | Staff | Billing | Viewer |
|---|---|---|---|---|---|---|---|---|
| `integration.manage` | Configure integrations, mappings and retries | ✓ | ✓ |  |  |  |  |  |
| `integration.read` | View integration status and sync logs | ✓ | ✓ | ✓ |  |  |  |  |

### Jobs

| Code | Description | Owner | Admin | Partner | Manager | Staff | Billing | Viewer |
|---|---|---|---|---|---|---|---|---|
| `job.create` | Create jobs | ✓ | ✓ | ✓ | ✓ |  |  |  |
| `job.delete` | Delete jobs | ✓ | ✓ | ✓ |  |  |  |  |
| `job.read` | View jobs | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `job.update` | Edit jobs, budgets and assignees | ✓ | ✓ | ✓ | ✓ |  |  |  |

### Period lock

| Code | Description | Owner | Admin | Partner | Manager | Staff | Billing | Viewer |
|---|---|---|---|---|---|---|---|---|
| `period_lock.manage` | Lock time, expenses and WIP up to a date | ✓ | ✓ | ✓ |  |  |  |  |
| `period_lock.read` | View period locks and their history | ✓ | ✓ | ✓ | ✓ |  | ✓ |  |
| `period_lock.reopen` | Reopen a locked period | ✓ | ✓ |  |  |  |  |  |

### Practice

| Code | Description | Owner | Admin | Partner | Manager | Staff | Billing | Viewer |
|---|---|---|---|---|---|---|---|---|
| `data.import_export` | Import and export staff, rates and jobs | ✓ | ✓ |  |  |  |  |  |
| `onboarding.manage` | Work through the onboarding checklist | ✓ | ✓ | ✓ |  |  |  |  |
| `organization.delete` | Delete the practice | ✓ |  |  |  |  |  |  |
| `organization.read` | View practice details | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `organization.update` | Change practice settings and numbering | ✓ | ✓ | ✓ |  |  |  |  |
| `settings.manage` | Manage services, activity codes and VAT rates | ✓ | ✓ | ✓ |  |  |  |  |

### Rates

| Code | Description | Owner | Admin | Partner | Manager | Staff | Billing | Viewer |
|---|---|---|---|---|---|---|---|---|
| `rate.manage` | Set staff and client rates | ✓ | ✓ | ✓ |  |  |  |  |
| `rate.read` | View charge-out and cost rates | ✓ | ✓ | ✓ | ✓ |  |  |  |

### Reports

| Code | Description | Owner | Admin | Partner | Manager | Staff | Billing | Viewer |
|---|---|---|---|---|---|---|---|---|
| `report.export` | Download reports as CSV, Excel or PDF | ✓ | ✓ | ✓ | ✓ |  | ✓ |  |
| `report.financial` | View WIP, debtors, billing and profit reports | ✓ | ✓ | ✓ | ✓ |  | ✓ |  |
| `report.read` | View time and job reports | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

### Roles

| Code | Description | Owner | Admin | Partner | Manager | Staff | Billing | Viewer |
|---|---|---|---|---|---|---|---|---|
| `role.manage` | Create, edit and delete custom roles | ✓ | ✓ |  |  |  |  |  |
| `role.read` | View roles and their permissions | ✓ | ✓ | ✓ | ✓ |  |  |  |

### Staff

| Code | Description | Owner | Admin | Partner | Manager | Staff | Billing | Viewer |
|---|---|---|---|---|---|---|---|---|
| `member.invite` | Add staff accounts | ✓ | ✓ | ✓ |  |  |  |  |
| `member.read` | View the staff list | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `member.remove` | Remove staff | ✓ | ✓ | ✓ |  |  |  |  |
| `member.update` | Change a staff member's role, status and details | ✓ | ✓ | ✓ |  |  |  |  |

### Tickets

| Code | Description | Owner | Admin | Partner | Manager | Staff | Billing | Viewer |
|---|---|---|---|---|---|---|---|---|
| `ticket.create` | Raise tickets | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| `ticket.delete` | Delete tickets and tags, and moderate comments | ✓ | ✓ | ✓ | ✓ |  |  |  |
| `ticket.read` | View tickets, comments, attachments and the dashboard | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `ticket.update` | Edit tickets, tags, comments and attachments | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |  |

### Time

| Code | Description | Owner | Admin | Partner | Manager | Staff | Billing | Viewer |
|---|---|---|---|---|---|---|---|---|
| `time.log` | Log own time and run a timer | ✓ | ✓ | ✓ | ✓ | ✓ |  |  |
| `time.manage_all` | Edit other people's unbilled time | ✓ | ✓ | ✓ | ✓ |  |  |  |
| `time.read_all` | View other people's time | ✓ | ✓ | ✓ | ✓ |  | ✓ | ✓ |
| `timesheet.approve` | Approve and reject timesheets | ✓ | ✓ | ✓ | ✓ |  |  |  |

## Where the period-lock permissions apply

`period_lock.read` shows the lock and its history, `period_lock.manage` moves the lock forward and
`period_lock.reopen` moves it back (it is deliberately held only by Owner and Admin: reopening a
closed period re-opens figures that may already have been reported). The three are independent: a
custom role may hold any of them without the others. See `docs/modules/period-locks.md`.

## Adding a permission

1. Add a `PermissionDef` to `CATALOGUE` and put its code in the sets of the system roles that
   should hold it (Owner and Admin get it automatically; Partner too unless it is excluded in
   `_PARTNER`).
2. Write a migration that calls `sync_permissions()` (`app/db/migration_helpers.py`: it upserts
   the catalogue) and inserts the grants for the system roles of every existing practice, as
   `alembic/versions/019_practice_settings_and_permissions.py` does with its `GRANTS` table. New
   practices copy the system roles from the catalogue.
3. Add its rows to the table above; `tests/test_permissions_doc.py` fails until you do.
