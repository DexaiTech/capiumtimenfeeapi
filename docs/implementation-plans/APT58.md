# APT58 – Search on the WIP Details grid: implementation plan

Status: **Implemented and deployed 2026-11-05 — waiting for the tester to verify.** See section 12.
Investigated on `origin/main` of both repos (API `capiumtimenfeeapi`, UI `capiumtimenfeeui`) on 2026-11-04.

## 1. Requirement understanding

- Ticket APT58 "Search tool doesn't work": *"Search tool does not work when searching the Job ID."*
  The original screenshot (02/11/2026) is **Work in Progress → WIP Details** (`/:orgSlug/wip/details`),
  search box "Description, client or job".
- First fix (02/11/2026, live since 03/11/2026 01:28): the item search also matches the job number and
  the client code (`Fix ticket APT58: WIP items search matches job number and client code`).
- Ticket reopened 04/11/2026 12:47 UTC with the comment: *"search is working only on first column, it
  should work on all columns of the grid"*. No screenshot, and the grid is not named.

**Assumption (to confirm with the client):** the grid is still WIP Details — the only grid on the ticket.
Since APT59 that page has two views: **By job** (default) and **By item**.

**Requirement as understood:** every column of the grid that shows text should be searchable from the
search box, in the view the user is on.

## 2. Existing behaviour (with code evidence)

### Flow

Search input → `useUrlSearch` → URL `?q=` → `filters.q` → API `q` → server-side filter → page of rows.

| Step | Where | What it does |
|---|---|---|
| Input | UI `src/features/wip/pages/WipDetailsPage.tsx:88`, `:253-257` | `useUrlSearch()`; placeholder "Client or job" (By job) / "Description, client or job" (By item) |
| Debounce/trim | UI `src/features/billing/hooks/useUrlSearch.ts:14-16` | trims the text, debounces it, writes `?q=` |
| Request | UI `WipDetailsPage.tsx:115`, `:119` (items), `:121-127` (jobs) | `q` sent to `GET /wip/items` (By item) or `GET /wip/summary?group_by=job` (By job) |
| API params | API `app/api/v1/wip_views.py:83-84` | `q`: max 100 chars, "Name or code contains this text" |
| Filter (By job) | API `app/services/wip_views.py:268-276` | groups kept when `q` is in `g.name`, `g.code` or `g.client_name` (lowercased, contains) |
| Group fields | API `app/services/wip_engine.py:125-132` | for `group_by=job`: `name` = job name (or "No job"), `code` = job number, `client_name` = client name |
| Filter (By item) | API `app/services/wip_views.py:339-349` | items kept when `q` is in description, client name, client code, job number or job name |
| Person names | API `wip_views.py` `items()` → `self.refs.member_names(..., page.items)` after `page_of` | names are looked up **after** filtering and paging, only for the rows of the page |

Searching is **server-side only** (no client-side filtering), applied **before** sorting and paging,
**case-insensitive** (`.lower()`), **partial** (`in`), on the trimmed text. Empty search sends no `q`
(all rows). Clearing the box removes `?q=`. Opening a job line clears the search
(`WipDetailsPage.tsx:180`, `onOpen: () => setSearchText('')`).

### Columns shown vs. columns searched

**By job** (`src/features/wip/components/jobLineColumns.tsx`):

| Column | Shows | Searched today |
|---|---|---|
| Client | client name + **client code** (`:33-41`) | name ✅, **code ❌** |
| Job | job number + name (`:44-62`) | ✅ both |
| Hours, Items, Value, Oldest item | numbers / date | ❌ |

**By item** (`src/features/wip/components/itemColumns.tsx`):

| Column | Shows | Searched today |
|---|---|---|
| Date | item date (`:38`) | ❌ |
| Client | name + code (`:46-47`) | ✅ both |
| Job | number + name (`:57-61`) | ✅ both |
| Item | kind badge ("Time", "Disbursement", …) + description + **supplier** (`:81-86`) | description ✅, **supplier ❌, kind ❌** |
| Person | person's name (`:103`) | **❌** |
| Hours, Rate, Value, Age | numbers (`:111`, `:123`, `:138`, `:155`) | ❌ |

