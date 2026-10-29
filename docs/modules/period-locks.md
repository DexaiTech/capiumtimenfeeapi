# Period locks

A practice closes a period once its time, expenses and work in progress have been reported, so
nobody can change the figures behind a closed month by accident. A practice locks whole **weeks,
months, quarters or accounting years**, each on its own: **every day inside a locked period is
closed**, every other day is open. Nothing is locked until somebody locks a period, and any one
period can be unlocked again (with a reason) so that its data can be corrected.

How a practice locks is a setting (`PUT /period-locks/settings`, see
[Lock length and accounting year](#lock-length-and-accounting-year)): `weekly` until it chooses
otherwise. A new lock is exactly one period of that length, never a free range.

A lock closes three kinds of dated data, called *areas* in the API and in the error details:

| Area | What it covers |
|---|---|
| `time` | Time entries: logging, editing, moving, deleting, corrections, a timer that is stopped |
| `expense` | Disbursements and expense claim lines (record, edit, move, delete, submit, approve) |
| `wip` | WIP write-offs and their reversal, dated by the write-off date |

Reading is never blocked, and neither are invoices, credit notes and payments (see
[What is not blocked](#what-is-not-blocked)). The lock applies to everybody, the Owner included:
to change closed data, unlock the period first (it needs a reason and leaves a trail).

All paths are under `/api/v1/organizations/{organization_id}`.

## Endpoints

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/period-locks` | `period_lock.read` | The locked periods, the latest event and the `settings` |
| GET | `/period-locks/calendar` | `period_lock.read` | The periods of one accounting year (`?year=`, default this one), each `open`, `locked`, `blocked` or `future`, and the years that can be shown |
| PUT | `/period-locks/settings` | `period_lock.manage` | Choose the lock length (and the accounting year start) |
| GET | `/period-locks/history` | `period_lock.read` | Every lock and unlock, newest first, cursor-paginated (`limit` 1-100, default 25; `cursor`) |
| POST | `/period-locks` | `period_lock.manage` | Lock a week, month, quarter or year |
| POST | `/period-locks/reopen` | `period_lock.reopen` | Unlock one locked period; needs a reason |

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
  "periods": [
    {
      "id": "…", "period_type": "quarter",
      "start_date": "2026-01-01", "end_date": "2026-03-31", "label": "Q1 2026",
      "reason": "March VAT return filed",
      "locked_by": "…", "locked_by_name": "Ada Smith", "locked_at": "2026-04-02T09:30:12.512345Z"
    }
  ],
  "last_event": {
    "id": "…", "event_type": "locked",
    "period_type": "quarter", "period_start": "2026-01-01", "period_end": "2026-03-31",
    "period_label": "Q1 2026", "reason": "March VAT return filed",
    "actor_user_id": "…", "actor_name": "Ada Smith", "created_at": "2026-04-02T09:30:12.512345Z"
  }
}
```

`periods` is oldest first and empty when nothing is locked. `period_type` is `month`, `quarter` or
`year`; `legacy` marks a lock that was set through a free date before periods existed (migration
052 turned each into one period from 2000-01-01 through that date). A legacy period can be
unlocked, never created.

`PeriodLockEventRead` is one entry of `GET /period-locks/history` (`data` is a list, `meta` has
`next_cursor` and `has_more`): `event_type` is `locked` or `reopened` (an unlock); `period_*` say
which period; `reason` is optional for a lock and always present for an unlock. Events written
before periods existed have no `period_*` and carry the old single date in
`previous_locked_through` and `locked_through`.

`PeriodLockCreate` (`POST /period-locks`):

| Field | Rule |
|---|---|
| `period_type` | required: `month`, `quarter` or `year` |
| `year` | required, 2000-2100 |
| `period` | the month (1-12) or quarter (1-4); not used for a year. A month or quarter without it, or out of range, is 422 `PERIOD_LOCK_INVALID_PERIOD` |
| `reason` | optional, up to 500 characters; blank means none |

The period must **have ended**: its last day is not after today in the practice's time zone (422
`PERIOD_LOCK_IN_FUTURE`), and it must **not overlap** a period that is already locked (422
`PERIOD_LOCK_OVERLAPS`; unlock the other one first).

`PeriodLockReopen` (`POST /period-locks/reopen`):

| Field | Rule |
|---|---|
| `lock_id` | **required**: the `id` of the locked period to unlock (404 when it is not locked, or belongs to another practice). Required on purpose: an empty body never unlocks anything |
| `reason` | **required**, 1-500 characters (422 `PERIOD_LOCK_REASON_REQUIRED` when missing or blank) |

Both routes answer 200 with the new `PeriodLockRead`. Unknown fields are 422. A lock or an unlock
is one transaction: the period row, the history event and the audit entry are written together or
not at all.

## Error codes

| Status | Code | When |
|---|---|---|
| 409 | `PERIOD_LOCKED` | A write is dated inside a locked period (every enforced route, see below) |
| 422 | `PERIOD_LOCK_IN_FUTURE` | `POST /period-locks`: the period has not ended yet |
| 422 | `PERIOD_LOCK_OVERLAPS` | `POST /period-locks`: it overlaps a period that is already locked |
| 422 | `PERIOD_LOCK_INVALID_PERIOD` | `POST /period-locks`: a month or quarter without a valid number |
| 422 | `PERIOD_LOCK_REASON_REQUIRED` | `POST /period-locks/reopen`: no reason |
| 422 | `VALIDATION_ERROR` | Malformed body (an unknown `period_type`, a year out of range, a reason over 500 characters, a missing `lock_id`) |
| 403 | `PERMISSION_DENIED` | The caller lacks the permission of the route |
| 404 | `RESOURCE_NOT_FOUND` | Not a member of the practice, or `lock_id` is not a locked period |

The 422 business codes appear both as `error.code` and as `error.details[0].code`, with the
field that caused them (`period` or `reason`) in `details[0].field`. Dates
inside the **messages** are written the UK way (`31/03/2026`) because people read them; dates in
`details` are ISO 8601.

### The refusal of a write: 409 `PERIOD_LOCKED`

```json
{
  "success": false,
  "error": {
    "code": "PERIOD_LOCKED",
    "message": "Time dated 15/03/2026 cannot be changed: the period is locked (Q1 2026, through 31/03/2026). Unlock it in Settings > Period Lock to make changes.",
    "details": {"date": "2026-03-15", "locked_through": "2026-03-31", "area": "time"},
    "request_id": "…"
  }
}
```

`locked_through` is the last day of the locked period that covers the date. The message starts with `Time`, `An expense` or `A WIP movement` according to `area`. `date` is the
date that was refused: for a request that touches several dates (a bulk batch, a move from one
date to another, a claim with many lines) it is the **earliest** of them, and nothing at all is
written.

## Lock length and accounting year

- `organizations.period_lock_granularity` is `weekly` (the default), `monthly`, `quarterly` or
  `annually`. `PUT /period-locks/settings` takes `granularity` and, optionally,
  `financial_year_start_month` (1-12) and `financial_year_start_day` (1-28; 1 January, the calendar
  year, unless set). `period_lock_configured` is false until the first call: the screen asks once.
  Changing the setting later is audited (`period_lock.settings_changed`) and touches no lock.
- An accounting year is named by the calendar year it starts in. Months and quarters are counted from
  its first day; a week runs from the practice's `week_start` for seven days and belongs to the year
  it begins in, so a year has 52 or 53 weeks.
- `POST /period-locks` takes `period_type` (`week`, `month`, `quarter`, `year`), `year` (the
  accounting year) and `period` (the week 1-53, month 1-12 or quarter 1-4 of that year). The type must
  be the one the setting makes (422 `PERIOD_LOCK_WRONG_LENGTH`), so a lock is always exactly one period
  of the chosen length. `GET /period-locks/calendar` lists those periods, so a client never works dates out.
- Locks made under another setting, and `legacy` locks, stay in force and can be unlocked; they are
  listed apart from the grid. Practices that already had locks start `period_lock_configured` with the
  length they used last (migration 060).

## Locking and unlocking

- A week, month, quarter or accounting year is locked once it has ended ("today" is the practice's today,
  its `timezone`), and never twice: locking an overlapping period is 422 `PERIOD_LOCK_OVERLAPS`,
  so a double click does not write two history rows.
- Unlocking opens **only that period**; the others stay locked. It always says why. Unlocking a
  year that was locked as one year does not leave its months locked, and a quarter inside a locked
  year cannot be unlocked on its own: unlock the year and lock the quarters you want.
- Two requests at the same time are serialised on the practice's lock row (`FOR UPDATE`), so
  overlapping locks can never both be recorded.
- Every change appends an event to `period_lock_events` and writes an audit entry, `period_lock.locked`
  or `period_lock.reopened`, with `target_type` `period_lock` and this metadata:

  ```json
  {
    "event_type": "reopened", "period": "Q1 2026", "period_type": "quarter",
    "period_start": "2026-01-01", "period_end": "2026-03-31",
    "reason": "Late invoice from the supplier",
    "changes": {"period": ["Q1 2026", null]}
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
  `on_date` lies inside a locked period (`start_date <= on_date <= end_date`), and returns `None`
  otherwise. A practice with no locked period is open.
- `assert_open_many` does the same for a collection of days with **one** lookup (the lock row,
  then the periods); the error names the earliest locked day. An empty collection returns at once without touching the
  database.
- `locked_dates` never raises: it returns `{day: "Period locked until 31/03/2026"}` for each of
  `days` that lies in a locked period and leaves the other days out; an empty collection, or a
  practice with nothing locked, gives `{}`. The re-rating job uses it to skip locked time and
  to say why.
- Both `assert_*` functions **share-lock the practice's lock row** (`SELECT ... FOR SHARE`) and
  hold the lock until the transaction ends; the periods are read in a second statement, after the
  lock is held, so a write that waited sees the period that was just locked. A request that locks
  or unlocks takes the row
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

`locked_periods`: one row per locked period. Columns: `id`, `organization_id`, `period_type`
(`week`, `month`, `quarter`, `year`, `legacy`), `start_date`, `end_date` (both inclusive), `reason`,
`locked_by`, `locked_at`. `UNIQUE (organization_id, period_type, start_date)`; `start_date <=
end_date`. Unlocking deletes the row (the history keeps the trace). Rows go with their practice.

`period_locks`: one row per practice (`UNIQUE (organization_id)`), created with the practice. Since
migration 052 it is only the **serialisation point** that locks and writes contend on; its
`locked_through` column is unused (null).

`period_lock_events`: the history, **append-only**. A trigger rejects every `UPDATE` and every
`DELETE` that somebody types (the foreign key's cascade when a practice is deleted passes). Checks:
`event_type IN ('locked', 'reopened')`; a reopening always has a reason; `reason` is 1-500
characters when present; when `period_type` is set, `period_start` and `period_end` are too. The
actor must be a member of the practice (a composite foreign key to `organization_members`).

## For the next module that writes dated data

Call `period_locks.assert_open(session, organization_id, the_date, "time" | "expense" | "wip")`
(or `assert_open_many`) **inside the unit of work and before the first write**, for the date a
row will have and, when you move a row, for the date it had. Do not catch `PeriodLockedError`: let
it reach the client as the 409. Give new write paths a test in
`tests/test_period_lock_enforcement.py` (`set_lock` (locks everything through a day as
a `legacy` period) and `assert_locked` in `tests/period_lock_helpers.py` make it three lines). WIP adjustments and transfers
(`wip.adjust`, `wip.transfer`) and any future writer of dated time, expense or WIP rows belong on
that list.
