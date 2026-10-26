# From WIP to cash: how billing works

This is the life of a piece of chargeable work in Capium Time & Fees, from the moment it
is recorded to the moment the client has paid for it, and how every figure reconciles to
the penny. The rules are in [`domain.md`](domain.md); the endpoints in
[`modules/billing.md`](modules/billing.md). The numbers below are exactly those of
`tests/test_invoices_reconciliation.py::test_full_reconciliation`.

## The ledgers

| Ledger | Rows | Changes it |
|---|---|---|
| **WIP** | `time_entries` and `disbursements` with `billing_status = 'unbilled'` (billable; time on an approved timesheet when approval is required) | logging time / disbursements |
| **WIP allocations** | `wip_allocations`: where each item went (an invoice line or a write-off), with its `wip_value` and `billed_value` | drafting, write-offs, void |
| **Invoices** | `invoices` + `invoice_lines`; totals on the invoice row | drafting, issue, void |
| **Settlement** | `payments`, `payment_allocations`, `credit_notes`; `invoices.paid_total`, `invoices.credited_total` | payments, credit notes |

An item's status moves `unbilled → in_draft → billed`, or `unbilled → written_off`. Deleting
a draft (or one of its lines), voiding an invoice and reversing a write-off move it back to
`unbilled`. A credit note never does.

## 1. Work in progress

Widgets Ltd's year-end accounts job collects:

| Item | Minutes | Rate (£/h) | WIP value |
|---|---|---|---|
| Partner time | 120 | 150.00 | £300.00 |
| Junior time | 90 | 90.00 | £135.00 |
| Junior time | 45 | 60.00 | £45.00 |
| Companies House fee (disbursement) | | | £13.00 |
| | | | **£493.00** |

(`charge_value = round(minutes × rate / 60)`, half up, snapshotted on the entry when it is
saved.) `GET /wip` shows the client with `time_value` 48,000, `disbursement_value` 1,300 and
the age buckets by item date.

### The billing summary and the billing basis

`GET /clients/{client_id}/billing-summary` (`wip.read`) shows, for each job of the client and for the
work without a job, what was spent and what it is worth, so the hours are visible even when nothing can
be billed yet (a fixed fee job whose fee is not recognised, non-billable time):

