# Work in progress: items, ledger, adjustments, transfers, credit notes and historic WIP

Code: `app/api/v1/{wip,wip_views,wip_adjustments,wip_transfers,credit_note_wip,wip_reports}.py` → `app/services/{wip,wip_engine,wip_adjustments,wip_transfers,credit_note_wip,wip_history}.py`
→ `app/repositories/{wip,wip_atoms,wip_ledger,wip_history}.py`; schemas `app/schemas/{billing,wip}.py`; models `app/db/models/{billing,wip}.py`.
Migrations: `027` (`wip_allocations.expense_id`), `028` (`wip_movements`, `wip_adjustments`, `wip_transfers`, `credit_note_wip`), `029` (`wip_snapshots`, `wip_snapshot_items`).

All paths are under `/api/v1/organizations/{organization_id}`. Another practice's ids answer 404, never 403. Money is integer pence, dates are ISO days in the practice's time zone.

---

# Contract for billing

Published first on purpose: invoicing (draft, issue, void), the billing UI and the later opening-WIP import read WIP through exactly what is written here. Everything below exists in the tree.

## 1. What WIP is: the item kinds

A **WIP item** is something the practice has done or paid for that has not been billed yet. There are four kinds (a fifth, `opening`, will follow the same pattern, see section 9):

| `kind` | Source row | `id` | Value (pence) | Date | Job |
|---|---|---|---|---|---|
| `time` | `time_entries`: billable, has a client, `billing_status = 'unbilled'`, on an **approved** timesheet when the practice requires approval | the time entry id | `charge_value` | `entry_date` | `job_id` |
| `disbursement` | `disbursements`: billable, `billing_status = 'unbilled'` | the disbursement id | `amount` | `incurred_on` | `job_id` |
| `expense` | `expenses` (a line of an expense claim): the claim is `approved`, the line is billable, has a client, `billing_status = 'unbilled'` and `recharge_amount > 0` (**the rule of `wip_eligible_expenses` / `wip_expense_conditions`** in `app/repositories/expense_lines.py`; WIP never re-implements it) | the expense line id | `recharge_amount` | `incurred_on` | `job_id` |
| `fixed_fee` | derived, one per fixed-fee job (section 3) | the **job id** | recognised fixed fee minus what is already billed | the day the fee was recognised | the job |

**Total WIP = Time WIP + Fixed fee WIP + Expense WIP.** Disbursements count in *Expense WIP* (a disbursement is an expense the practice paid for a client); `disbursement_value` keeps meaning "disbursements only" and `expense_value` is the expense claim lines only. Time on a **fixed-fee job** counts in *Fixed fee WIP* (section 3), every other time in *Time WIP*.

`in_draft` items are not WIP (they are on a draft invoice); `billed` and `written_off` items are gone from WIP.

## 2. Reading the items (REST)

| Method | Path | Permission | Use |
|---|---|---|---|
| GET | `/clients/{client_id}/wip` | `wip.read` | **The list for Prepare Invoice**: every item of one client, bounded (not paginated), oldest first; `?job_id=` narrows it. The existing path, extended: `items` now also holds `expense` and `fixed_fee` items; `totals` (unchanged keys) and the new `composition` give the figures |
| GET | `/wip/items` | `wip.read` | Items across clients, cursor-paginated, filters `client_id`, `job_id`, `kind`, `partner_user_id`, `manager_user_id`, `service_id`, `min_age_days` (the WIP Details page) |

An item of `GET /clients/{client_id}/wip` (the legacy keys are kept, the new ones are additive):

```json
{
  "kind": "time", "id": "…", "date": "2026-09-07", "age_days": 41,
  "job": {"id": "…", "number": "J00042", "name": "Accounts 2026"},
  "person": {"user_id": "…", "full_name": "Grace Hopper"},
  "description": "Bank reconciliation", "minutes": 90, "hours": "1.50", "rate": 12000,
  "value": 18000, "charge_value": 18000, "component": "time", "selectable": true,
  "activity_code_id": null, "supplier": null, "claim_id": null, "fixed_fee": null
}
```

