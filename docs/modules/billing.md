# Billing: disbursements, WIP, invoices, credit notes, payments, statements

Code: `app/api/v1/{disbursements,wip,invoices,credit_notes,payments}.py` →
`app/services/{disbursements,wip,invoices,invoice_pdf,credit_notes,payments,statements,billing_common}.py`
→ `app/repositories/{disbursements,wip,invoices,credit_notes,payments,billing_common}.py`; schemas
in `app/schemas/{billing,invoices,payments}.py`. The worker job `send_invoice_email` is in
`app/workers/handlers.py`; the `invoice` email template in `app/templates/email/invoice.{html,txt}`.
Tests: `tests/test_disbursements.py`, `tests/test_wip.py`, `tests/test_invoices*.py`,
`tests/test_credit_notes.py`, `tests/test_payments.py`, `tests/test_statements.py` (helpers in
`tests/billing_helpers.py`). **No schema change**: migration `009` is still a no-op.

The money flow with a worked example is in [`docs/billing.md`](../billing.md).

All paths are under `/api/v1/organizations/{organization_id}`. Money is integer pence, VAT rates
basis points, `quantity` and `hours` are decimal strings with two places (`"1.50"`). Another
practice's ids answer 404. Lists are keyset-paginated (`limit`, `cursor`, `meta.next_cursor`)
unless marked bounded.

## Endpoints

