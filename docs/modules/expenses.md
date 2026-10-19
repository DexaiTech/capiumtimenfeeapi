# Expenses: expense types, claims, receipts, approval and work in progress

Code: `app/api/v1/{expense_types,expenses,expense_receipts}.py` → `app/services/{expense_types,expense_claims,expense_workflow,expense_receipts,expense_lines,expense_rules,expense_access,expense_reader,receipt_files}.py`
→ `app/repositories/{expense_types,expense_claims,expense_lines,expense_receipts,expense_paging}.py`; schemas in `app/schemas/expenses.py`;
models `app/db/models/expenses.py`; notification helpers in `app/services/notification_events.py`; the upload parser and the storage are the ones of
the tickets (`app/utils/multipart.py`, `app/services/{storage,attachment_files}.py`).
Tests: `tests/test_expense_{types,lines,lines_edit,claims,lists,workflow,corrections,period_locks,receipts,notifications,wip,references,
concurrency,migration,openapi}.py` (helpers in `tests/expense_helpers.py`).
Migrations: `024` (`expense_types` and the eight default types of every practice), `025` (`expense_claims`, `expenses`,
`expense_claim_events`), `026` (`expense_receipts`).
**Permissions added**: none. `expense.log`, `expense.read_all`, `expense.approve` and `expense.manage` already exist (see [Permissions](#permissions)).
**Notification types added**: `expense_pending`, `expense_approved`, `expense_returned` (see [Notifications](#notifications)): a UI that lists the notification
types must not assume seven.

Staff record what they spend for the practice (a train fare, a hotel, mileage, postage) on **expense claims**: a claim has lines, a line
has an expense type, a date, an amount with its VAT, optionally a client and a job to recharge it to, and receipts. A claim goes to
approval; once approved, its billable lines are **work in progress** for the client (see [Work in progress](#work-in-progress)). Everything is
scoped to one practice. Disbursements (`docs/domain.md`, Disbursements) are a separate, older module and are not changed by this one.

All paths are under `/api/v1/organizations/{organization_id}`. Another practice's ids answer 404, never 403. The rules behind the endpoints
are in `docs/domain.md` (Expenses). The interactive schema is `/openapi.json`; every route there has a summary, a description and its error
responses.

## Conventions

| Thing | Representation |
|---|---|
| Envelope | Success `{"success": true, "data": …, "message": "…" or null, "meta": … or null}`. Error `{"success": false, "error": {"code", "message", "details", "request_id"}}`. |
| Authentication | `Authorization: Bearer <access token>` on **every** call, receipt downloads included (see [Downloading and showing a receipt](#downloading-and-showing-a-receipt)). |
| Money | Integer **pence** (`net`, `vat`, `gross`, `recharge_amount`, totals, thresholds); never a float. VAT rates are basis points (`2000` = 20.00%); `markup_bp` is basis points too. Rounding is half up. |
| Dates and timestamps | `incurred_on` is a calendar day in the practice's time zone (`2026-09-07`), never in the future. Timestamps are UTC (`2026-10-18T12:05:03.169759Z`). |
| `error.details` | 422: a list of `{field, code, message}`; `field` is the request field (`net`, `gross`, `miles`, `job_id`, `expense_type_id`, `incurred_on`, `reason`, `file`, `lines`, `cursor`, …). A **query parameter** that fails the schema check (`limit=0`, `status=paid`, `date_from=abc`) is named `query.<param>`; one that breaks a rule of the service (a start after the end, a cursor of another sort) is named by its plain name. A reference in a request body to something that does not exist (an unknown `client_id`, `job_id`, `expense_type_id`, `user_id`) answers 404 with `details: {"field": "<name>"}`. Other errors: an object or `null`, listed with each [error code](#error-codes). |
| Lists | `GET /expense-types`, `/expense-claims`, `/expense-approvals`, `/expenses` and `/expense-receipts` are cursor-paginated: query `limit` (1–100, default 25) and `cursor`; response `meta {next_cursor, has_more}`. A cursor belongs to the sort and direction it came from: another sort or direction answers 422 on `cursor`. Everything else is bounded and not paginated. |
| Ids | UUIDs. |
| Write responses | Creates answer 201 with the record, updates and transitions 200 with the record, deletes 200 with `data: null`. `message` is a short sentence (`Expense claim created`). **A line endpoint returns the line** (`ExpenseLineRead`), **a claim or workflow endpoint returns the claim with its lines and history** (`ExpenseClaimDetail`), except the bulk approval and the lists (`ExpenseClaimRead`, without lines and history). |
| Request bodies | JSON objects; unknown fields answer 422. Strings are trimmed; a blank optional string becomes `null`. PATCH bodies hold any subset of the fields; `null` is refused for a field that cannot be empty. A receipt upload is `multipart/form-data`. |

## Endpoint index

| Method | Path | Permission |
|---|---|---|
| GET | `/expense-types` | `expense.log`, `expense.read_all` or `expense.manage` |
| POST | `/expense-types` | `expense.manage` |
| GET | `/expense-types/{type_id}` | as the list |
| PATCH | `/expense-types/{type_id}` | `expense.manage` |
| DELETE | `/expense-types/{type_id}` | `expense.manage` |
| GET | `/expense-claims` | any `expense.*` permission; whose claims: see [Who sees what](#who-sees-what) |
| POST | `/expense-claims` | the claimant with `expense.log`; for someone else `expense.manage` |
| GET | `/expense-claims/summary` | as the list |
| GET | `/expense-claims/{claim_id}` | as the list (one's own, one's reports', or all) |
| PATCH | `/expense-claims/{claim_id}` | the claimant with `expense.log`, or `expense.manage` |
| DELETE | `/expense-claims/{claim_id}` | as PATCH |
| POST | `/expense-claims/{claim_id}/lines` | as PATCH |
| PATCH | `/expense-claims/{claim_id}/lines/{line_id}` | as PATCH; an **approved** claim: `expense.manage` with a `reason` |
| DELETE | `/expense-claims/{claim_id}/lines/{line_id}` | as PATCH |
| POST | `/expense-claims/{claim_id}/submit` | as PATCH |
| POST | `/expense-claims/{claim_id}/recall` | as PATCH |
| POST | `/expense-claims/{claim_id}/approve` | `expense.approve`, as the claimant's manager or with `expense.read_all`; not one's own |
| POST | `/expense-claims/{claim_id}/return` | as approve |
| GET | `/expense-approvals` | `expense.approve` |
| POST | `/expense-approvals/bulk-approve` | `expense.approve` (each claim as for approve) |
| GET | `/expenses` | as `GET /expense-claims` (the lines of the visible claims) |
| POST | `/expenses/{line_id}/receipts` | as PATCH of the claim |
| GET | `/expenses/{line_id}/receipts/{receipt_id}` | whoever may see the claim |
| DELETE | `/expenses/{line_id}/receipts/{receipt_id}` | as PATCH of the claim |
| GET | `/expense-receipts` | as `GET /expense-claims` (the receipts of the visible claims) |

`/expense-claims/summary` is declared before `/expense-claims/{claim_id}`, so the word is never read as an id.

## Vocabulary

| Thing | Values |
|---|---|
| Claim status (`status`) | `draft` (being written), `submitted` (waiting for a decision), `approved` (decided; immutable but for corrections), `returned` (sent back with a reason; edited and submitted again). Lists and charts use this order. |
| History action (`events[].action`) | `submitted`, `recalled`, `approved`, `returned`, `corrected`. |
| Payer (`payer`) | `staff` (the person paid and is reimbursed; default) or `practice` (company card). Only `staff` lines count in `totals.reimbursable`. |
| Line `billing_status` | `unbilled`, `in_draft`, `billed`, `written_off`. Every line starts `unbilled`; billing sets the others (see [Work in progress](#work-in-progress)). A line that is not `unbilled` cannot be corrected (409 `EXPENSE_BILLED`). |
| Default expense types | Every practice starts with Travel (`TRAVEL`, zero-rated, billable), Mileage (`MILEAGE`, outside the scope of VAT, mileage, billable), Subsistence (`SUBSISTENCE`, standard rate), Accommodation (`ACCOM`, standard, billable), Postage (`POSTAGE`, exempt, billable), Printing (`PRINTING`, standard, billable), Software (`SOFTWARE`, standard) and Other (`OTHER`, standard). The default VAT code is the practice's live rate of that kind (none when it has none). Practices that existed before migration `024` got them from it. |

## Limits

| What | Limit |
|---|---|
| Claim title | at most 200 characters; blank clears it |
| Line description | 1–500 characters; supplier at most 200 |
| Amounts | integers 0 – 1,000,000,000,000 pence; the gross of a line must be more than zero |
| Miles | more than 0, less than 1,000,000, at most two decimal places |
| Lines | at most 100 in a `POST /expense-claims`; at most **200** in a claim (409 `EXPENSE_CLAIM_LINE_LIMIT`) |
| Return reason, approval note | at most 2,000 characters |
| Correction `reason` | at most 500 characters |
| Bulk approval | 1–100 distinct claim ids |
| Receipts | at most **10 MiB** (10,485,760 bytes) each, at most **5** per line; PDF, JPG, JPEG, PNG only |
| Expense type | `name` 1–80 characters, `code` 1–12 of `A-Z 0-9 _ -` (upper-cased) |
| List | `limit` 1–100 (default 25); `q` at most 200 characters; `status` at most 4 values, `billing_status` at most 4 |

---

# Expense types

A type says what a claim line is for. `ExpenseTypeRead`:

```json
{
  "id": "01a14ee6-aa1f-7508-9ebd-419a0757e494",
  "name": "Accommodation",
  "code": "ACCOM",
  "default_vat_rate_id": "01a14ee6-aa17-752f-ada7-24783844952f",
  "is_mileage": false,
  "default_billable": true,
  "is_active": true,
  "is_used": false,
  "can_delete": true,
  "created_at": "2026-10-18T12:05:01.984452Z",
  "updated_at": "2026-10-18T12:05:01.984452Z"
}
```

| Field | Meaning |
|---|---|
| `default_vat_rate_id` | A live VAT code (`GET /vat-rates`) or null. It prices the VAT of a line that gives only its net (or only its gross) amount and names no VAT code of its own. |
| `is_mileage` | Lines of this type are priced from `miles` (see [Mileage](#mileage)). It cannot change once a line uses the type (409 `EXPENSE_TYPE_IN_USE`). |
| `default_billable` | What a new line of this type **that has a client** starts with. |
| `is_active` | An inactive type cannot be chosen for a new line or for a line's type change; lines that already use it keep it. |
| `is_used`, `can_delete` | Whether any line uses the type. A used type is deactivated, never deleted. |

## GET /expense-types

`expense.log`, `expense.read_all` or `expense.manage`. Query: `q` (name or code), `is_active`, `is_mileage`, `sort` (`name` default, `code`), `order`
(`asc` default, `desc`), `limit`, `cursor`. Response `ExpenseTypeRead[]`.

## POST /expense-types

`expense.manage`. Body `{name, code, default_vat_rate_id?, is_mileage?, default_billable?, is_active?}`. 201, message `Expense type created`.
`name` is unique among the **active** types, ignoring case (409 `EXPENSE_TYPE_NAME_TAKEN`); `code` is unique among **all** types (409 `EXPENSE_TYPE_CODE_TAKEN`).
A VAT code that is archived or unknown is 422 on `default_vat_rate_id`.

## GET /expense-types/{type_id}

Same permissions as the list. 404 for an unknown type.

## PATCH /expense-types/{type_id}

`expense.manage`. Any subset of the create fields; `default_vat_rate_id: null` clears the VAT code. A rename, or bringing an inactive type back,
claims its name again (409 when another active type has it). 409 `EXPENSE_TYPE_IN_USE` when `is_mileage` changes on a used type.

## DELETE /expense-types/{type_id}

`expense.manage`. 200, `data: null`. A type that lines use answers 409 `EXPENSE_TYPE_IN_USE` with `details {expense_type_id, is_active}`: deactivate it instead.

---

# Claims and lines

## Objects

`ExpenseClaimRead` (every item of the lists, the approvals queue and the bulk approval):

```json
{
  "id": "01a14ee6-ae4d-726b-870c-1a08594c7a12",
  "user_id": "01a14ee6-aa6e-734a-baaf-196d7e9486d8",
  "user_name": "Jamie Lee",
  "created_by": "01a14ee6-aa6e-734a-baaf-196d7e9486d8",
  "created_by_name": "Jamie Lee",
  "title": "Client visit, Leeds",
  "status": "approved",
  "submitted_at": "2026-10-18T12:05:03.581383Z",
  "decided_at": "2026-10-18T12:05:03.780281Z",
  "decided_by": "01a14ee6-aa85-7179-9c00-65add6b50161",
  "decided_by_name": "Sam Patel",
  "decision_note": "Thanks",
  "line_count": 3,
  "first_date": "2026-09-07",
  "last_date": "2026-09-07",
  "totals": {"net": 14213, "vat": 1600, "gross": 15813, "recharge": 4730, "reimbursable": 6213},
  "actions": {
    "can_edit": false, "can_delete": false, "can_submit": false, "can_recall": false,
    "can_approve": false, "can_return": false, "can_correct": true
  },
  "created_at": "2026-10-18T12:05:03.169759Z",
  "updated_at": "2026-10-18T12:05:03.762989Z"
}
```

| Field | Type | Meaning |
|---|---|---|
| `user_id`, `user_name` | uuid, string or null | The **claimant**: whose expenses these are (and who is reimbursed). Possibly a staff member without a login. |
| `created_by`, `created_by_name` | uuid, string or null | Who entered the claim: the claimant, or the person who recorded it for them. |
| `title` | string or null | Free text, at most 200 characters. |
| `status` | enum | See [Vocabulary](#vocabulary). |
| `submitted_at` | timestamp or null | When it was last submitted; null while a draft (also after a recall). |
| `decided_at`, `decided_by`, `decided_by_name`, `decision_note` | | Set while `approved` or `returned`; **cleared when the claim is submitted again**. `decision_note` is the reason of a return (always set then) or the optional note of an approval. The history keeps everything. |
| `line_count`, `first_date`, `last_date` | int, date or null | The number of lines and the earliest and latest `incurred_on`; `0`, `null`, `null` for an empty claim. |
| `totals` | object | `net`, `vat`, `gross` of all lines; `recharge` the sum of the lines' `recharge_amount`; `reimbursable` the gross of the lines the staff member paid (`payer: staff`). Computed from the lines on every read, never stored. |
| `actions` | object | What **the caller** may do now: show or hide the buttons by it, and still handle a 403/409. `can_edit` (title, add, edit, delete lines and receipts: draft or returned), `can_delete` (draft), `can_submit` (draft or returned), `can_recall` (submitted), `can_approve` and `can_return` (submitted, and the caller may decide on it), `can_correct` (approved and the caller has `expense.manage`). |

`ExpenseClaimDetail` (`GET /expense-claims/{id}` and every write that returns a claim) is an `ExpenseClaimRead` plus:

* `lines`: `ExpenseLineRead[]`, oldest `incurred_on` first (then by id), each with its receipts.
* `events`: the claim's **history**, oldest first: `{id, action, actor_user_id, actor_name, note, created_at}`. A submission, a recall, an approval, a return and
  every [correction](#corrections-of-approved-claims) adds one; `note` is the reason or note given. It is the audit trail the claimant sees.

`ExpenseLineRead` (a line, wherever it appears):

```json
{
  "id": "01a14ee6-aea7-7687-b012-d3b476983990",
  "claim_id": "01a14ee6-ae4d-726b-870c-1a08594c7a12",
  "expense_type_id": "01a14ee6-aa1f-7508-9ebd-4197fe35336c",
  "expense_type": {"id": "01a14ee6-aa1f-7508-9ebd-4197fe35336c", "name": "Travel", "code": "TRAVEL", "is_mileage": false},
  "incurred_on": "2026-09-07",
  "description": "Train to Leeds",
  "client_id": "01a14ee6-aaa6-73f5-ac68-22ce4847ce32",
  "client": {"id": "01a14ee6-aaa6-73f5-ac68-22ce4847ce32", "code": "ACME", "name": "Acme Ltd"},
  "job_id": "01a14ee6-aab9-75a4-9f11-c53352423894",
  "job": {"id": "01a14ee6-aab9-75a4-9f11-c53352423894", "number": "TJ00002", "name": "Year-end accounts", "billing_basis": "hourly"},
  "supplier": "LNER",
  "vat_rate_id": "01a14ee6-aa17-752f-ada7-247a28512ba4",
  "vat_rate_bp": 0,
  "net": 4300,
  "vat": 0,
  "gross": 4300,
  "miles": null,
  "mileage_rate_applied": null,
  "is_billable": true,
  "markup_bp": 1000,
  "recharge_amount": 4730,
  "payer": "staff",
  "billing_status": "unbilled",
  "receipt_required": true,
  "receipt_count": 1,
  "receipts": [
    {"id": "01a14ee6-afb8-76d1-ab24-9e3f7c8441bc", "expense_id": "01a14ee6-aea7-7687-b012-d3b476983990", "file_name": "receipt.png",
     "content_type": "image/png", "size_bytes": 69, "is_image": true, "uploaded_by": "01a14ee6-aa6e-734a-baaf-196d7e9486d8",
     "uploaded_by_name": "Jamie Lee", "created_at": "2026-10-18T12:05:03.509559Z"}
  ],
  "created_at": "2026-10-18T12:05:03.229940Z",
  "updated_at": "2026-10-18T12:05:03.862028Z"
}
```

| Field | Meaning |
|---|---|
| `client_id`, `client`, `job_id`, `job` | The client and job the expense is recharged to; all null for an overhead. A job implies its client. |
| `vat_rate_id`, `vat_rate_bp` | The VAT code of the cost **and the rate it had when the line was saved** (both or neither): a later change of the rate does not change the line. |
| `net`, `vat`, `gross` | Pence; `gross = net + vat` always. |
| `miles`, `mileage_rate_applied` | Mileage lines only: the miles (a string with two decimals, `"42.50"`) and the pence per mile the net was priced at. |
| `is_billable`, `markup_bp`, `recharge_amount` | Whether the client is charged, the practice's markup when the line was saved, and what the client is charged: `net` plus the markup, or an override. `recharge_amount` is 0 for a line that is not billable. |
| `payer` | `staff` or `practice`. |
| `billing_status` | See [Vocabulary](#vocabulary). |
| `receipt_required` | `true` when the gross is above the practice's `receipt_required_threshold` and the line is not mileage (compare with `receipt_count`: the claim cannot be submitted while such a line has no receipt). |
| `receipts` | The line's receipts, oldest first (metadata only; see [Receipts](#receipts)). |

## Who sees what

* Everyone with `expense.read_all` or `expense.manage` sees every claim of the practice.
* Everyone else with an `expense.*` permission sees **their own** claims and, with `expense.approve`, the claims of the staff **they manage**
  (`manager` on the staff record). Someone else's claim is **403** (not 404): the claim exists in the practice.
* A person with no `expense.*` permission at all (the Viewer) gets 403 on every expense route.
* Writing needs more than seeing: see [Who may write](#who-may-write).

## Who may write

A claim, its lines and its receipts are written by **the claimant with `expense.log`**, and by **anyone with `expense.manage`** (for staff without a
login, or to help). Someone else's claim without `expense.manage` is 403, whatever the status. A draft or returned claim takes edits; a submitted or approved one answers 409
`EXPENSE_CLAIM_LOCKED` (`details {status}`), except the [corrections](#corrections-of-approved-claims) of an approved claim.

## GET /expense-claims

Query: `status` (repeatable), `user_id`, `mine`, `date_from`/`date_to` (inclusive), `client_id`, `job_id`, `is_billable`, `q`, `sort`, `order`, `limit`, `cursor`.

* `date_from`, `date_to`, `client_id`, `job_id` and `is_billable` match **a line of the claim** (`is_billable=true`: a billable line; `false`: a line that is not billable), and **the same line** must satisfy all of them.
* `q` searches the title, the claimant's name and the lines' description and supplier (case-insensitive substring); a blank `q` is ignored.
* `sort`: `created_at` (default, newest first), `submitted_at` (drafts, which have none, count as the oldest), `total` (gross) or `user` (the claimant's
  name, ascending by default); `order` `asc` | `desc` overrides the direction. Ties are broken by id.
* `mine=true` keeps the caller's own; with a `user_id` of someone else it is empty.

Response `ExpenseClaimRead[]` (no lines).

## POST /expense-claims

Body `{title?, user_id?, lines?}`. `user_id` is the claimant: the caller by default (needs `expense.log`), another **active** member with `expense.manage` (a member without
a login included; 404 for someone outside the practice, 422 for an inactive member). `lines` are up to 100 [lines](#post-expense-claimsclaim_idlines): **one bad line fails the whole call** and
nothing is written. 201, `ExpenseClaimDetail`, message `Expense claim created`. Every line date passes the [period lock](#period-lock).

## GET /expense-claims/summary

For the menu badges. Response:

```json
{
  "counts": {"draft": 0, "submitted": 1, "approved": 0, "returned": 0, "total": 1},
  "pending_gross": 15713,
  "awaiting_my_approval": {"count": 1, "gross": 15713}
}
```

`counts` are the visible claims by status; `pending_gross` the gross of the visible claims that are `submitted`; `awaiting_my_approval` the caller's own approval queue
(the claims `GET /expense-approvals` lists: `count` and their gross; zero without `expense.approve`).

## GET /expense-claims/{claim_id}

`ExpenseClaimDetail`. Another person's claim the caller may not see: 403; another practice's: 404.

## PATCH /expense-claims/{claim_id}

Body `{title}` (null or blank clears it). Draft or returned only (409 `EXPENSE_CLAIM_LOCKED` otherwise). 200, `ExpenseClaimDetail`.

## DELETE /expense-claims/{claim_id}

A **draft** claim, with its lines, receipts and the receipts' files; any other status answers 409 `EXPENSE_CLAIM_LOCKED`. Every line date passes the period lock. 200, `data: null`.

## POST /expense-claims/{claim_id}/lines

Adds a line to a draft or returned claim; 201, `ExpenseLineRead`, message `Expense added`. 409 `EXPENSE_CLAIM_LINE_LIMIT` beyond 200 lines.

| Field | Required | Notes |
|---|---|---|
| `expense_type_id` | yes | An **active** type of the practice (404 unknown, 422 inactive). |
| `incurred_on` | yes | The day it was spent; not in the future (422 `future_date`). It passes the [period lock](#period-lock). |
| `description` | yes | 1–500 characters. |
| `client_id`, `job_id` | no | The client must accept work (409 `CLIENT_INACTIVE` for an inactive or archived one). The job must be an **open** job (planned, in progress or on hold; 409 `JOB_CLOSED` for a completed or cancelled one) **of that client** (422 on `job_id` for another client's job); a job without a client sets the client. |
| `supplier` | no | At most 200 characters. |
| `vat_rate_id` | no | A VAT code of the practice; see [Amounts](#amounts). |
| `net`, `vat`, `gross` | see below | Pence. |
| `miles` | mileage types | See [Mileage](#mileage); refused (422) for other types. |
| `is_billable` | no | Defaults to the type's `default_billable` **and** a client being set; `false` without a client. `true` needs a client (422 `needs_client`) and a job that is not non-billable (422 `non_billable_job`). |
| `recharge_amount` | no | Overrides the recharge; needs `expense.manage` (403 otherwise) and a billable line (422 on a line that is not billable, unless 0). |
| `payer` | no | `staff` (default) or `practice`. |

### Amounts

Pence, with the VAT of the line:

* **Any two** of `net`, `vat`, `gross` give the third (`gross = net + vat`); all three must agree (422 `gross_mismatch` on `gross`); `gross` below `net` is 422 (`gross_below_net`), `vat` above `gross` is 422 (`vat_above_gross`).
* **Only `net`**: the VAT is `net × rate`; **only `gross`**: the VAT is the VAT contained in it (`gross × rate ÷ (1 + rate)`). The rate is that of `vat_rate_id` when given, else the type's
  `default_vat_rate_id` when it is still live, else **no VAT**. The line keeps the code and the rate it used (`vat_rate_id`, `vat_rate_bp`).
* The gross must be more than 0 (422 `not_positive`) and at most 1,000,000,000,000 (422 `too_large`); none of the amounts may be negative; none at all is 422 on `net` (`required`).

### Mileage

A line of a type with `is_mileage` is priced from `miles`: **`net = round(miles × the practice's mileage_rate)`** (pence per mile, half up; the rate is kept on the line as
`mileage_rate_applied`). `net` and `gross` are refused (422 `mileage_priced`); `vat` may be given. The VAT code is that of `vat_rate_id`, else the type's default. A mileage line never needs a receipt.

### Recharge

A billable line is charged to the client at **`net` plus the practice's `expense_markup_bp`** (half up) and the markup is kept on the line (`markup_bp`). A line that is not billable has
`recharge_amount` 0. Only someone with `expense.manage` may override the recharge amount.

## PATCH /expense-claims/{claim_id}/lines/{line_id}

Changes any field of a line of a draft or returned claim; what is not sent stays. 200, `ExpenseLineRead`, message `Expense updated`.

* Sending `net`, `vat` or `gross` recalculates the others as when adding (only `vat` keeps the net); changing `vat_rate_id` alone recalculates the VAT; `vat_rate_id: null` removes the code and keeps the amounts.
* Changing `miles` (or the type to a mileage type) prices the mileage again **at today's rate**; otherwise the stored rate stays.
* Changing the net or `is_billable` recalculates the recharge at today's markup, unless `recharge_amount` is sent (`expense.manage`; `null` recalculates it).
* Changing `client_id` clears the job unless `job_id` is sent. A job that did not change is not checked for being closed again.
* An edit that changes nothing writes nothing (no audit row).
* An **approved** claim's line: see [Corrections of approved claims](#corrections-of-approved-claims). A submitted claim: 409 `EXPENSE_CLAIM_LOCKED`.
* The old and the new date pass the [period lock](#period-lock).

## DELETE /expense-claims/{claim_id}/lines/{line_id}

Draft or returned claims only; removes the line's receipts and their files. 200, `data: null`. The line date passes the period lock.

---

# Workflow

```text
draft ──submit──▶ submitted ──approve──▶ approved
  ▲                  │  │
  └────recall────────┘  └──return──▶ returned ──(edit)──submit──▶ submitted
```

Every transition locks the claim row (two requests at once give one winner and one 409), writes a row of the claim's history (`events`), an audit row and, where it applies, a
notification. A transition from another status answers 409 `INVALID_STATUS_TRANSITION` with `details {from, to}`. All of them return the claim as `ExpenseClaimDetail`.

## POST /expense-claims/{claim_id}/submit

Draft or returned → submitted; message `Expense claim submitted`. Checks, in this order:

1. At least one line (422 `EXPENSE_CLAIM_EMPTY`, `details [{field: "lines", code: "empty"}]`).
2. Every line date passes the [period lock](#period-lock) (409 `PERIOD_LOCKED`).
3. Every line **above the receipt threshold** (`gross > receipt_required_threshold`; mileage never) has at least one receipt (422 `RECEIPT_REQUIRED`, one `details` entry per line without one:
   `{field: "lines", code: "receipt_required", message, line_id}`). The rule is enforced **only here**: a draft may hold lines without receipts.

It sets `submitted_at` and clears the previous decision. The approvers are notified (`expense_pending`). The claimant with `expense.log`, or `expense.manage`.

## POST /expense-claims/{claim_id}/recall

Submitted → draft, before anyone decides: clears `submitted_at`. Message `Expense claim recalled`. The claimant with `expense.log`, or `expense.manage`.

## POST /expense-claims/{claim_id}/approve

Submitted → approved; message `Expense claim approved`. Optional body `{note}` (at most 2,000 characters). Every line date passes the period lock **again**. The claimant is notified (`expense_approved`).
Its billable lines become [work in progress](#work-in-progress).

## POST /expense-claims/{claim_id}/return

Submitted → returned; message `Expense claim returned`. Body `{reason}`: **required** (422 `EXPENSE_RETURN_REASON_REQUIRED` for a missing, empty or blank one; at most 2,000 characters). The claimant edits the claim and
submits it again. The claimant is notified (`expense_returned`, with the reason).

### Who decides

Approving and returning need **`expense.approve`**, and either **the claimant's manager** (the `manager` of the staff record) **or `expense.read_all`**; otherwise 403 `PERMISSION_DENIED`.
**Nobody decides on their own claim** unless they hold `organization.update` (403 `SELF_APPROVAL_FORBIDDEN`). `actions.can_approve` and `can_return` already say so.

## GET /expense-approvals

The approval queue: **submitted** claims waiting for the caller, **oldest submission first**: the claims of the people the caller manages, or everyone's with `expense.read_all`. The caller's own claim is listed only when they may
decide on it (`organization.update`). Query `user_id`, `q`, `limit`, `cursor`. Response `ExpenseClaimRead[]`. 403 without `expense.approve`; an approver who manages nobody and cannot read everything gets an empty list.

## POST /expense-approvals/bulk-approve

Body `{claim_ids: [uuid]}` (1–100; duplicates are ignored). **All or nothing**, in one transaction:

* an id that is not a claim of the practice: 404 with `details {claim_ids: [the missing ids]}`;
* a claim that is not submitted: 409 `INVALID_STATUS_TRANSITION`;
* a claim the caller may not decide on: 403 (one's own claim: `SELF_APPROVAL_FORBIDDEN`);
* a line in a locked period: 409 `PERIOD_LOCKED`.

Response 200: `ExpenseClaimRead[]` in the order of the request, message `3 expense claims approved`. Each claim gets its own history row, audit row and notification. The claims are locked in id order, so overlapping bulk calls
never deadlock.

## Corrections of approved claims

An approved claim cannot be edited by its claimant, but someone with **`expense.manage`** may correct a line (`PATCH …/lines/{line_id}`, any field) **with a `reason`** (at most 500 characters):

* no reason: 422 `EXPENSE_CORRECTION_REASON_REQUIRED`; a line that is billed, in a draft invoice or written off: 409 `EXPENSE_BILLED` (`details {billing_status}`); the line date passes the period lock;
* the claim **stays approved** with its decision; the history gets a `corrected` event with the reason, and the audit row is `expense.corrected` with the before and after values;
* a correction that changes nothing writes nothing; the totals and the recharge amount follow at once (so does the [work in progress](#work-in-progress));
* adding or deleting lines, and editing the title, stay closed to everybody (409 `EXPENSE_CLAIM_LOCKED`).

---

# Receipts

A line holds at most **5** receipts (409 `RECEIPT_LIMIT_REACHED`, `details {max_receipts: 5}`). `ReceiptRead`: `{id, expense_id, file_name, content_type, size_bytes, is_image, uploaded_by, uploaded_by_name, created_at}`.

## What is accepted

An upload is accepted only when **all** of these hold, whatever the client says:

| Kind | Extensions | Stored `content_type` | The content must |
|---|---|---|---|
| PNG | `.png` | `image/png` | start with the PNG signature |
| JPEG | `.jpg`, `.jpeg` | `image/jpeg` | start with `FF D8 FF` |
| PDF | `.pdf` | `application/pdf` | start with `%PDF` |

* The **extension** must be on the list **and** the **content** must be what the extension says; a mismatch (an HTML or SVG file named `.png`, a PNG named `.pdf`) is refused (422 `FILE_TYPE_NOT_ALLOWED`). GIF, WebP, SVG, HTML, Office files and everything else are refused.
* A `Content-Type` the client declares for the file part must fit the kind (`image/png` or `image/x-png` for a PNG, `image/jpeg`, `image/jpg` or `image/pjpeg` for a JPEG, `application/pdf` or `application/x-pdf` for a PDF); a contradicting one is refused. Missing or `application/octet-stream` is fine. The stored `content_type` always comes from the table, never from the client.
* The size is at most **10 MiB** (10,485,760 bytes, the limit inclusive): 413 `FILE_TOO_LARGE` with `details {max_bytes: 10485760}`; the request body is read under that cap (the limit plus 64 KiB of envelope), so an oversized upload is stopped early, and a declared `Content-Length` above it is refused without reading. An empty file is 422 `FILE_EMPTY`.
* The file name is sanitised like a ticket attachment's (no path, no control characters, none of `<>:"|?*`, at most 150 characters). Files are stored under a random key in the practice's own folder of `STORAGE_DIR`.
* There is **no virus scanning**.

## POST /expenses/{line_id}/receipts

`multipart/form-data` with one file part named `file`. 201, `ReceiptRead`, message `Receipt uploaded`. Allowed while the claim is a draft or returned, to the claimant with `expense.log` and to `expense.manage` (403 otherwise, 409 `EXPENSE_CLAIM_LOCKED` for a submitted or
approved claim). A caller who cannot upload (403, 404, 409) is refused **before the body is read**. Other errors: 400 `FILE_INVALID` (not multipart, no `file`, two files, no file name), 413, 422, 409 `RECEIPT_LIMIT_REACHED`, 503 `STORAGE_UNAVAILABLE`. Nothing is stored when a check fails, and a file whose database write fails is removed again. Receipts are not dated data:
the period lock does not stop them.

```text
POST /api/v1/organizations/{organization_id}/expenses/{line_id}/receipts
Authorization: Bearer <access token>
Content-Type: multipart/form-data; boundary=…

--…
Content-Disposition: form-data; name="file"; filename="taxi.jpg"
Content-Type: image/jpeg

<bytes>
--…--
```

Do not set the `Content-Type` header yourself with `fetch`/`FormData`: the browser adds the boundary.

## GET /expenses/{line_id}/receipts/{receipt_id}

Whoever may see the claim. Streams the file with `Content-Type` (the stored type), `Content-Length`, `X-Content-Type-Options: nosniff`, `Cache-Control: no-store` and `Content-Disposition`: `inline; filename="…"; filename*=UTF-8''…` for
**images**, `attachment; …` for a PDF. 404 `RECEIPT_FILE_MISSING` when the record exists but the storage has lost the file; 404 for a receipt that is not on that line.

### Downloading and showing a receipt

The request needs the Bearer token, which a browser does not send for `<img src>` or `<a href>`: **fetch the bytes with the token and show them from a blob URL** exactly as for ticket attachments (`docs/modules/tickets.md`, "Downloading and showing a file"):
an image (`is_image`) in an `<img src={blobUrl}>`, a PDF with `<a href={blobUrl} download={file_name}>` or in a new tab; revoke the URL when the element goes away.

## DELETE /expenses/{line_id}/receipts/{receipt_id}

Removes the record and the stored file (after the commit). Draft or returned claims only, by the claimant with `expense.log` or `expense.manage`. 200, `data: null`, message `Receipt deleted`. Deleting a line, a claim or a receipt removes its files from the storage.

## GET /expense-receipts

The Receipts page: every receipt the caller may see (the same visibility as `GET /expense-claims`), each with the line and the claim it proves:
`ReceiptListItem` = `ReceiptRead` + `{claim_id, claim_status, claim_title, user_id, user_name, description, incurred_on, gross, client}` (`client`: `{id, code, name}` or null).
Query `claim_id`, `expense_id`, `user_id`, `mine`, `client_id`, `status` (of the claim; repeatable), `date_from`/`date_to` (the line date, inclusive), `q` (file name, description, supplier, claim title, claimant), `sort` (`created_at` default | `incurred_on` | `gross`), `order` (`desc` default | `asc`), `limit`, `cursor`.

---

# Lines for the finance views

## GET /expenses

Expense lines across claims, for My Expenses and the finance views. The same visibility as `GET /expense-claims`. Each item is an `ExpenseLineRead` plus `{claim_status, claim_title, user_id, user_name}`.
Query: `status` (of the claim; repeatable), `claim_id`, `user_id`, `mine`, `client_id`, `job_id`, `expense_type_id`, `is_billable`, `billing_status` (repeatable), `date_from`/`date_to` (the line date, inclusive), `needs_receipt` (`true`: only the lines that **need a receipt and have none**: gross above the practice's `receipt_required_threshold`, not mileage, no receipt yet; `false` or absent: no filter), `q` (description, supplier, claim title, claimant),
`sort` (`incurred_on` default | `gross` | `created_at`), `order` (`desc` default | `asc`), `limit`, `cursor`.

---

# Period lock

Expense lines are **dated data**: `docs/modules/period-locks.md` closes them. Everything that creates, edits, moves or deletes dated lines, and the steps that make them count, asks the lock first (area `expense`, `period_locks.assert_open` /
`assert_open_many`, inside the transaction and before writing), and answers **409 `PERIOD_LOCKED`** when a date is on or before `locked_through`:

| Operation | Dates asked |
|---|---|
| `POST /expense-claims` with lines | all the lines' dates |
| Add a line | the line's date |
| Edit a line (when something changes) | the old and the new date; a correction of an approved claim the same |
| Delete a line | its date |
| Delete a draft claim | all its lines' dates |
| Submit, approve, bulk approve | all the claim's lines' dates (approving checks again, so a period locked after the submission stops the approval; bulk: all or nothing) |
| Return, recall, receipts, every read | never |

---

# Work in progress

Expense lines feed WIP and billing through **one function**, `wip_eligible_expenses` in `app/repositories/expense_lines.py`:

```python
async def wip_eligible_expenses(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    client_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    date_from: date | None = None,  # inclusive, on the line date
    date_to: date | None = None,  # inclusive
    ids: Collection[uuid.UUID] | None = None,  # only these lines (an empty collection: none)
    lock: bool = False,  # SELECT ... FOR UPDATE OF the lines (billing, before allocating)
) -> list[WipExpense]: ...
```

A line is WIP when **its claim is `approved`**, the line **`is_billable`**, has a **client**, **`billing_status = 'unbilled'`** and **`recharge_amount > 0`**. The result is ordered by line date, then id.
`WipExpense` is a frozen dataclass: `id`, `claim_id`, `user_id` (the claimant), `client_id`, `job_id`, `incurred_on`, `description`, `supplier`, `net`, `vat`, `recharge_amount` (what the client is charged, pence),
`vat_rate_id`, `vat_rate_bp` (the VAT code the cost had and its rate when the line was saved; null when it has none). `wip_expense_conditions(organization_id)` returns the same rule as SQLAlchemy conditions over `Expense` for queries that aggregate
(it works whether or not the query already joins the claim). The partial index `ix_expenses_wip (organization_id, client_id, incurred_on)` serves it.

`billing_status` is owned by billing, the same four values as time and disbursements: when the billing module takes expense lines it reads them with `ids` and `lock=True`, sets `in_draft` while a line is on a draft invoice, `billed` once the invoice is issued, `written_off` for a write-off and `unbilled` again when
that is reversed. Only a line that is `unbilled` can be corrected. A client that has expense lines cannot be deleted (409 `CLIENT_IN_USE`, `details.expenses`) and neither can a job (409 `JOB_IN_USE`); a client merge moves the lines to the surviving client.

---

# Notifications

Through the notification service (`docs/modules/notifications.md`); all three have in-app and email on by default, SMS off, and the practice and each person may change that.

| Type | Raised when | Recipients | Link |
|---|---|---|---|
| `expense_pending` | A claim is submitted (once per submission) | The claimant's manager when they are active and hold `expense.approve`; else every active member who holds both `expense.approve` and `expense.read_all`; never the claimant | `/expenses/approvals` |
| `expense_approved` | A claim is approved (once per approval; also in a bulk approval) | The claimant (not when they approved their own claim) | `/expenses/mine` |
| `expense_returned` | A claim is returned (once per return) | The claimant, with the reason in the text (not when they returned their own) | `/expenses/mine` |

`entity_type` is `expense_claim`, `entity_id` the claim, `payload {claim_total}` (formatted, e.g. `£84.50`). A claimant without a login is skipped without failing anything. The UI should route `/expenses/approvals` to the approvals queue and `/expenses/mine` to My Expenses.

---

# Permissions

| Code | Allows |
|---|---|
| `expense.log` | Recording and submitting **one's own** claims: lines, receipts; reading the expense types |
| `expense.read_all` | Seeing everyone's claims, lines and receipts; reading the types; **deciding** on any claim (with `expense.approve`) |
| `expense.approve` | Approving and returning claims, the approval queue and the bulk approval, for one's reports (the staff one manages) or, with `expense.read_all`, anybody's; one's own claim only with `organization.update` |
| `expense.manage` | Creating, editing and deleting the expense types; writing **anyone's** claims, lines and receipts (staff without a login, help); overriding a recharge amount; **correcting approved claims** |

| Role | Codes |
|---|---|
| Owner, Admin, Partner | all four (and `organization.update`: they may decide on their own claims) |
| Manager | `expense.log`, `expense.read_all`, `expense.approve` |
| Staff | `expense.log` |
| Billing | `expense.read_all` |
| Viewer | none: every expense route answers 403 |

Custom roles get exactly the codes they are given. Hide or disable what the caller cannot do (`actions` on a claim says it), and still handle a 403.

# Error codes

| HTTP | `error.code` | When | `error.details` |
|---|---|---|---|
| 400 | `FILE_INVALID` | A receipt upload that is not `multipart/form-data`, has no `file` part, more than one, or no file name | null |
| 401 | `AUTH_REQUIRED` | No or an invalid access token | null |
| 403 | `PERMISSION_DENIED` | The role lacks the code; someone else's claim the caller may not see or write; deciding on a claim one may not decide on; overriding a recharge without `expense.manage` | null |
| 403 | `SELF_APPROVAL_FORBIDDEN` | Approving or returning one's own claim without `organization.update` | null |
| 404 | `RESOURCE_NOT_FOUND` | Unknown id, or another practice's; a reference in a body that does not exist | `{"field": "client_id"}` for a body reference; bulk approval `{"claim_ids": [ids]}` |
| 404 | `RECEIPT_FILE_MISSING` | The receipt record exists but the stored file is gone | null |
| 409 | `EXPENSE_CLAIM_LOCKED` | Changing a claim, a line or a receipt of a submitted or approved claim; deleting a claim that is not a draft | `{"status": "submitted"}` |
| 409 | `INVALID_STATUS_TRANSITION` | Submit, recall, approve or return from a status that does not allow it | `{"from", "to"}` |
| 409 | `EXPENSE_BILLED` | Correcting a line that is in a draft invoice, billed or written off | `{"billing_status"}` |
| 409 | `EXPENSE_TYPE_IN_USE` | Deleting a type that lines use, or changing its `is_mileage` | `{"expense_type_id", "is_active"}` (delete) |
| 409 | `EXPENSE_TYPE_NAME_TAKEN`, `EXPENSE_TYPE_CODE_TAKEN` | A type with that name (active ones) or code exists | `[{field, code: "duplicate", message}]` |
| 409 | `EXPENSE_CLAIM_LINE_LIMIT` | The claim already holds 200 lines | `{"max_lines": 200}` |
| 409 | `RECEIPT_LIMIT_REACHED` | The line already holds 5 receipts | `{"max_receipts": 5}` |
| 409 | `JOB_CLOSED` | A line on a completed or cancelled job | `{"job_id", "status"}` |
| 409 | `CLIENT_INACTIVE` | A line for an inactive or archived client | |
| 409 | `PERIOD_LOCKED` | A dated change on or before the period lock | `{"date", "locked_through", "area": "expense"}` |
| 409 | `CLIENT_IN_USE`, `JOB_IN_USE` | Deleting a client or a job that expense lines refer to | counts (client) |
| 413 | `FILE_TOO_LARGE` | A receipt above 10 MiB (also a body bigger than that plus its envelope) | `{"max_bytes": 10485760}` |
| 413 | `REQUEST_TOO_LARGE` | Any other write whose body exceeds `MAX_REQUEST_BODY_KB` (receipt uploads are exempt) | |
| 422 | `VALIDATION_ERROR` | A field or parameter is invalid (see Conventions for the field names); amounts that do not add up (`gross_mismatch`, `gross_below_net`, `vat_above_gross`, `not_positive`, `too_large`), `miles`, dates in the future, a job of another client | `[{field, code, message}]` |
| 422 | `EXPENSE_CLAIM_EMPTY` | Submitting a claim without lines | `[{field: "lines", code: "empty", message}]` |
| 422 | `RECEIPT_REQUIRED` | Submitting with lines above the receipt threshold that have no receipt | `[{field: "lines", code: "receipt_required", message, line_id}]` |
| 422 | `EXPENSE_RETURN_REASON_REQUIRED` | Returning a claim without a reason | `[{field: "reason", code: "required", message}]` |
| 422 | `EXPENSE_CORRECTION_REASON_REQUIRED` | Correcting an approved line without a reason | `[{field: "reason", code: "required", message}]` |
| 422 | `FILE_TYPE_NOT_ALLOWED` | Extension not on the list, content not what the extension says, or a contradicting declared type | `[{field: "file", code: "type_not_allowed", message}]` |
| 422 | `FILE_EMPTY` | A zero-byte receipt | `[{field: "file", code: "empty", message}]` |
| 429 | `RATE_LIMITED` | The global request ceiling | `{"retry_after": seconds}` |
| 503 | `STORAGE_UNAVAILABLE` | The file store cannot take the receipt | null |

# Audit

Every write is audited in the same transaction (`GET /audit-logs`, `audit_log.read`), with the caller, the IP address, the user agent and the request id. A change that changes nothing writes nothing.

| Action | `target_type` | Meta |
|---|---|---|
| `expense_type.created` | `expense_type` | `name`, `code`, `is_mileage` |
| `expense_type.updated` | `expense_type` | `name`, `code`, `changes: {field: [old, new]}` |
| `expense_type.deleted` | `expense_type` | `name`, `code` |
| `expense_claim.created` | `expense_claim` | `user_id`, `user_name`, `title`, `line_count`, `gross` |
| `expense_claim.updated` | `expense_claim` | `title`, `changes: {title: [old, new]}` |
| `expense_claim.deleted` | `expense_claim` | `user_id`, `title`, `line_count`, `gross` |
| `expense_claim.submitted`, `.recalled`, `.approved`, `.returned` | `expense_claim` | `user_id`, `user_name`, `title`, `old_status`, `new_status`, `line_count`, `gross`, and `note` (the reason or the note) |
| `expense.created`, `expense.deleted` | `expense` | `claim_id`, `claimant_id`, `expense_type_id`, `description` (cut), `incurred_on`, `client_id`, `net`, `vat`, `gross`, `recharge_amount`, `is_billable`, `payer` |
| `expense.updated` | `expense` | the same, and `changes: {field: [old, new]}` (and `reason` when one was given) |
| `expense.corrected` | `expense` | the same as `expense.updated`, for an approved claim; `reason` is always there |
| `expense_receipt.added` | `expense_receipt` | `claim_id`, `expense_id`, `file_name`, `content_type`, `size_bytes` |
| `expense_receipt.deleted` | `expense_receipt` | `claim_id`, `expense_id`, `file_name`, `size_bytes` |

# Settings the module reads

Practice settings (`PATCH /organizations/{id}`, `organization.update`; money in pence; `docs/modules/foundation.md`): `receipt_required_threshold` (default 2,500: a line above it, gross, needs a receipt to be submitted), `mileage_rate` (45 pence a mile),
`expense_markup_bp` (0: basis points added when a billable expense is recharged), the practice's time zone (the day "today" is) and its currency (the notification text). A new practice is seeded with the eight default types (`app/services/practice_defaults.py`).
Claims, lines and receipts keep what they were saved with (the markup, the VAT rate, the mileage rate): changing a setting does not change them, except that **editing** a line prices what the edit changes again at today's values.

# Screens and the calls behind them

| Screen | Calls |
|---|---|
| Expenses menu badges | `GET /expense-claims/summary` (`counts`, `awaiting_my_approval`) |
| My Expenses | `GET /expense-claims?mine=true&sort=created_at` (filters: `status`, `date_from`, `date_to`, `q`); a line view: `GET /expenses?mine=true` |
| New claim / edit | `POST /expense-claims` (with `lines`, or empty and then `POST …/lines`); `GET /expense-types?is_active=true` for the type picker; clients, jobs and VAT codes from their own endpoints; `PATCH`/`DELETE …/lines/{id}`; receipts: upload `POST /expenses/{line}/receipts`, show with the blob flow; **Submit**: `POST …/submit` and show the `RECEIPT_REQUIRED` lines (`details[].line_id`) |
| Claim detail | `GET /expense-claims/{id}`: lines, receipts, `events` (history), buttons by `actions` |
| Approvals | `GET /expense-approvals` (oldest first); approve `POST …/approve`; return with a reason `POST …/return`; select many: `POST /expense-approvals/bulk-approve` |
| All expenses / finance | `GET /expense-claims` (filters, sorts) and `GET /expenses` (lines: `billing_status`, `is_billable`, `client_id`, `expense_type_id`) |
| Receipts | `GET /expense-receipts` (filters, sorts); open one with the blob flow; the lines that still need a receipt: `GET /expenses?needs_receipt=true` |
| Settings > Expense types | `GET/POST/PATCH/DELETE /expense-types` (`expense.manage`; deactivate a used type) |
| Corrections | `PATCH …/lines/{id}` with a `reason` on an approved claim (`actions.can_correct`) |

# Types at a glance

```ts
type ClaimStatus = 'draft' | 'submitted' | 'approved' | 'returned'
type ClaimEventAction = 'submitted' | 'recalled' | 'approved' | 'returned' | 'corrected'
type Payer = 'staff' | 'practice'
type BillingStatus = 'unbilled' | 'in_draft' | 'billed' | 'written_off'

interface ExpenseType {
  id: string; name: string; code: string; default_vat_rate_id: string | null; is_mileage: boolean
  default_billable: boolean; is_active: boolean; is_used: boolean; can_delete: boolean; created_at: string; updated_at: string
}
interface Receipt {
  id: string; expense_id: string; file_name: string; content_type: string; size_bytes: number
  is_image: boolean; uploaded_by: string; uploaded_by_name: string | null; created_at: string
}
interface ExpenseLine {
  id: string; claim_id: string; expense_type_id: string
  expense_type: { id: string; name: string; code: string; is_mileage: boolean }
  incurred_on: string; description: string
  client_id: string | null; client: { id: string; code: string; name: string } | null
  job_id: string | null; job: { id: string; number: string; name: string; billing_basis: string } | null
  supplier: string | null; vat_rate_id: string | null; vat_rate_bp: number | null
  net: number; vat: number; gross: number; miles: string | null; mileage_rate_applied: number | null
  is_billable: boolean; markup_bp: number; recharge_amount: number; payer: Payer; billing_status: BillingStatus
  receipt_required: boolean; receipt_count: number; receipts: Receipt[]; created_at: string; updated_at: string
}
interface ExpenseLineListItem extends ExpenseLine {
  claim_status: ClaimStatus; claim_title: string | null; user_id: string; user_name: string | null
}
interface ClaimActions {
  can_edit: boolean; can_delete: boolean; can_submit: boolean; can_recall: boolean
  can_approve: boolean; can_return: boolean; can_correct: boolean
}
interface ExpenseClaim {
  id: string; user_id: string; user_name: string | null; created_by: string; created_by_name: string | null
  title: string | null; status: ClaimStatus; submitted_at: string | null; decided_at: string | null
  decided_by: string | null; decided_by_name: string | null; decision_note: string | null
  line_count: number; first_date: string | null; last_date: string | null
  totals: { net: number; vat: number; gross: number; recharge: number; reimbursable: number }
  actions: ClaimActions; created_at: string; updated_at: string
}
interface ClaimEvent {
  id: string; action: ClaimEventAction; actor_user_id: string; actor_name: string | null; note: string | null; created_at: string
}
interface ExpenseClaimDetail extends ExpenseClaim { lines: ExpenseLine[]; events: ClaimEvent[] }
interface ExpenseSummary {
  counts: Record<ClaimStatus | 'total', number>; pending_gross: number
  awaiting_my_approval: { count: number; gross: number }
}
interface ReceiptListItem extends Receipt {
  claim_id: string; claim_status: ClaimStatus; claim_title: string | null; user_id: string; user_name: string | null
  description: string; incurred_on: string; gross: number; client: { id: string; code: string; name: string } | null
}

// Requests
interface ExpenseLineInput {
  expense_type_id: string; incurred_on: string; description: string; client_id?: string | null; job_id?: string | null
  supplier?: string | null; vat_rate_id?: string | null; net?: number; vat?: number; gross?: number; miles?: number | string
  is_billable?: boolean; recharge_amount?: number; payer?: Payer
}
interface ExpenseClaimInput { title?: string | null; user_id?: string; lines?: ExpenseLineInput[] }
```