## 3. Root cause (code evidence)

1. **The search is a fixed list of fields, not "all columns".** Both filters hard-code the fields they
   compare (`wip_views.py:268-276` and `:339-349`). Columns added to the grid later were never added
   to the search.
2. **By job:** the job line's client code is shown but not searched (`wip_views.py:273-275` checks
   `g.name`, `g.code`, `g.client_name` only; `g.code` is the job number for job lines).
3. **By item:** the Person, Supplier and item-kind values are shown but not searched. The person's name
   cannot be searched today because it is only looked up after paging, for the page's rows.
4. **Not supported by the code:** "only the first column" taken literally. In both views more than the
   first column is searched (Client and Job). The job number **is** found in both views since the first
   fix. So the client most likely searched a value in one of the unsearched columns (person, supplier,
   kind, client code on a job line, a date or an amount). **This needs one example from the client**
   (which view, what they typed, which row they expected) before we can say we reproduced it.
5. Not a cause: pagination (filter runs before paging), sorting (after filtering), case (lowercased),
   spaces (trimmed in the UI), nested properties (the API filters flat atom fields).

## 4. Affected files

API (behaviour):
- `app/services/wip_views.py` — `summary()` filter (`:268-276`), `items()` filter (`:339-349`).
- `app/api/v1/wip_views.py` — the `q` descriptions (`:83-84` and the items endpoint).
- `tests/test_wip.py` — search tests (`test_items_search_matches_job_number_and_client_code`, `:224`).
- `docs/modules/wip.md` — what `q` searches.

UI (text only):
- `src/features/wip/pages/WipDetailsPage.tsx:256` — the placeholder text.

## 5. Shared component impact

| Shared piece | Used by | Impact of the proposed change |
|---|---|---|
| `WipViewService.summary()` / `GET /wip/summary` | WIP Details "By job"; WIP Summary, WIP Ageing and Historic WIP through `src/features/wip/components/SummaryView.tsx:104` | Adding the client code for **job** groups only changes job-grouped searches: a job line is also found by its client's code. Client/partner/manager/service groups unchanged. |
| `WipViewService.items()` / `GET /wip/items` | WIP Details "By item"; `TransferDialog.tsx:103` (does **not** send `q`) | Only searches with `q` are affected, i.e. WIP Details "By item". |
| `useUrlSearch` | many Billing/WIP pages | **Not changed.** |
| Historic items `GET /reports/wip/items` (`wip_reports.py:164`) | Historic WIP items | **Not changed** (separate code path). |

No other screens change.

## 6. Proposed solution (smallest change)

API only, two filters, no new endpoints, no schema change:

1. **By item (`items()`):** also match
   - the **supplier** (`atom.supplier`),
   - the **kind label** as shown in the grid ("Time", "Disbursement", "Expense", "Fixed fee",
     "Adjustment" — same labels as the UI badge),
   - the **person's name**: look up names for the distinct `person_user_id`s of the filtered atoms
     **before** the text filter (one `member_names` call, as today, just earlier), and reuse that map
     for the rows of the page.
2. **By job (`summary()`):** also match `g.client_code` (the client code shown in the Client column).
3. **Dates and amounts:** *not* proposed by default (see risks). If the client confirms they want them,
   match the date as displayed (`dd/mm/yyyy`) — an extra string compare, no other change.
4. UI: change the placeholder to "Search the grid" (one string), no logic change.

Rejected: client-side filtering (would only search the loaded page and break with pagination);
a generic "search every field of the row" helper (touches shared code for one screen).

## 7. Potential risks

- **More matches:** a short term (e.g. "Ti") now also matches the "Time" kind on every time item. Expected
  for "search all columns", but worth telling the client.
- **Person lookup cost:** one extra names query per search over the practice's live WIP, only when `q`
  is set. The atoms are already in memory; the query is by distinct ids.
- **Summary search scope:** job-grouped search on WIP Summary/Ageing/Historic also finds by client code
  (intended, harmless).