* `value` is what counts in the totals, `charge_value` (time only) what the time is worth at its charge-out rate: they differ only for time on a fixed-fee job whose fee is already recognised (value `0`, see section 3).
* `selectable` says whether billing may take **this item by its id** (`time`, `disbursement`, `expense`: yes; `fixed_fee`, `adjustment`: no).
* `person`: the person who did the work (time), the claimant (expense), the person who recorded it (disbursement).
* `fixed_fee` (kind `fixed_fee` only): `{fee, recognised, billed, mode, progress_bp, time_entry_ids}`.
* `kind = adjustment` rows (a write-on or an amount write-off that is still in force) appear in `/wip/items` only, never in the Prepare Invoice list; they are not billable by themselves (section 7).

## 3. Fixed fees

A job with `billing_basis = 'fixed'` has a `fixed_fee`. How much of it counts as WIP follows the practice setting `fixed_fee_wip_mode` (`docs/domain.md`):

* `on_completion`: the unbilled fee counts once the job's status is `completed` or `closed`;
* `by_budget_progress`: `fee × min(1, logged minutes ÷ budget minutes)`, rounded half up, minus what is billed; a job without `budget_minutes` has no measurable progress and is treated as not yet recognised.

`billed` is the net of the job's **live** `fixed_fee` invoice lines (draft or issued, not void: `WipRepository.fixed_fee_billed`). The `fixed_fee` item's `value` is `max(0, recognised − billed)`.

* While the fee is **not recognised**, the job's unbilled time counts at its charge value, as a `time` item with `component = "fixed_fee"`.
* Once **recognised**, the fee replaces that time: the job's time items stay in the list (billing needs them) with `value = 0` and `charge_value` as recorded, and the `fixed_fee` item carries the money.

**How billing bills a fixed fee** (nothing changes in the invoice flow): select the job's unbilled **time entries** (the `fixed_fee` item lists them in `fixed_fee.time_entry_ids`); `plan_lines` produces the `fixed_fee` line (`net = fee − already billed`, overridable) and allocates the entries as today. A fixed fee that is recognised while the job has **no** unbilled time left cannot be billed through time entries; billing may offer a `fixed_fee_job_ids` selection that creates the `fixed_fee` line (`job_id`, `net_amount ≤ item.value`) **without allocation rows**: what is billed of a fixed fee is always derived from the live `fixed_fee` lines, never from allocations.

## 4. Taking items out of WIP (Python, `app/services/wip.py`)

```python
async def lock_wip_items(
    session, organization, client,
    time_entry_ids, disbursement_ids, expense_ids=(),   # expense_ids is new and optional
) -> LockedItems: ...

@dataclass
class LockedItems:
    time_entries: list[TimeEntry]; disbursements: list[Disbursement]; expenses: list[Expense]
    wip_value: int   # time charge_value + disbursement amount + expense recharge_amount
    count: int

async def lock_items(session, organization_id, time_entry_ids, disbursement_ids, expense_ids=()) -> LockedItems
async def lock_allocation_items(session, organization_id, allocations) -> LockedItems   # for issue / void / release
def set_items_status(items, status)   # 'in_draft' | 'billed' | 'unbilled' | 'written_off' on all three kinds
```

* Lock order is **invoice row → time entries → disbursements → expense lines, each in id order**: every flow uses it, so none can deadlock.
* `lock_wip_items` answers **404** for an id of another practice, **422 `CLIENT_MISMATCH`** (`details {items: [ids]}`) for an item of another client and **409 `WIP_NOT_AVAILABLE`** with `details: [{"time_entry_id" | "disbursement_id" | "expense_id": id, "reason": …}]` when an item is no longer WIP. Reasons: `not_billable`, the item's `billing_status` (`in_draft`, `billed`, `written_off`), `timesheet_not_approved`, `claim_not_approved`, `nothing_to_recharge`.
* `set_items_status` sets `Expense.billing_status` too (the same four values as time and disbursements); a non-billable item can never leave `unbilled` (database CHECK).
* `lock_allocation_items(session, org_id, allocations)` locks whatever the allocations point at, whatever its kind: use it in `issue`, `void`, line removal and draft deletion instead of listing the ids by hand.