- **Terms**: the contract terms in words ("Fixed fee of £2,000.00", "Hourly at the rates agreed with the
  client or the practice's own rates").
- **Hours**: `logged`, `billable`, `non_billable`, `billed`, `ready` (billable, unbilled and approved when
  the practice requires it) and `awaiting_approval`.
- **Value by source**: `company_time_value` (unbilled billable time at the practice's own rates:
  `rate_source` staff, grade, service_grade, default) and `contract_time_value` (at rates agreed with
  the client: `rate_source` client_*, or the job's own rate), the `fixed_fee` and `fixed_fee_billed`, and
  the `other_costs` (disbursements ready to bill).
- **Budget against actual**, three separate figures: `actual_cost` (the cost of the people who did the
  work; needs `rate.read`), `budgeted_cost` (the job's `budget_cost`, the cost expected at the service level
  it is sold at) and `agreed_price` (the fixed fee, else the fee budget).
- **`basis_values`**, what billing the ready work would come to on each **billing basis**: `contract`
  (the time at the rates in force, or the fixed fee still to bill, plus disbursements), `job` (what is left
  of the value set for the job, plus disbursements) and `team` (the cost of the team members who did the
  work, plus disbursements; needs `rate.read`). `time_costs` lists the cost of each ready entry so the
  invoice can be valued on the team basis.

The Prepare Invoice page offers the three bases; the chosen one sets the default amount of each line
(`overrides` of `POST /invoices`), and a line edited by hand keeps the amount typed. The difference to the
WIP value is the write-on or write-off, as ever.

## 2. The draft

The partner bills the time at **£450.00** instead of £480.00:

```http
POST /invoices
{ "client_id": "...", "time_entry_ids": [t1, t2, t3], "disbursement_ids": [fee],
  "group_by": "job",
  "overrides": [{ "job_id": "<accounts job>", "net_amount": 45000 }] }
```

Two lines are built:

| Line | Kind | Qty | Unit | Net | VAT 20% | Gross |
|---|---|---|---|---|---|---|
| Accounts 2026 (7 Sep 2026) | time | 4.25 | £105.88 | £450.00 | £90.00 | £540.00 |
| Companies House filing fee | disbursement | 1.00 | £13.00 | £13.00 | £2.60 | £15.60 |
| **Invoice** | | | | **£463.00** | **£92.60** | **£555.60** |

* `quantity` = 255 minutes = 4.25 hours; the unit amount is the net over the hours.
* VAT is computed **per line** (`round(net × rate_bp / 10000)`, half up) and the invoice
  totals are the sums of the lines; `gross = net + vat` is also a database check.
* The VAT rate of a line is its own rate, else its service's, else the client's default,
  else the practice default; 0 for a practice that is not VAT-registered. The pair
  `(vat_rate_id, vat_rate_bp)` is snapshotted on the line.

**Apportionment.** The line's £450.00 is spread over its three items in proportion to their
WIP values with the largest-remainder method (`money.apportion`), so the parts add up to
exactly the line net:

| Item | WIP | Exact share | Billed | Write-off |
|---|---|---|---|---|
| Partner time | 30,000 | 28,125.00 | 28,125 | 1,875 |
| Junior time | 13,500 | 12,656.25 | 12,656 | 844 |
| Junior time | 4,500 | 4,218.75 | **4,219** | 281 |
| | 48,000 | | **45,000** | **3,000** |

The floors add up to 44,999; the one penny left goes to the largest remainder (0.75). Each
item now has a `wip_allocations` row (`wip_value`, `billed_value`) and is `in_draft`, so no
other draft or write-off can take it: concurrent drafts for the same time lock the items and
all but one get 409 `WIP_NOT_AVAILABLE`. The invoice detail shows per line
`wip_value 48,000`, `billed_value 45,000`, `adjustment −3,000`.

Changing a WIP line's net later re-apportions it; deleting the line or the draft deletes the
allocations and returns the items to `unbilled`.

## 3. Issue

```http
POST /invoices/{id}/issue   { "issue_date": "2026-10-01" }
```

The invoice takes the next number of the `invoice` sequence (`INV-00001`) under the
sequence row lock, inside the issuing transaction: two concurrent issues queue on it, and a
failed issue rolls its number back, so numbers are gapless. The due date is the issue date
plus the client's payment terms (else the practice's, here 30 days: 31 Oct 2026). The items
become `billed`. From now on the invoice is immutable except `notes` and `reference`.

Balance = gross − paid − credited = **£555.60**.

## 4. Part payment

```http
POST /payments  { "client_id": "...", "received_on": "2026-10-10", "amount": 20000,
                  "allocations": [{ "invoice_id": "<INV-00001>", "amount": 20000 }] }
```

Under the invoice row lock (then the payment's): the allocation may not exceed the invoice
balance (409 `ALLOCATION_EXCEEDS_BALANCE`) and the allocations together not the payment
(409 `ALLOCATION_EXCEEDS_PAYMENT`). `paid_total` becomes 20,000: balance **£355.60**, display
status `part_paid` (`overdue` once the due date has passed).

## 5. Credit note

```http
POST /invoices/{id}/credit-notes
{ "reason": "Goodwill", "issue_date": "2026-10-12",
  "lines": [{ "description": "Goodwill reduction", "net_amount": 2000 }] }
```

£20.00 + £4.00 VAT = £24.00, numbered `CN-00001`, at most the outstanding balance (409
`CREDIT_EXCEEDS_BALANCE`). `credited_total` becomes 2,400: balance **£331.60**. The WIP
allocations are untouched: the credit is a reduction of the bill, not unbilled work.

## 6. Final payment

The client pays £331.60 (plus another invoice, INV-00002, for £144.00) in one sum with
`auto_allocate: true`. Auto-allocation pays the client's open invoices oldest first (issue
date, then number): £331.60 to INV-00001 and £144.00 to INV-00002. Both are `paid`.

## 7. Reconciliation

For INV-00001:

```
gross 55,560 = paid 20,000 + 33,160  +  credited 2,400  +  balance 0
net   46,300 = billed value of its WIP allocations (45,000 + 1,300)
WIP   49,300 − billed 46,300 = 3,000 written off on this bill
```

And for the client, the statement (`GET /clients/{id}/statement`) lists invoices (+),
credit notes (−) and payments (−) in date order with a running balance:

```
invoiced 69,960 (55,560 + 14,400) = paid 67,560 + credited 2,400;  closing balance 0;
cash on account 0
```

The closing balance is always Σ invoice balances − cash on account (money received but not
allocated), because a payment reduces the balance by its full amount when it is received.

## The other ways out of WIP

* **Void** (`POST /invoices/{id}/void`, only without payments or credit notes): the invoice
  keeps its number and becomes `void`; its allocations get `reversed_at` (kept for the audit
  trail) and the items return to `unbilled` at their original WIP values, ready to be billed
  again on a new invoice with the next number.
* **Write-off without an invoice** (`POST /wip/write-offs`): items get allocations with
  `billed_value = 0` and become `written_off`; `DELETE /wip/write-offs/{id}` reverses it while
  none of them has moved on.

## What the database guarantees on top

`gross_total = net_total + vat_total`, `gross_total ≥ 0`, `paid_total + credited_total ≤
gross_total`, a void invoice has no payments, `allocated_total ≤ amount` on a payment, at
most one live allocation per WIP item (partial unique indexes), a write-off allocation bills
0, and composite foreign keys keep every reference inside one practice (and payment and
invoice of an allocation on the same client).