- **Amounts/dates:** matching numbers as text (e.g. "50" in £150.00) gives surprising results — kept out
  unless asked.

## 8. Implementation steps

1. Write failing API tests first (`tests/test_wip.py`):
   - items: search by supplier, by person name ("Priya"), by kind label ("Disbursement"), mixed case,
     no match → empty list;
   - summary `group_by=job`: search by client code ("C0005") finds that client's job lines;
   - existing searches (description, client name/code, job number/name) still pass.
2. Change `items()` filter (move the names lookup before the filter) → verify: tests pass.
3. Change `summary()` filter (add `client_code`) → verify: tests pass, WIP Summary tests unchanged.
4. Update `q` descriptions and `docs/modules/wip.md`.
5. UI: placeholder text; update the WipDetailsPage test that asserts it.
6. Checks: ruff check, ruff format --check, mypy app; tsc -b, eslint, prettier, vitest (wip tests);
   CI pytest.

## 9. Testing strategy (to run after implementation — none run yet)

| # | Scenario | View | Expected |
|---|---|---|---|
| 1 | Exact job number `J00009` | both | that job's line / items only |
| 2 | Partial job number `00009`, `J0000` | both | matching jobs |
| 3 | Other columns: client name, client code `C0005`, job name, description, supplier, person `Priya`, kind `Disbursement` | By item / By job as applicable | matching rows |
| 4 | Upper/lower case `j00009`, `PRIYA` | both | same as exact |
| 5 | Leading/trailing spaces `  J00009 ` | both | same as exact (trimmed) |
| 6 | No match `zzzz` | both | empty state "Try a different search…" |
| 7 | Clear the search | both | all rows back, `?q=` removed |
| 8 | Pagination: search that matches > one page | By item | next pages contain only matches |
| 9 | Sorting with a search (Value, Date, Client) | both | matches only, in the chosen order |
| 10 | Regression: WIP Summary (Client/Job/Partner/Manager/Service tabs), WIP Ageing, Historic WIP search; Transfer dialog item list | — | unchanged except job tab also finds by client code |

## 10. Acceptance criteria

- In WIP Details **By item**, typing any text shown in the Client, Job, Item (kind, description,
  supplier) or Person column finds the row (case-insensitive, partial, trimmed).
- In WIP Details **By job**, typing the client name, client code, job number or job name finds the line.
- Job number search works in both views (APT58 original).
- Empty search and clearing behave as today; sorting and pagination work on the matched rows.
- WIP Summary, WIP Ageing, Historic WIP and the Transfer dialog behave as before (job tab: also by
  client code).
- Dates/amounts searchable **only if** the client confirms they want it.

## 11. Questions for the client (before implementing)

1. Which grid and which view (By job / By item) were you on, and what did you type that was not found?
2. Should dates and amounts be searchable too (e.g. "24/08/2026", "375")?

## 12. Outcome (2026-11-05)

Decisions (product owner): text columns only — dates and amounts are **not** searchable; the
client questions in section 11 were not needed to proceed.

Implemented as in section 6:

- API `app/services/wip_views.py`: By item also matches the supplier, the person's name (names
  looked up before the search) and the kind label (`KIND_LABELS`); By job also matches the client
  code. `q` descriptions and `docs/modules/wip.md` updated.
- UI `WipDetailsPage.tsx`: By item placeholder "Client, job, item or person".
- Tests: `test_items_search_matches_every_text_column`,
  `test_summary_by_job_search_matches_the_client_code` (both fail on the old code, pass now);
  `tests/test_wip.py` 15 passed, `test_wip_history_items.py` 2 passed, API CI green.
- Browser check on a local stack (not production): By item "Priya", "PRIYA", "Companies House",
  "disburse", "J00009", "c0005"; By job "C0005", "c0001", "J00031"; no match → "No items match";
  clearing restores all rows.
- Commits: API `ec7d88b` (PR #19), UI APT58 placeholder (PR #15). Deployed 2026-11-05.

Remaining: the tester's original search is unknown; ask them to repeat it on live.