## 5. Allocation rows: how an item is marked billed (`wip_allocations`)

One row per item and destination; billing writes them when a draft is built, exactly as for time and disbursements:

| Column | Meaning |
|---|---|
| `time_entry_id` / `disbursement_id` / **`expense_id`** | **Exactly one is set** (`CHECK num_nonnulls(...) = 1`). `expense_id` is a composite FK `(organization_id, expense_id) → expenses`. |
| `invoice_id`, `invoice_line_id` | The invoice and line that bill the item (both or neither). A write-off has neither (`write_off_id` instead): **exactly one destination** (`invoice_line_id` or `write_off_id`). |
| `wip_value` | The item's WIP value when it was allocated: `charge_value` / `amount` / **`recharge_amount`**. |
| `billed_value` | What the line bills for the item (`money.apportion` of the line net over the line's allocations by `wip_value`, in id order). `billed_value − wip_value` is the item's write-up (+) or write-off (−). |
| `reversed_at` | Null while the allocation is live. Set when it was released but must be kept (void, credit-note return). |

The partial unique indexes `uq_wip_allocations_{time_entry,disbursement,expense}_live` allow **one live allocation per item**; a violation is 409/422 on `items` through the generic handler (`This expense is already being billed.`).

Status life cycle of an item (all three kinds): `unbilled → in_draft` (allocation created) `→ billed` (invoice issued); back to `unbilled` when the draft line is removed or the draft is deleted (**rows deleted**) or the invoice is voided (**rows kept, `reversed_at` set**).

## 6. Releasing items

| Event | Allocation rows | Item status |
|---|---|---|
| Draft line removed, draft deleted | deleted | `in_draft → unbilled` |
| Invoice voided | `reversed_at` set | `billed → unbilled` |
| **Credit note returned to WIP** (`POST /credit-notes/{id}/wip`, owned by WIP, billing does nothing) | `reversed_at` set for the items returned | `billed → unbilled` |
| WIP write-off (`POST /wip/write-offs`) | a row with `write_off_id` | `unbilled → written_off` |

A credit-note return leaves the **invoice `issued`** (it keeps its lines, credit notes and payments): code that lists `allocations_for_invoice` must not assume that a reversed allocation means a void invoice. A reversed item can be billed again: a new allocation is created next to the reversed one.

## 7. What billing does not do

* It never writes `wip_movements`, `wip_adjustments`, `wip_transfers` or `credit_note_wip` and never edits `billing_status` except through `set_items_status`.
* It does not bill write-ons or transfers by themselves: an adjustment changes the job's WIP **balance** until the job is next billed (an invoice, draft included, that allocates items of the job settles it), nothing more. The difference between WIP and billed is still recorded per allocation (`billed_value − wip_value`) as before.
* The unified list gives `selectable` per item; the Prepare Invoice UI must only send `selectable` items.

## 8. Readers of `wip_allocations` (reports, job figures)

Joins that start from an allocation and outer-join `TimeEntry` and `Disbursement` to find the client and job (`app/repositories/reports.py::_allocation_select`, `app/repositories/time_jobs.py`) must also outer-join `Expense` (`Expense.id = wip_allocations.expense_id`, `client_id`, `job_id`, `incurred_on`): an allocation of an expense line has neither of the other two.

## 9. Opening WIP (later)

An imported opening WIP record is the fifth kind: `kind = 'opening'`, `component = 'time'`, selectable. The pattern is the one of the expense lines: a nullable `opening_wip_id` column on `wip_allocations` with its composite FK, the CHECK widened to four columns, a partial unique live index, a `LockedItems.opening` list locked after the expense lines, and one entry in the source registry of `app/services/wip_engine.py`.