### Disbursements

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/disbursements` | `wip.read` (all) or `disbursement.log` (own only) | Newest first by `incurred_on`. Filters `client_id`, `job_id`, `billing_status` (repeatable), `created_by`, `date_from`, `date_to`, `is_billable`. |
| POST | `/disbursements` | `disbursement.log` or `disbursement.manage` | Record `{client_id, job_id?, incurred_on, description, supplier?, amount>0, vat_rate_id?, is_billable=true}`. 201. |
| GET | `/disbursements/{id}` | `wip.read` or own | One disbursement (403 for someone else's without `wip.read`). |
| PATCH | `/disbursements/{id}` | own with `disbursement.log`, any with `disbursement.manage` | Change any field while `unbilled`. |
| DELETE | `/disbursements/{id}` | as PATCH | Delete while `unbilled`. |

### WIP

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/wip` | `wip.read` | One row per client with WIP, by client name: `minutes`, `hours`, `time_value`, `disbursement_value`, `total`, `oldest_date`, `ages{days_0_30, days_31_60, days_61_90, days_91_plus}`, `item_count`. Filters `client_id`, `partner_user_id`, `manager_user_id` (the client's), `job_id`. |
| GET | `/clients/{client_id}/wip?job_id=` | `wip.read` | Bounded: every WIP item (`kind` time/disbursement, `date`, `job`, `person`, `minutes`, `hours`, `rate`, `value`, …) in date order, plus `totals`. |
| POST | `/wip/write-offs` | `wip.write_off` | `{client_id, items:[{time_entry_id}|{disbursement_id}] | job_id, reason, written_off_on?}`. 201. |
| GET | `/wip/write-offs` | `wip.read` | Newest first by date; filters `client_id`, `job_id`, `date_from`, `date_to`. |
| GET | `/wip/write-offs/{id}` | `wip.read` | One write-off with its `items` (`kind`, `id`, `wip_value`). |
| DELETE | `/wip/write-offs/{id}` | `wip.write_off` | Reverse: allocations deleted, items back to `unbilled`. |

### Invoices

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/invoices` | `invoice.read` | Newest first (`created_at`). Filters `client_id`, `status` (display status, repeatable), `date_from`/`date_to` (issue date), `q` (number, client name or code), `outstanding`. |
| POST | `/invoices` | `invoice.create` | Draft from WIP (below). 201, returns the detail. |
| GET | `/invoices/{id}` | `invoice.read` | Detail (below). |
| PATCH | `/invoices/{id}` | `invoice.create` (+ `invoice.issue` when issued) | Draft: `issue_date`, `due_date`, `reference`, `notes`. Issued: `notes`, `reference` only. |
| DELETE | `/invoices/{id}` | `invoice.create` | Delete a draft; its WIP returns to `unbilled`. |
| POST | `/invoices/{id}/lines` | `invoice.create` | Manual line `{description, quantity=1, unit_amount, net_amount?, vat_rate_id?, service_id?, job_id?, position?}`. 201. |
| PATCH | `/invoices/{id}/lines/{line_id}` | `invoice.create` | `{description, quantity, unit_amount, net_amount, vat_rate_id (null = default resolution), position}`. |
| DELETE | `/invoices/{id}/lines/{line_id}` | `invoice.create` | Remove a line; its WIP returns to `unbilled`. |
| POST | `/invoices/{id}/wip` | `invoice.create` | Add WIP `{time_entry_ids, disbursement_ids, group_by, overrides}` to a draft. |
| POST | `/invoices/{id}/issue` | `invoice.issue` | Optional `{issue_date, due_date}`. Numbers it, WIP → `billed`. |
| POST | `/invoices/{id}/void` | `invoice.issue` | `{reason}`. Reverses the WIP allocations. |
| GET | `/invoices/{id}/pdf` | `invoice.read` | `application/pdf` (DRAFT/VOID watermark when not issued). |
| POST | `/invoices/{id}/send` | `invoice.issue` | Optional `{to:[email]}`. 202 `{invoice_id, to, queued}`. |

### Credit notes

| Method | Path | Permission | Purpose |
|---|---|---|---|
| POST | `/invoices/{id}/credit-notes` | `credit_note.create` | `{lines:[{description, net_amount>0, vat_rate_id?}], reason, issue_date?}`. 201. |
| GET | `/credit-notes` | `invoice.read` | Newest first by date; filters `client_id`, `invoice_id`, `date_from`, `date_to`, `q`. `lines` is null in the list. |
| GET | `/credit-notes/{id}` | `invoice.read` | With lines. |
| GET | `/credit-notes/{id}/pdf` | `invoice.read` | PDF. |

### Payments and statements

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/payments` | `payment.read` | Newest first by `received_on`. Filters `client_id`, `date_from`, `date_to`, `method` (repeatable), `state` (`unallocated`, `part_allocated`, `allocated`, `void`, `on_account` = some money unallocated), `q` (reference/notes). |
| POST | `/payments` | `payment.record` | `{client_id, received_on, amount>0, method, reference?, notes?, allocations?:[{invoice_id, amount}], auto_allocate?}`. 201. |
| GET | `/payments/{id}` | `payment.read` | With `allocations`. |
| POST | `/payments/{id}/allocations` | `payment.record` | `{allocations}` or `{auto_allocate: true}`. |
| DELETE | `/payments/{id}/allocations/{allocation_id}` | `payment.record` | Unallocate. |
| POST | `/payments/{id}/void` | `payment.record` | Optional `{reason}` (kept in the audit log). |
| GET | `/clients/{client_id}/statement?date_from&date_to` | `invoice.read` + `payment.read` | Statement JSON (below). |
| GET | `/clients/{client_id}/statement/pdf?date_from&date_to` | `invoice.read` + `payment.read` | Statement PDF. |

## Shapes

**Invoice detail** (`InvoiceRead`): `id, number, status (draft|issued|void), display_status,
client{id,code,name}, issue_date, due_date, reference, notes, currency, net_total, vat_total,
gross_total, paid_total, credited_total, balance, sent_at, issued_at, issued_by, voided_at,
void_reason, created_at`, plus

* `lines[]`: `id, position, kind (time|fixed_fee|disbursement|manual), description, job_id,
  service_id, quantity, unit_amount, net_amount, vat_rate_id, vat_rate_bp, vat_amount,
  gross_amount, wip` where `wip` (null on manual lines) is `{item_count, time_entry_ids,
  disbursement_ids, wip_value, billed_value, adjustment}`; `adjustment = billed − wip`
  (negative = write-off, positive = write-up);
* `vat_summary[]`: `{vat_rate_bp, net, vat}` per rate, highest first;
* `wip`: `{wip_value, billed_value, adjustment}` for the whole invoice;
* `payments[]`: `{allocation_id, payment_id, received_on, method, reference, amount}`;
* `credit_notes[]`: `{id, number, issue_date, reason, gross_total}`.

The list returns `InvoiceSummaryRead` (the header fields only).

**Draft request** (`POST /invoices`): `{client_id, issue_date?, due_date?, reference?, notes?,
time_entry_ids[], disbursement_ids[], group_by: job|entry|single (default job), overrides[]}`.
An override picks one line by exactly one of `job_id` (the line billing that job's time),
`time_entry_id` (the line containing that entry) or `disbursement_id`, and sets any of
`net_amount` (≥ 0), `description`, `vat_rate_id`. A draft may be created empty (then add manual
lines).

**Payment** (`PaymentRead`): `id, client, received_on, amount, method, reference, notes,
allocated_total, unallocated, state, voided_at, created_by, created_at, allocations[]{id,
invoice_id, invoice_number, amount, created_at}` (`allocations` is null in the list).

**Statement** (`StatementRead`): `client, currency, date_from, date_to, opening_balance,
entries[]{date, type (invoice|credit_note|payment), id, number, description, amount (signed:
invoices +, credit notes and payments −), balance}, invoiced, credited, paid, closing_balance,
unallocated_cash`.

## Error codes

| Status | Code | When |
|---|---|---|
| 404 | `RESOURCE_NOT_FOUND` | Unknown id or another practice's (client, item, invoice, payment, allocation, write-off, credit note). |
| 409 | `CLIENT_INACTIVE` | New disbursement for an inactive/archived client (from `app.services.clients.ensure_client_accepts_work`). |
| 409 | `DISBURSEMENT_LOCKED` | Changing/deleting a disbursement that is not `unbilled`. |
| 422 | `CLIENT_MISMATCH` | A WIP item, or an invoice in a payment allocation, belongs to another client. |
| 409 | `WIP_NOT_AVAILABLE` | Item not billable, not `unbilled`, or on an unapproved timesheet when approval is required (`details[].reason`: `not_billable`, `in_draft`, `billed`, `written_off`, `timesheet_not_approved`); a job with no WIP to write off. |
| 422 | `DUPLICATE_ITEM` | The same item twice in a write-off. |
| 409 | `WRITE_OFF_BILLED` | Reversing a write-off whose items moved on. |
| 422 | `OVERRIDE_NO_LINE` / `OVERRIDE_AMBIGUOUS` / `OVERRIDE_DUPLICATE` | An override matches no line / several lines / a line already overridden. |
| 422 | `NEGATIVE_WIP_LINE` | A line that bills WIP set below zero (use a manual discount line). |
| 422 | `INVOICE_TOTAL_NEGATIVE` | A change would take the invoice gross below zero. |
| 409 | `INVOICE_NOT_DRAFT` | Line/WIP changes, delete or issue on a non-draft. |
| 409 | `INVOICE_LOCKED` | Changing dates of an issued invoice, or anything on a void one. |
| 409 | `INVOICE_EMPTY` | Issuing a draft without lines. |
| 409 | `INVOICE_NOT_ISSUED` | Void/send/credit note/payment allocation on a draft or void invoice. |
| 409 | `INVOICE_HAS_PAYMENTS` / `INVOICE_HAS_CREDITS` | Voiding an invoice with payments / credit notes. |
| 422 | `NO_RECIPIENT` | Sending with no address given and none on the client. |
| 409 | `CREDIT_EXCEEDS_BALANCE` | Credit note gross > invoice balance (`details{gross_total, balance}`). |
| 409 | `ALLOCATION_EXCEEDS_BALANCE` | An allocation > the invoice balance. |
| 409 | `ALLOCATION_EXCEEDS_PAYMENT` | Allocations > the payment's unallocated amount. |
| 409 | `NOTHING_TO_ALLOCATE` | `auto_allocate` on an existing payment found nothing to do. |
| 409 | `PAYMENT_VOID` / `PAYMENT_ALLOCATED` | Allocating/voiding a void payment / voiding an allocated one. |
| 422 | `VALIDATION_ERROR` | Body/query validation (unknown fields, amounts ≤ 0, bad dates, …). |

## Locking (deadlock-free order)

Invoice row(s) in id order → WIP items (time entries in id order, then disbursements in id
order) → payment row → document sequence row. Every flow takes a subset in that order:
draft creation (items), add WIP / remove line / delete draft / issue / void (invoice → items;
issue also the `invoice` sequence), write-off (items), write-off reversal (write-off → items),
credit note (invoice → `credit_note` sequence), allocation (invoices → payment), unallocation
(invoice → payment), payment void (payment). Values are re-read under the locks
(`populate_existing`) before any check. Drafting or adding WIP that bills fixed-fee jobs also
takes a transaction-scoped advisory lock per fixed-fee job (job id order) after the item locks,
before reading what live fixed-fee lines already bill, so two drafts for different time of one
fixed-fee job cannot both bill the whole fee; only billing takes these locks. Tests race drafts
for the same time, drafts for one fixed-fee job, issues (gapless numbers), credit notes and
allocations from both sides.

## Decisions beyond docs/domain.md

* **Fixed-fee jobs**: time on a `fixed` job always becomes one `fixed_fee` line per job,
  whatever `group_by` says, at `fixed_fee` **less what live (draft or issued) fixed-fee lines
  for that job already bill**, never below 0, so later time on a billed fixed job is cleared
  at 0 (a 100% write-off) instead of billing the fee twice. Adding time of a fixed job to a
  draft that already has its fixed-fee line joins that line (its net is kept and
  re-apportioned).
* **Line maths for WIP lines**: time lines carry `quantity` = hours (2 dp), `unit_amount` = the
  average hourly rate, `net_amount` = the sum of the items' values. When a net is overridden
  the unit amount becomes `round(net / quantity)` so the line reads sensibly; the net is the
  amount billed. Lines that bill WIP cannot be negative (the allocation check `billed_value ≥ 0`
  would forbid it anyway). Apportionment runs over the line's allocations in id order.
* **Disbursement VAT**: the disbursement's `vat_rate_id` is the line's chosen rate; without it
  the general order applies (client default, then practice default — the job's service is not
  used for disbursements).
* **Credit note VAT**: line rate, else client default, else practice default.
* **Snapshots**: a line stores the resolved `vat_rate_id` and `vat_rate_bp`; later rate changes
  never alter it. Changing a line's net recomputes its VAT from the stored `vat_rate_bp`.
* **Draft totals** may never go below zero (the database forbids `gross_total < 0`), so a
  discount that would do so is refused at once (422 `INVOICE_TOTAL_NEGATIVE`).
* **Display status precedence**: `draft`, `void`, `paid` (balance 0, also when settled by
  credit notes), `overdue` (balance > 0, due date before today in the practice time zone),
  `part_paid` (some payment received), `issued`. Overdue wins over part paid.
* **Issue dates**: issue date = request, else the draft's, else today (practice time zone);
  due date = request, else the draft's if not before the issue date, else issue date + client
  terms, else practice terms.
* **Clients of any status** can be billed, credited and paid; only new disbursements check
  the client status.
* **Payments**: allocating twice from one payment to one invoice increases the existing
  allocation (the table holds one row per pair). `auto_allocate` pays the client's issued
  invoices with a balance by issue date, then number. On create, `auto_allocate` with nothing
  open simply leaves the money on account.
* **Statement**: void invoices and void payments are omitted entirely; payments reduce the
  balance by their full amount (allocated or not), so the closing balance is what is owed on
  invoices less cash on account; `unallocated_cash` reports that cash (current allocation
  state, for payments received up to `date_to`). Entries on one day: invoices, then credit
  notes, then payments. Without `date_from` the opening balance is 0.
* **Send**: one email to all recipients with the PDF attached, from the worker. Recipients: the
  addresses in `to`, else the client's `invoice_email`, else its billing contacts, else its
  primary contact, else the client email (422 `NO_RECIPIENT` when there is none). The enqueue is
  deduplicated per invoice while a job is live (`queued: false` on a repeat). `sent_at` is set
  by the job after the email went; the job skips when `sent_at` is already later than the
  request (idempotent re-delivery), and fails permanently for a voided/unknown invoice. No row
  lock is held while talking to SMTP.
* **Write-off**: only current WIP can be written off (same eligibility as billing). Reversal
  deletes the write-off and its allocations.
* **Disbursement reads** without `wip.read`: own only; someone else's by id is 403 (same
  practice), another practice's 404.
* **PDFs**: fpdf2 core fonts (Latin-1, which has `£`); text is normalised to Latin-1 (typographic
  dashes/quotes become ASCII, other characters `?`). Content streams are uncompressed so the
  text is searchable in tests.

## Billing v2 contract

Wave 2 (migrations `030`, `031`, `032`). Everything below is **additive** to the sections above except
the two changes marked **(changed)**. No permission code was added. Paths are under
`/api/v1/organizations/{organization_id}`; money is pence; dates are `YYYY-MM-DD` (practice time zone);
timestamps are UTC. `PersonRef` is `{user_id, full_name}`, `ClientRef` is `{id, code, name}`.

### 1. Invoice statuses (changed)

Stored `status`: `draft`, `awaiting_approval`, `approved`, `issued`, `void`. `display_status` is derived
(first match wins) and is what lists, tabs and badges use:

| `display_status` | When |
|---|---|
| `draft`, `awaiting_approval`, `approved`, `void` | the stored status |
| `credited` | issued and fully credited: `credited_total > 0` and `credited_total = gross_total` (nothing was paid) |
| `paid` | issued, `balance = 0` (payments, or payments plus credit notes) |
| `overdue` | issued, `balance > 0`, `due_date` before today |
| `part_paid` | issued, `balance > 0`, `paid_total > 0` |
| `issued` | issued, nothing paid |

A fully credited invoice used to display as `paid`; it is now `credited`. `GET /invoices?status=` takes all nine
(repeatable). `Invoice.balance` is still `gross - paid - credited` for issued invoices and 0 otherwise.

New fields on every invoice (`InvoiceSummaryRead`, also in the detail): `approval_required: bool` (the invoice needs
approval before it can be issued under the practice's current settings: a draft whose gross reaches the threshold, or
one that is awaiting approval or approved; false once issued or void). New fields on the **detail** only:
`created_by` (user id), `approvals[]` (history, oldest first, below) and `can_approve: bool` (the caller may approve
it right now: holds `invoice.approve`, the invoice is `awaiting_approval`, and the self-approval rule below allows it).

### 2. Invoice approval

Practice settings (`PATCH /organizations/{id}`, `organization.update`): `invoice_approval_required` (default false) and
`invoice_approval_threshold` (pence, default 0). **Approval is needed when it is on and `gross_total >= threshold`**
(0 = every invoice), judged on the current gross and the current settings.

Flow: `draft -> awaiting_approval -> approved -> issued`; `awaiting_approval` or `approved` go back to `draft` on
reject. A draft that needs no approval is issued directly as before.

| Method | Path | Permission | Body | Result |
|---|---|---|---|---|
| POST | `/invoices/{id}/submit` | `invoice.create` | optional `{note}` (≤ 500) | 200 `InvoiceRead`, `awaiting_approval` |
| POST | `/invoices/{id}/approve` | `invoice.approve` | optional `{note}` (≤ 500) | 200 `InvoiceRead`, `approved` |
| POST | `/invoices/{id}/reject` | `invoice.approve` | `{reason}` 1–500, **required** | 200 `InvoiceRead`, back to `draft` |
| POST | `/invoices/{id}/issue` | `invoice.issue` | unchanged | see below |

Rules:

* **Locked while waiting.** `awaiting_approval` and `approved` invoices cannot change: line, WIP and date edits and
  delete answer 409 `INVOICE_NOT_DRAFT`, the header `PATCH` 409 `INVOICE_NOT_EDITABLE`. Reject first to edit.
* **Segregation.** Whoever created the draft or submitted it cannot approve it (409 `SELF_APPROVAL_NOT_ALLOWED`),
  **except a practice Owner** (system role `owner`), so a sole owner is never locked out. Any holder of `invoice.approve`
  may reject, including the submitter (this recalls a submission).
* `approve` needs `awaiting_approval`; `reject` needs `awaiting_approval` or `approved` (an approval can be withdrawn
  before issue). Otherwise 409 `INVOICE_NOT_AWAITING_APPROVAL`.
* `submit` needs a draft with lines (409 `INVOICE_NOT_DRAFT`, `INVOICE_EMPTY`) and approval to be required for it
  (otherwise 409 `APPROVAL_NOT_REQUIRED`: issue it directly).
* **`issue`** works from `approved`, or from `draft` when approval is not needed. A draft that needs approval, and an
  `awaiting_approval` invoice, answer 409 `INVOICE_APPROVAL_REQUIRED` (`details: {status, gross_total, threshold}`). An
  approved invoice can be issued even if the settings changed after the approval.
* History rows (`invoice_approvals`): `approvals[]` = `{id, action: submitted|approved|rejected, actor: PersonRef,
  note, gross_total (at that moment), created_at}`; a rejection always carries its reason in `note`.
* Notifications (in-app, email per the practice's switches): `invoice_approval_pending` to every active member holding
  `invoice.approve` except the submitter, on submit; `invoice_approved` and `invoice_rejected` to the people who
  created and submitted the invoice (never the decider). The link is `/billing/invoices/{id}`.
* Audit: `invoice.submitted`, `invoice.approved`, `invoice.rejected` (target `invoice`; meta has the client, gross
  total and the note or reason). The caller's IP is recorded like every audit row.

### 3. Issued invoices cannot be edited (changed)

`PATCH /invoices/{id}` on **any non-draft invoice** (`awaiting_approval`, `approved`, `issued`, `void`) answers 409
`INVOICE_NOT_EDITABLE`, whatever the body, including `notes` and `reference` (these used to stay editable once issued;
`INVOICE_LOCKED` is no longer returned). Only drafts take `issue_date`, `due_date`, `reference`, `notes`.

### 4. List summary for the status tabs

`GET /invoices/summary` (`invoice.read`; filters `client_id`, `date_from`, `date_to`, `q` as in the list, no `status`):

```json
{
  "counts": {"draft": 2, "awaiting_approval": 1, "approved": 0, "issued": 4, "part_paid": 1, "paid": 7,
             "credited": 1, "overdue": 3, "void": 1, "total": 20},
  "outstanding": 1250000,
  "overdue": 480000,
  "awaiting_approval_gross": 120000
}
```

`counts` follow `display_status` (each invoice counted once), `total` is the number of invoices matching the filters,
`outstanding` the sum of balances of issued invoices, `overdue` the sum of the balances of the overdue ones.

### 5. Invoice templates in PDF and email

The invoice PDF (`GET /invoices/{id}/pdf`, the `send` email attachment, the `send_invoice_email` worker) uses the
invoice template of **the client** (`clients.invoice_template_id`, even when it is inactive), else **the practice
default**. `GET /invoices/{id}/pdf?template_id=<id>` previews another template (permission `invoice.read`; unknown,
deleted or another practice's template: 404; an inactive one: 404 unless the caller has `settings.manage`).

| Setting | Effect |
|---|---|
| `layout = standard` | As before: one row per invoice line (description, quantity, rate, VAT, amount). |
| `layout = detailed` | The standard rows, and under every time, fixed-fee and disbursement line one indented row per item it bills: date, person, narrative, hours (`h:mm`) and the item's billed amount. |
| `layout = summary` | One row per job: lines of the same job and VAT rate merge into a row (job name, VAT, amount; no quantity or rate); lines without a job (manual lines) stay as they are. |
| `show_logo` | The practice has no logo image yet, so this switches the **letterhead block** (practice name as the heading, address, phone, email). Off: one small "From" line with the name and the VAT and company numbers, which always stay. |
| `show_bank_details` | Prints the "How to pay" block (practice bank details). |
| `footer_text` | Printed at the bottom; null falls back to the practice `invoice_footer` (the previous behaviour). |

Credit-note PDFs use `show_logo` and the footer; the statement PDF uses `show_logo`, `show_bank_details` and the
footer (the statement used to have none). The invoice email's `bank_details` value is empty when the template hides the
bank details, and the template footer is added at the end of the built-in email.

### 6. Payment reversal

`POST /payments/{id}/reverse` — `payment.record`, body `{reason (1–500, required), bounced: bool = false}`. In **one
transaction** it deletes every allocation of the payment, lowers each invoice's `paid_total` (their balances and
display statuses follow), sets `allocated_total = 0` and marks the payment reversed. Returns the `PaymentRead`
(`allocations: []`).

* `PaymentRead` gains `reversed_at`, `reversed_by` (user id), `reversal_reason`, `bounced`; `state` gains **`reversed`**.
  A reversed payment also has `voided_at` set (every "live payment" rule treats it as void; `state` says which).
  `GET /payments?state=void` lists voided payments that are **not** reversed; `state=reversed` lists the reversed ones.
* 409 `PAYMENT_REVERSED` (already reversed), 409 `PAYMENT_VOID` (void), 422 missing reason, 404, 403.
* The existing `void` (only when unallocated) and unallocate endpoints stay. `auto_allocate` is still oldest invoice
  first (issue date, number).
* Audit `payment.reversed` (meta: reason, `bounced`, `allocations_removed[{invoice_id, invoice_number, amount,
  invoice_balance}]`). Notification `payment_reversed` to every active member holding `payment.record` except the
  person who reversed it; link `/billing/payments`.
* The client statement shows a reversed payment as the payment (dated when received) **and** a new entry of type
  `payment_reversal` (+ the amount, dated the practice-local day of the reversal, never before the receipt).

### 7. Debtors

Permission: any of `payment.read`, `invoice.read`, `report.financial`. Balances are the invoice balances **as at**
the date (gross less payments received and credit notes issued by then, void invoices left out: the same figures as the
Aged Debtors report). Days past due = `as_at - due_date`; buckets **Current** (not past due, `<= 0`), **1–30**,
**31–60**, **61–90**, **90+** (91 days or more).

`GET /debtors` — query: `as_at` (default today; a future date is 422), `client_id`, `partner_user_id`,
`manager_user_id` (the client's), `min_balance` (pence, clients whose total is at least this), `sort` =
`client` (default) | `total` | `current` | `days_1_30` | `days_31_60` | `days_61_90` | `days_90_plus` | `oldest_due`,
`order` = `asc` | `desc` (default `asc` for `client`, else `desc`), `limit`, `cursor` (`meta.next_cursor`; a cursor of
another sort or filter is 422). Only clients that owe something are listed.

```json
{
  "as_at": "2026-10-18", "currency": "GBP",
  "totals": {"current": 100000, "days_1_30": 50000, "days_31_60": 0, "days_61_90": 0, "days_90_plus": 12000,
             "total": 162000, "overdue": 62000, "client_count": 3, "invoice_count": 5},
  "rows": [{
    "client": {"id": "…", "code": "WID001", "name": "Widgets Ltd"},
    "partner_user_id": null, "manager_user_id": null,
    "current": 100000, "days_1_30": 50000, "days_31_60": 0, "days_61_90": 0, "days_90_plus": 12000,
    "total": 162000, "overdue": 62000, "invoice_count": 4,
    "oldest_due_date": "2026-06-30", "max_days_overdue": 110,
    "last_reminder_at": "2026-10-10T09:00:00Z"
  }]
}
```

`totals` cover **every matching client**, not only the page; `overdue = total - current`; `last_reminder_at` is the
latest reminder (queued or sent) for any of the client's invoices.

`GET /debtors/{client_id}/invoices?as_at=` — the client's open invoices (bounded list, oldest due first); 404 for an
unknown or another practice's client:

```json
{
  "client": {"id": "…", "code": "WID001", "name": "Widgets Ltd"}, "as_at": "2026-10-18",
  "totals": {"current": 0, "days_1_30": 0, "days_31_60": 0, "days_61_90": 0, "days_90_plus": 0, "total": 0},
  "invoices": [{
    "id": "…", "number": "INV-00012", "source": null, "issue_date": "2026-09-01", "due_date": "2026-10-01",
    "days_overdue": 17, "bucket": "days_1_30", "gross_total": 144000, "paid": 0, "credited": 0, "outstanding": 144000,
    "display_status": "overdue", "last_reminder_at": null, "reminder_count": 0
  }]
}
```

`source` is `null` until the opening-balances import marks opening invoices (`"opening"`). `days_overdue` is 0 when not
yet due; `bucket` is one of `current`, `days_1_30`, `days_31_60`, `days_61_90`, `days_90_plus`.

### 8. Payment reminders

Settings (`organization.update`): `payment_reminders_enabled` (default **false**) and `payment_reminder_days_after_due`
(1–365, default **7**) on the practice (`OrganizationRead`/`OrganizationUpdate`).

`POST /debtors/reminders` — `payment.record`. Body: **exactly one** of `client_ids` (1–200: every overdue invoice of
each client) or `invoice_ids` (1–500), `template` (`client_payment_reminder` default | `client_payment_final_notice`),
`dry_run: bool = false`. An unknown id or another practice's is 404. **One email per client** lists that client's
overdue invoices; it goes to the client's billing recipients (the same order as invoices: `invoice_email`, else billing
contacts, else the primary contact, else the client email) through the messaging gateway (the recording mock when
no SMTP is configured), queued as a `send_payment_reminder_email` job in the same transaction. Each invoice gets a
`payment_reminders` row. **Per invoice and day** (practice time zone) at most one reminder is queued or sent.

```json
{
  "dry_run": false, "template": "client_payment_reminder", "emails_queued": 1, "invoices_queued": 2,
  "clients": [{
    "client": {"id": "…", "code": "WID001", "name": "Widgets Ltd"}, "to": ["bills@widgets.example"],
    "subject": "Payment reminder from Acme Accountants: 2 overdue invoices",
    "text": "Dear Bill Payer, …", "total_due": 264000,
    "invoices": [{"invoice_id": "…", "number": "INV-00012", "due_date": "2026-10-01", "days_overdue": 17, "outstanding": 144000}]
  }],
  "skipped": [{"invoice_id": "…", "number": "INV-00009", "client_id": "…", "reason": "already_reminded_today"}]
}
```

`skipped[].reason`: `already_reminded_today`, `not_overdue`, `no_balance`, `not_issued`, `no_recipient`. With
`dry_run: true` nothing is written or queued (status 200; a real run answers 202) and `clients` is what would go out.

`GET /debtors/reminders` — history, newest first, cursor-paginated; filters `client_id`, `invoice_id`, `status`
(`queued`, `sent`, `failed`, `skipped`), `source` (`manual`, `scheduled`), `date_from`, `date_to` (the day it was sent):

```json
{"id": "…", "client": {"id": "…", "code": "WID001", "name": "Widgets Ltd"},
 "invoice": {"id": "…", "number": "INV-00012"}, "template": "client_payment_reminder",
 "source": "manual", "status": "sent", "sent_on": "2026-10-18", "to": ["bills@widgets.example"],
 "days_overdue": 17, "amount_due": 144000, "sent_by": {"user_id": "…", "full_name": "Pat Partner"},
 "sent_at": "2026-10-18T09:00:03Z", "error": null, "created_at": "2026-10-18T09:00:00Z"}
```

`sent_by` is null for the scheduler. **Scheduled reminders**: the daily worker job `send_payment_reminders` (once per
practice per day) queues one reminder for every overdue invoice of a practice with `payment_reminders_enabled` that is at
least `payment_reminder_days_after_due` days past due and has **never** been reminded (queued or sent) before.

### 9. Statements

`POST /clients/{client_id}/statement/send` — needs `payment.record`, `invoice.read` and `payment.read`. Body (all
optional) `{date_from, date_to, to: [email] (1–10)}`; `to` defaults to the client's billing recipients (422
`NO_RECIPIENT` when none). Queues a `send_statement_email` job that renders the statement PDF (template footer and
bank details as in section 5) and emails it as an attachment. 202:

```json
{"id": "…", "client_id": "…", "to": ["bills@widgets.example"], "date_from": null, "date_to": "2026-10-18",
 "closing_balance": 264000, "queued": true}
```

Logged in `statement_sends` and audited as `statement.send_queued`. A second request for the same client while one is
queued (the last ten minutes) is not queued again (`queued: false`, the existing `id`).

`GET /statements` — permission `invoice.read` and `payment.read`. The clients that owe something or hold cash on
account. Query: `client_id`, `partner_user_id`, `manager_user_id`, `min_balance`, `overdue_only` (bool), `never_sent`
(bool), `q` (client name or code), `sort` = `client` (default) | `balance` | `overdue` | `last_sent`, `order`, `limit`,
`cursor`.

```json
{
  "totals": {"client_count": 3, "outstanding": 500000, "overdue": 120000, "on_account": 20000, "balance": 480000},
  "rows": [{
    "client": {"id": "…", "code": "WID001", "name": "Widgets Ltd"}, "partner_user_id": null, "manager_user_id": null,
    "outstanding": 264000, "on_account": 0, "balance": 264000, "overdue": 144000,
    "oldest_overdue_due_date": "2026-10-01", "oldest_overdue_days": 17,
    "last_statement_sent_at": "2026-09-30T08:00:00Z", "last_statement_to": ["bills@widgets.example"],
    "has_recipient": true
  }]
}
```

`outstanding` = what is owed on issued invoices, `on_account` = unallocated cash, `balance = outstanding - on_account`
(the statement's closing balance), `overdue` = the part of `outstanding` past its due date; `meta.next_cursor` pages.

### 10. New error codes

| Status | Code | When |
|---|---|---|
| 409 | `INVOICE_NOT_EDITABLE` | `PATCH` of a non-draft invoice |
| 409 | `INVOICE_APPROVAL_REQUIRED` | Issuing a draft that needs approval, or one awaiting approval |
| 409 | `APPROVAL_NOT_REQUIRED` | Submitting an invoice that needs no approval |
| 409 | `INVOICE_NOT_AWAITING_APPROVAL` | Approve/reject in the wrong status |
| 409 | `SELF_APPROVAL_NOT_ALLOWED` | The creator or submitter approving (an Owner may) |
| 409 | `PAYMENT_REVERSED` | Reversing, allocating or voiding a reversed payment |
| 422 | `NO_RECIPIENT` | A statement with no address given and none on the client |
| 422 | `VALIDATION_ERROR` | Missing reject/reversal reason, both or neither of `client_ids`/`invoice_ids`, a future `as_at`, … |

### 11. Notification types and templates added

Types: `invoice_approval_pending`, `invoice_approved`, `invoice_rejected`, `payment_reversed` (in-app and email by
default). Client-facing email templates (category `system`, customisable under Settings → Email templates):
`client_payment_reminder`, `client_payment_final_notice`, `client_statement`.

### 12. For the other modules

* Reversed payments keep `voided_at`, so `voided_at IS NULL` still means "live"; `payments.reversed_at` /
  `bounced` tell the reversals apart (the Payments report can split them).
* Invoice `status` has three more stored values: anything that meant "not issued yet" by `status = 'draft'` should
  also consider `awaiting_approval` and `approved`.

## Estimates and recurring invoices

Both sit next to invoices and never touch WIP. Permissions: `estimate.read` / `estimate.manage` and
`recurring.read` / `recurring.manage` (all in the catalogue and the default roles since migration 019). Tables from
migration 046: `estimates`, `estimate_lines`, `recurring_schedules`, `recurring_lines`, `recurring_runs`.

### Estimates

| Method | Path | Notes |
|---|---|---|
| GET | `/estimates` | filters `client_id`, `status` (repeat: `draft`, `sent`, `accepted`, `declined`, `expired`), `q` (number, reference, client); cursor-paginated, newest first |
| GET | `/estimates/summary` | counts per display status and `open_gross` (value of estimates sent and still open); the status filter is ignored |
| POST | `/estimates` | creates a **draft** with 1–200 lines; priced like invoice lines (net = round(qty × unit), VAT from the chosen rate, else service, client, practice default; 0 when not VAT-registered) |
| GET / PATCH / DELETE | `/estimates/{id}` | PATCH and DELETE only on a draft (409 `ESTIMATE_NOT_DRAFT`); PATCH `lines` replaces every line |
| POST | `/estimates/{id}/send` | takes the next `estimate` sequence number (`EST-00001`), sets `issue_date` (default today), keeps or sets `expiry_date`, status `sent` (409 `ESTIMATE_EMPTY`). **Email delivery is not wired yet** |
| POST | `/estimates/{id}/accept`, `/decline` | only a `sent` estimate (409 `ESTIMATE_NOT_SENT`); an expired one can still be answered |
| POST | `/estimates/{id}/convert` | needs `invoice.create` as well; copies the lines of a `sent` or `accepted` estimate into a new **draft** invoice (the estimate's job fills lines that have none) and stores `converted_invoice_id` (409 `ESTIMATE_NOT_CONVERTIBLE`, `ESTIMATE_ALREADY_CONVERTED`) |

Stored status is `draft → sent → accepted | declined`. `display_status` adds `expired`: a `sent` estimate whose
`expiry_date` is before today (practice time zone); it is derived, never stored. A numbered estimate cannot be deleted.
Audit actions: `estimate.created|updated|sent|accepted|declined|converted|deleted`.

### Recurring invoices

| Method | Path | Notes |
|---|---|---|
| GET | `/recurring` | filters `client_id`, `status` (repeat: `active`, `paused`, `ended`), `q` (name, client) |
| POST | `/recurring` | `{client_id, name, frequency: weekly\|monthly\|quarterly\|annually, start_date, end_date?, reference?, notes?, lines}`; the lines are priced once at creation so a bad job, service or VAT rate is refused at once |
| GET / PATCH / DELETE | `/recurring/{id}` | PATCH: name, frequency, end date, reference, notes, `lines` (replaces all); 409 `SCHEDULE_ENDED`. DELETE only when it never ran (409 `SCHEDULE_HAS_RUNS`) |
| POST | `/recurring/{id}/pause`, `/resume` | 409 `SCHEDULE_NOT_ACTIVE` / `SCHEDULE_NOT_PAUSED`; resuming skips the dates missed while paused |
| GET | `/recurring/{id}/runs` | newest run date first; each run has `status` (`succeeded` / `failed`), `invoice_id`, `invoice_number` (null while a draft), `error`, `attempts` |
| POST | `/recurring/runs/{run_id}/retry` | tries a failed run again; answers 200 with the run even if it fails again (409 `RUN_NOT_FAILED`) |

Dates are counted **from the start date** (31 January → 28 February → 31 March). The `run_recurring_invoices` job runs
daily from the worker (`PERIODIC`): for every practice, each active schedule whose `next_run_date` is today or earlier
gets one run row (unique per schedule and date) and a **draft** invoice of manual lines, priced again that day (a VAT
change applies to the next invoice). It catches up at most 12 missed dates per day, never issues anything, and leaves a
failed date alone until someone retries it. The draft's `created_by` is the schedule's creator. Audit actions:
`recurring.created|updated|paused|resumed|deleted|run|run_failed`.

Not built: auto-issuing and emailing the recurring invoice, recurring jobs from `client_services.frequency`, estimate
PDF and email, converting an estimate to a job.
