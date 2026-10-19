# Period locks

A practice closes a period once its time, expenses and work in progress have been reported, so
nobody can change the figures behind a closed month by accident. The lock is one date per
practice, `locked_through`: **every day on or before it is closed** (the date itself included),
every later day is open. Nothing is locked until somebody locks a period.

A lock closes three kinds of dated data, called *areas* in the API and in the error details:

| Area | What it covers |
|---|---|
| `time` | Time entries: logging, editing, moving, deleting, corrections, a timer that is stopped |
| `expense` | Disbursements and expense claim lines (record, edit, move, delete, submit, approve) |
| `wip` | WIP write-offs and their reversal, dated by the write-off date |

Reading is never blocked, and neither are invoices, credit notes and payments (see
[What is not blocked](#what-is-not-blocked)). The lock applies to everybody, the Owner included:
to change closed data, reopen the period first (it needs a reason and leaves a trail).

All paths are under `/api/v1/organizations/{organization_id}`.

## Endpoints

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/period-locks` | `period_lock.read` | The current lock and the latest event |
| GET | `/period-locks/history` | `period_lock.read` | Every lock and reopening, newest first, cursor-paginated (`limit` 1-100, default 25; `cursor`) |
| POST | `/period-locks` | `period_lock.manage` | Lock up to a date (moves the lock forward) |
| POST | `/period-locks/reopen` | `period_lock.reopen` | Move the lock back, or remove it; needs a reason |

Permission checks come before the body is read, so a caller without the permission gets 403
whatever they send. A practice the caller is not an active member of answers 404 on every route
and its lock is never visible. The other practices' locks are independent: each practice has its
own row.

| Role | `period_lock.read` | `period_lock.manage` | `period_lock.reopen` |
|---|---|---|---|
| Owner, Admin | yes | yes | yes |
| Partner | yes | yes | no |
| Manager, Billing | yes | no | no |
| Staff, Viewer | no | no | no |

The three permissions are independent: a custom role may hold any of them without the others
(`docs/modules/permissions.md` lists every code).

## Shapes

`PeriodLockRead` (`data` of the three non-history routes):

```json
{
  "locked_through": "2026-03-31",
  "updated_at": "2026-04-02T09:30:12.512345Z",
  "last_event": {
    "id": "…", "event_type": "locked",
    "previous_locked_through": "2026-02-28", "locked_through": "2026-03-31",
    "reason": "March VAT return filed",
    "actor_user_id": "…", "actor_name": "Ada Smith", "created_at": "2026-04-02T09:30:12.512345Z"
  }
}
```

A practice that never locked anything answers
`{"locked_through": null, "updated_at": null, "last_event": null}`. `updated_at` and `last_event`
describe the latest change; `locked_through` is null again after a reopening to "none".

`PeriodLockEventRead` is one entry of `GET /period-locks/history` (`data` is a list, `meta` has
`next_cursor` and `has_more`): `event_type` is `locked` or `reopened`; `previous_locked_through`
and `locked_through` are the lock before and after (null = nothing locked); `reason` is optional
for a lock and always present for a reopening; `actor_name` is the full name of the person.

`PeriodLockCreate` (`POST /period-locks`):

| Field | Rule |
|---|---|
| `locked_through` | required date, 2000-01-01 or later; **later than the current lock** (422 `PERIOD_LOCK_NOT_FORWARD`) and **not after today** in the practice's time zone (422 `PERIOD_LOCK_IN_FUTURE`) |
| `reason` | optional, up to 500 characters; blank means none |

`PeriodLockReopen` (`POST /period-locks/reopen`):

| Field | Rule |
|---|---|
| `reopen_to` | **required** (it may be `null`): the new last locked day, **earlier than the current lock** (422 `PERIOD_LOCK_NOT_BACKWARD`, also when nothing is locked); `null` removes the lock altogether. Required on purpose: an empty body never unlocks the practice |
| `reason` | **required**, 1-500 characters (422 `PERIOD_LOCK_REASON_REQUIRED` when missing or blank) |

Both routes answer 200 with the new `PeriodLockRead`. Unknown fields are 422. A lock or a
reopening is one transaction: the lock row, the history event and the audit entry are written
together or not at all.

## Error codes

| Status | Code | When |
|---|---|---|
| 409 | `PERIOD_LOCKED` | A write is dated on or before `locked_through` (every enforced route, see below) |
| 422 | `PERIOD_LOCK_NOT_FORWARD` | `POST /period-locks`: the date is not later than the current lock |
| 422 | `PERIOD_LOCK_IN_FUTURE` | `POST /period-locks`: the date is after today in the practice's time zone |
| 422 | `PERIOD_LOCK_NOT_BACKWARD` | `POST /period-locks/reopen`: nothing is locked, or `reopen_to` is not earlier than the lock |
| 422 | `PERIOD_LOCK_REASON_REQUIRED` | `POST /period-locks/reopen`: no reason |
| 422 | `VALIDATION_ERROR` | Malformed body (a date that is not ISO 8601, a reason over 500 characters, a missing `reopen_to`) |
| 403 | `PERMISSION_DENIED` | The caller lacks the permission of the route |
| 404 | `RESOURCE_NOT_FOUND` | Not a member of the practice |

The four 422 business codes appear both as `error.code` and as `error.details[0].code`, with the
field that caused them (`locked_through`, `reopen_to` or `reason`) in `details[0].field`. Dates
inside the **messages** are written the UK way (`31/03/2026`) because people read them; dates in
`details` are ISO 8601.

### The refusal of a write: 409 `PERIOD_LOCKED`

```json
{
  "success": false,
  "error": {
    "code": "PERIOD_LOCKED",
    "message": "Time dated 15/03/2026 cannot be changed: the period is locked through 31/03/2026.",
    "details": {"date": "2026-03-15", "locked_through": "2026-03-31", "area": "time"},
    "request_id": "…"
  }
}
```

The message starts with `Time`, `An expense` or `A WIP movement` according to `area`. `date` is the
date that was refused: for a request that touches several dates (a bulk batch, a move from one
date to another, a claim with many lines) it is the **earliest** of them, and nothing at all is
written.

## Locking and reopening

- Locking only moves the lock **forward**: a date that is not later than the current one is 422
  `PERIOD_LOCK_NOT_FORWARD` (the same date twice is a refusal, not a no-op, so a double click
  does not write two history rows). "Today" is the practice's today (its `timezone`), and the
  lock may reach today itself but not tomorrow.
- Reopening moves the lock **back** to an earlier date or removes it (`reopen_to: null`), and
  always says why.
- Two requests at the same time are serialised on the practice's lock row (`FOR UPDATE`), so a
  chain of locks is always strictly increasing and a lock cannot be recorded twice.
- Every change appends an event to `period_lock_events` and writes an audit entry, `period_lock.locked`
  or `period_lock.reopened`, with `target_type` `period_lock` and this metadata:

  ```json
  {
    "event_type": "reopened", "locked_through": "2026-02-28",
    "previous_locked_through": "2026-03-31", "reason": "Late invoice from the supplier",
    "changes": {"locked_through": ["2026-03-31", "2026-02-28"]}
  }
  ```

## How the lock is enforced

One function family in `app/services/period_locks.py` is the only gatekeeper. A service that
creates, edits or deletes dated time, expense or WIP data calls it **inside its unit of work,
before it writes**:

```python
from datetime import date
from collections.abc import Collection

PeriodArea = Literal["time", "expense", "wip"]


async def assert_open(
    session: AsyncSession, organization_id: uuid.UUID, on_date: date, area: PeriodArea
) -> None: ...


async def assert_open_many(
    session: AsyncSession,
    organization_id: uuid.UUID,
    days: Collection[date],
    area: PeriodArea,
) -> None: ...


async def locked_dates(
    session: AsyncSession, organization_id: uuid.UUID, days: Collection[date]
) -> dict[date, str]: ...
```

Exact behaviour:

- `assert_open` raises `PeriodLockedError` (409 `PERIOD_LOCKED`, a `ConflictError`) when
  `on_date <= locked_through`, and returns `None` otherwise. A practice with no lock row, or with
  `locked_through` null, is open.
- `assert_open_many` does the same for a collection of days with **one** lookup; the error names
  the earliest day (`min(days)`). An empty collection returns at once without touching the
  database.
- `locked_dates` never raises: it returns `{day: "Period locked until 31/03/2026"}` for each of
  `days` that lies in the locked period and leaves the other days out; an empty collection, or a
  practice with nothing locked, gives `{}`. The re-rating job uses it to skip locked time and
  to say why.
- Both `assert_*` functions **share-lock the practice's lock row** (`SELECT ... FOR SHARE`) and
  hold the lock until the transaction ends. A request that locks or reopens takes the row
  `FOR UPDATE`, so: a lock that is being set **waits for the writes already checked against the
  old state**, and a write that starts while a lock is being set **waits for it and is then
  refused**. No write can slip into a period after the lock was recorded.
- Because the check runs inside the unit of work, a refused write leaves nothing behind (no row,
  no audit entry, no number taken).
- The dates are compared as calendar days: time, expenses and write-offs carry plain dates in the
  practice's calendar, so there is no time-zone conversion.

### Where it is called

| Area | Service | Operation | Date checked |
|---|---|---|---|
| `time` | `TimeEntryService` | create (also what a **timer stop** does) | the entry date |
| `time` | `TimeEntryService` | bulk create | every entry date of the batch (one refusal, nothing written) |
| `time` | `TimeEntryService` | update | the old date **and** the new date |
| `time` | `TimeEntryService` | delete | the entry date |
| `time` | `TimeCorrectionService` | correct an approved entry | the old date and the new date |
| `expense` | `DisbursementService` | create | `incurred_on` |
| `expense` | `DisbursementService` | update | the old `incurred_on` and, when sent, the new one |
| `expense` | `DisbursementService` | delete | the old `incurred_on` |
| `expense` | `ExpenseClaimService` | create (with lines), add line | each line's `incurred_on` |
| `expense` | `ExpenseClaimService` | update line | the old and the new `incurred_on` |
| `expense` | `ExpenseClaimService` | delete line, delete claim | the line dates |
| `expense` | `ExpenseWorkflowService` | submit, approve | the dates of all the claim's lines |
| `wip` | `WipService` | create write-off | `written_off_on` (today in the practice's time zone when not sent) |
| `wip` | `WipService` | reverse write-off | the write-off's own `written_off_on` |
| re-rating | `rerate` | re-rate a period | `locked_dates`: locked entries are skipped with the reason |

A change of a time entry or disbursement that moves it **out of** a locked period is refused as
well as one that moves it in: the old date is checked too.

### What is not blocked

- **Reads** of anything, the lock state and its history included.
- **Timesheet decisions**: submit, recall, approve, reject and reopen change the sheet's status,
  not the time on it, and work in a locked week. The time itself stays closed.
- **Timers**: start, pause, resume and discard write no time. Only the **stop**, which writes the
  entry, can be refused; when it is, no entry is written and the timer is kept as it was, a
  `replace` start is refused for the same reason (it stops the running timer first), and the
  person can discard the timer or ask for the period to be reopened.
- **Invoices, credit notes, payments and statements**: billing a locked period is the point of
  closing it. Marking time and disbursements as billed or paid is billing status, not an edit of
  the work.
- Jobs, clients, grades, rates and every other record without a date of work.

## Data

`period_locks`: one row per practice (`UNIQUE (organization_id)`), created with the practice (and
for the practices that existed when migration 020 ran). Columns: `id`, `organization_id`,
`locked_through` (date, null = nothing locked), `created_at`, `updated_at` (trigger-maintained). A
missing row is treated as "open" and is created on the first lock.

`period_lock_events`: the history, **append-only**. A trigger rejects every `UPDATE` and every
`DELETE` that somebody types (the foreign key's cascade when a practice is deleted passes). Checks:
`event_type IN ('locked', 'reopened')`; a reopening always has a reason; a `locked` event moves
forward (`locked_through` > `previous_locked_through`) and a `reopened` one moves back; `reason`
is 1-500 characters when present. The actor must be a member of the practice (a composite foreign
key to `organization_members`).

## For the next module that writes dated data

Call `period_locks.assert_open(session, organization_id, the_date, "time" | "expense" | "wip")`
(or `assert_open_many`) **inside the unit of work and before the first write**, for the date a
row will have and, when you move a row, for the date it had. Do not catch `PeriodLockedError`: let
it reach the client as the 409. Give new write paths a test in
`tests/test_period_lock_enforcement.py` (`set_lock` and `assert_locked` in
`tests/period_lock_helpers.py` make it three lines). WIP adjustments and transfers
(`wip.adjust`, `wip.transfer`) and any future writer of dated time, expense or WIP rows belong on
that list.
