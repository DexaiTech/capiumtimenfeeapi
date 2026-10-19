# Central clients

The central client service is the Capium group's master list of clients. A practice **enables**
one of its clients to get a local client that follows the central name and address, links and
unlinks existing clients, and applies what happens to a client centrally (renamed, archived,
restored, merged into another). This document is the contract for the API and for the UI built on
it.

Code: `app/api/v1/{central_clients,integrations}.py` → `app/services/{central_sync,central_events,
client_merge,integration_logs}.py` and the gateway package `app/services/central_clients/`
→ `app/repositories/{central_clients,client_merge,integration_logs}.py`; schemas in
`app/schemas/{central_clients,integrations}.py`; models `app/db/models/integrations.py` and the
`central_*`/`merged_into_client_id` columns of `clients`. Migration `016`.
Tests: `tests/test_central_{clients,sync,events,migrations,openapi}.py`,
`tests/test_client_merge.py` (helpers `tests/central_helpers.py`, `tests/merge_helpers.py`).

All paths are under `/api/v1/organizations/{organization_id}`. Responses use the standard
envelope; another practice's ids answer 404. Related: `docs/modules/clients.md` (the client
shapes), `docs/domain.md` ("Central clients").

## The gateway

The API talks to the central service only through one interface
(`app/services/central_clients/base.py`), chosen by `CENTRAL_CLIENT_GATEWAY` (`mock`, the default
and so far the only one; see `factory.py`):

```python
class CentralClientGateway(Protocol):
    name: str

    async def search(self, q: str, limit: int) -> list[CentralClient]: ...  # blank q: all, by name
    async def get(self, central_id: str) -> CentralClient | None: ...  # None: unknown
    async def changes_since(self, cursor: str | None, limit: int = 100) -> CentralChanges: ...
```

Every call raises `CentralUnavailableError` when the service cannot be used; the callers then
fall back on the cache (below). `CentralClient` is a frozen dataclass: `central_id`, `name`,
`company_number`, `address_line1`, `address_line2`, `city`, `postcode`, `country` (ISO alpha-2),
`status` (`active` | `archived`), `updated_at`. `CentralClientEvent`: `event_id` (unique and
stable), `type` (`renamed` | `archived` | `restored` | `merged`), `central_id`, `occurred_at`,
the optional `name`, address fields and `country` of a rename (`None` leaves a field alone, `""`
clears an address field) and, for a merge, `into_central_id`. `CentralChanges`: `events`
(oldest first), `cursor` (pass it to the next call), `has_more`.

A real gateway is one more class with these three methods; nothing else changes.

### The mock

`MockCentralClientGateway` (`mock.py`) is deterministic and lives in the API process (a restart
resets it; it is for development and demonstrations, never for production):

* **25 clients** `CC-0001` … `CC-0025`: limited companies, LLPs and sole traders in Leeds,
  Nottingham, Halifax, Derby, Manchester, Edinburgh, Cardiff, Reading, Chelmsford, Birmingham,
  Wakefield, Salisbury, Belfast, Doncaster, Aberdeen, London, Swindon, Truro, Bristol, Norwich,
  Liverpool, Stoke-on-Trent, Bolton, Newcastle and Southampton. Company numbers are unique
  (`09231457`, `OC412345`, `SC654321`, `NI123456`, …); three sole traders/partnerships have none
  (`CC-0009`, `CC-0010`, `CC-0016`). `CC-0001` is *Northgate Bakery Ltd*, `CC-0002` *Brightwater
  Plumbing & Heating Ltd*, `CC-0004` *Ashby & Partners LLP*. `CC-0023` (*Old Mill Printers Ltd*)
  is **archived** centrally.
* **An outage switch** (`POST /central-clients/mock/availability`): every call then fails as if
  the service were down.
* **A change feed**: `POST /central-clients/mock/changes` renames, archives, restores or merges a
  client of the catalogue and appends the event to the feed, which `process-changes` reads
  (event ids `mock-evt-000001`, …).
* Tests get a clean mock for every test through the `central_gateway` fixture
  (`tests/conftest.py`): `central_gateway.rename(...)`, `.archive(...)`, `.restore(...)`,
  `.merge(...)`, `.set_available(False)`, `.reset()`.

The mock endpoints answer 404 unless the gateway is the mock **and** `APP_ENV` is not
`production`.

## The cache and the outage rule

`central_client_cache` keeps, per practice, the last known state of every central client the
practice has seen: every live search result and every central `get` (enabling, linking,
synchronising) refreshes it (`fetched_at`), and an event updates the rows it has. While the central service is down:

* **search** serves the cache (`source: "cache"`, `stale: true`); with nothing cached yet it
  answers 503 `CENTRAL_UNAVAILABLE` (header `Retry-After: 30`);
* **enable** and **link** use the cached copy (`from_cache: true`; the client's
  `central_sync_state` is `stale` until the next synchronisation); a central client that was
  never cached answers 503;
* **synchronise** keeps the stored name and address, sets `central_sync_state = "stale"` and
  logs the failure, still **200**;
* **process-changes** answers 200 with `available: false`.

Nothing here ever answers 5xx because the central service is down, except the two 503s above.

## Endpoints

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/central-clients?q=&limit=` | `client.create` | Search the central service (`q` up to 200 characters, name or company number; `limit` 1–50, default 20). |
| POST | `/central-clients/{central_id}/enable` | `client.create` | Enable a central client: 201 `created`, 200 `linked_existing` / `already_enabled`. |
| POST | `/clients/{client_id}/central-link` | `client.create` | Link an existing client: `{central_id}`. |
| DELETE | `/clients/{client_id}/central-link` | `client.create` | Unlink: the client becomes local. |
| POST | `/clients/{client_id}/central-sync` | `client.update` | Pull the name and address of one linked client. |
| POST | `/clients/central-sync` | `client.update` | The same for every linked, non-archived client (at most 200 per call). |
| POST | `/central-clients/events` | `settings.manage` | Receive one central event. |
| POST | `/central-clients/process-changes` | `settings.manage` | Pull the change feed and apply it (at most 100 events per call). |
| GET / POST | `/central-clients/mock/availability` | `settings.manage` | Read / switch the mock outage (404 unless the mock, never in production). |
| POST | `/central-clients/mock/changes` | `settings.manage` | Push a change into the mock and its feed (201). |
| GET | `/integrations/sync-logs?integration=&status=&entity_id=&limit=&cursor=` | `settings.manage` | The sync log, newest first, keyset-paginated. |

`{central_id}` is the central service's id: 1–64 characters of `A-Za-z0-9._:-`, starting with a
letter or digit (422 otherwise). No new permission codes were added: nothing to migrate for
roles, no change to the UI permission constants.

### Search: `GET /central-clients`

```json
{
  "success": true,
  "data": {
    "source": "live",              // "cache" while the central service is down
    "stale": false,                // true: the items may be out of date
    "items": [{
      "central_id": "CC-0001",
      "name": "Northgate Bakery Ltd",
      "company_number": "09231457",        // normalised: upper case, no spaces; null for sole traders
      "address_line1": "12 Northgate Street", "address_line2": null,
      "city": "Leeds", "postcode": "LS1 4AB", "country": "GB",
      "status": "active",                  // "archived": it cannot be enabled or linked
      "updated_at": "2025-01-06T09:00:00Z",   // when the central service last changed it
      "fetched_at": "2026-10-18T10:15:00Z",   // when this practice's cache last heard of it
      "enabled": false,                    // a live client of the practice is linked to it
      "client_id": null, "client_code": null,
      "matching_client": null              // {id, code, name}: a client that is not linked but has
    }]                                     // the same company number; enabling links *it*
  }
}
```

A blank `q` lists the first `limit` clients by name. A name matches as a case-insensitive
substring; a company number as a substring of the normalised number (from three characters:
`oc 4123` finds `OC412345`).

### Enable: `POST /central-clients/{central_id}/enable`

No body. The response is `data: {result, from_cache, client}` where `client` is the full
**ClientDetail** (`docs/modules/clients.md`):

| `result` | Status | What happened |
|---|---|---|
| `created` | 201 | A new client: code from the `client` sequence, `name`, address, `country` and `company_number` from the central service, status `active`, `client_type` `limited_company` when there is a company number else `sole_trader`, `central_client_id`, `central_sync_state` `synced` (`stale` when `from_cache`). |
| `linked_existing` | 200 | A client of the practice that was not linked and had the same company number (ignoring case, spaces and leading zeros) was **adopted**: linked, and its name and address replaced by the central values (the audit entry lists the changes). Everything else on the client stays. When several clients share the number the live one wins over an archived one, then the oldest. |
| `already_enabled` | 200 | A client of the practice is already linked to it: that client is returned. This also covers a client that was merged away (see Merge). |

Never a duplicate: two requests enabling the same central client at once create **one** client
(the other answers `already_enabled`). Errors: 404 (the central service does not know the id),
409 `CENTRAL_CLIENT_ARCHIVED`, 503 `CENTRAL_UNAVAILABLE`, 422 (malformed id).

### Link and unlink

`POST /clients/{client_id}/central-link` with `{"central_id": "CC-0001"}` links a client of the
practice: it takes the central name and address (which are then managed centrally), keeps its
own company number or takes the central one when it has none, and returns the **ClientDetail**.
Errors: 409 `CENTRAL_CLIENT_ALREADY_LINKED` (another client of the practice uses that central
client; `details: {client_id, client_code}`), 409 `CLIENT_ALREADY_LINKED` (this client is linked:
unlink it first; `details: {central_id}`), 409 `CLIENT_MERGED`, 409 `CENTRAL_CLIENT_ARCHIVED`,
404 (client or central client), 503 `CENTRAL_UNAVAILABLE` (down and not cached).

`DELETE /clients/{client_id}/central-link` makes the client local again: `central_client_id`
and `central_synced_at` become null, `central_sync_state` `local`, and its name and address
become editable (the values stay). 409 `CLIENT_NOT_LINKED` for a client that is not linked.
Enabling the same central client again afterwards links the same client again
(`linked_existing`), so unlinking never leads to a duplicate.

### Synchronise

`POST /clients/{client_id}/central-sync` → `data`:

```json
{
  "client_id": "…", "code": "C0001", "name": "Northgate Bakery & Cafe Ltd", "central_id": "CC-0001",
  "state": "synced",                  // "synced" | "stale" (central down) | "failed" (central no longer knows it)
  "changed": true,
  "changes": {"name": ["Northgate Bakery Ltd", "Northgate Bakery & Cafe Ltd"], "city": ["Leeds", "York"]},
  "from_cache": false,                // true when the central service was unavailable
  "message": "Name and address updated.",
  "synced_at": "2026-10-18T10:16:00Z"
}
```

`changes` lists `{field: [old, new]}` for the name, the address lines, city, postcode and
country. The messages are `Name and address updated.`, `Already up to date.`, `The central
client service is unavailable: the stored name and address were kept.` (`stale`) and `The central
service no longer knows this client.` (`failed`). 409 `CLIENT_NOT_LINKED`, 409 `CLIENT_MERGED`.
A synchronisation that finds the client unlinked or re-linked meanwhile answers 409
`CENTRAL_LINK_CHANGED` (try again).

`POST /clients/central-sync` handles the practice's linked clients that are not archived or
merged away, **at most 200 per call, the longest unsynchronised first**:

```json
{ "available": true, "processed": 2, "synced": 2, "stale": 0, "failed": 0, "changed": 1,
  "remaining": 0,                     // linked clients left for another call
  "results": [ /* one entry per client, as above */ ] }
```

When the central service fails once, the remaining clients are marked `stale` without more
calls and `available` is `false`.

### Events

`POST /central-clients/events` receives **one** event:

```json
{ "event_id": "evt-100", "type": "renamed", "central_id": "CC-0001",
  "name": "Northgate Bakehouse Ltd", "city": "York", "address_line2": "" }
```

`event_id` (1–100 characters) is required; `type` is `renamed`, `archived`, `restored` or
`merged`; `occurred_at` is optional. A rename carries at least one of `name`, `address_line1`,
`address_line2`, `city`, `postcode`, `country` (a field left out is left alone, `""` clears an
address line; the name and the country cannot be cleared). A merge carries `into_central_id`
(not the same as `central_id`); no other type may. 422 otherwise.

`data`: `{event_id, type, central_id, outcome, duplicate, message, client_id}` with `outcome`
`applied` or `skipped` and `client_id` the client the event changed (the **surviving** client
for a merge):

| Event | Applied when | Skipped (200, `message`) |
|---|---|---|
| `renamed` | the linked client takes the fields the event carries; `central_sync_state` becomes `synced`; the cache follows | not enabled here (`This central client is not enabled in this practice.`); merged away; nothing new (`Already up to date.`) |
| `archived` | the linked client's status becomes `archived` | not enabled; merged away; `The client is already archived.` |
| `restored` | the linked client's status becomes `active` (also after the client was archived by hand) | not enabled; merged away; `The client is not archived.` |
| `merged` | see Merge | neither enabled; only the survivor enabled (`The merged central client is not enabled in this practice.`); already merged; the survivor is missing |

Events for a central client that is not enabled here are **never errors**. An event id is
processed **once**: a repeat (the same id again, even with other content, even at the same
moment) answers 200 `duplicate: true`, `outcome: "skipped"`, changes nothing and is logged
`skipped`. A delivery that failed (500) is not recorded as processed: send it again. Handlers
run **as the system**: their audit entries have `actor_user_id` null; the person who made the
call, the event id and the source (`webhook` for this endpoint, `feed` for `process-changes`)
are in the metadata.

`POST /central-clients/process-changes` reads the events after this practice's cursor
(`integration_cursors`, one per practice) and applies each through the **same handler**:

```json
{ "available": true, "fetched": 3, "applied": 2, "skipped": 1, "failed": 0,
  "has_more": false,      // more events are waiting: call again
  "cursor": "3",          // where this practice has read the feed up to
  "events": [ /* one CentralEventResult per event */ ] }
```

The cursor moves forward only when the whole batch was handled. When an event fails the batch
stops (`failed: 1`, `has_more: true`, the cursor stays); the next call reads the same events
again and the ones that went through are skipped as repeats. While the central service is down
the answer is `available: false` and the failure is logged.

### Mock endpoints

`GET /central-clients/mock/availability` → `{available, gateway: "mock"}`; `POST` the same URL with
`{"available": false}` switches the outage on (the switch is process-wide). `POST
/central-clients/mock/changes` takes the event body above without `event_id` (and without
`occurred_at`), changes the catalogue and returns `201 {event_id, type, central_id}`; 404 for an
unknown central id. All three answer 404 outside the mock or in production.

## Linked clients

A client with `central_client_id` set is **linked**. Every client response (`ClientRead`,
`ClientListItem`, `ClientDetail`) carries:

| Field | Meaning |
|---|---|
| `central_client_id` | The linked central client, null for a client created here. |
| `central_sync_state` | `local` (not linked), `synced`, `stale`, `failed`. |
| `central_synced_at` | When the name and address were last confirmed against the central service. |
| `merged_into_client_id` | Set on a client that was merged away: the client that now holds its work. |

`ClientDetail` adds `merged_into: {id, code, name} | null`. The client list takes
`central=linked|local`, the summary has `by_central: {linked, local}`.

**`PATCH /clients/{id}` on a linked client:** changing `name`, `address_line1`,
`address_line2`, `city`, `postcode` or `country` answers **409 `CLIENT_FIELD_MANAGED_CENTRALLY`**
with `details: [{field, code: "managed_centrally", message}]` for every managed field that
would change (nothing is saved); sending the value it already has is fine. Every other field
stays editable. After unlinking, all of them are editable again. The central fields cannot be set
on create or update (422).

## Merge

The central service says client **A** was merged into client **B** (`type: "merged"`,
`central_id` = A, `into_central_id` = B).

* **Both enabled here:** A's work moves to B in **one transaction**, with both client rows locked
  (concurrent merges in either direction queue up and exactly one applies). A is archived and
  gets `merged_into_client_id = B`; it is never deleted, so the history that names it stays valid.
* **Only A enabled here:** A is re-linked to B's central id and takes the central values the
  event carries (else the cached ones; `central_sync_state` `stale` until the next
  synchronisation); nothing moves.
* **Only B, or neither:** skipped.
* If B was itself merged away earlier, the work goes to the client B points at (chains are
  followed); every client already merged into A is re-pointed at the survivor.
* **Rows re-pointed** (every row of the source client moves, with its counts in the audit entry):
  `jobs`, `time_entries`, `timers`, `disbursements`, `wip_write_offs`, `invoices`, `credit_notes`,
  `payments`, `payment_allocations` (together, in one statement: their composite foreign keys
  carry the client twice), and, with a rule for what cannot simply move, `client_contacts`,
  `client_services` and `rate_rules`.
* **Same thing on both: the survivor's wins**, and the source's copy stays on the archived
  client and is listed in `left_behind`:
  * a **service** B takes already → B's agreement (fee, frequency, dates) stands;
  * a **rate rule** whose period overlaps a rule of the same kind and scope on B (two such rules
    cannot coexist) → B's rate stands;
  * a **primary contact** when B has one → it moves as an ordinary contact (`contacts_demoted`).
* **History is not moved:** `rate_changes` and `rate_rerun_items` keep the client they were
  written for; each rate rule that moved gets one new `rate_changes` row (`action: updated`,
  `source: client_merge`, old and new `client_id`).
* **WIP, invoices, balances** are simply the survivor's now: the overview and every screen read
  by `client_id`.
* A test (`tests/test_client_merge.py`) discovers every table that refers to a client from the
  model metadata and fails when a table is not handled in `app/repositories/client_merge.py`
  (`REPOINTED`, `CONFLICT_RULES` or `HISTORY`), so a later module cannot be forgotten.

A merged client ignores later events (`This client was merged into another client.`), is left out
of the bulk synchronisation, answers 409 `CLIENT_MERGED` to a single synchronisation, and still
carries its `central_client_id`, so enabling its central id again answers `already_enabled` with
that archived client.

The audit entry `client.central_merged` belongs to the **surviving** client (its `target_id`):

```json
{ "mode": "merged", "event_id": "…", "source": "webhook", "requested_by": "<user id>",
  "central_id": "CC-0001", "client_id": "<survivor id>",
  "merged_client": {"id": "…", "code": "C0001", "name": "…", "central_id": "CC-0001"},
  "survivor":      {"id": "…", "code": "C0002", "name": "…", "central_id": "CC-0002"},
  "counts": {"jobs": 2, "time_entries": 3, "timers": 1, "disbursements": 2, "wip_write_offs": 1,
             "invoices": 2, "credit_notes": 1, "payments": 2, "payment_allocations": 1,
             "client_contacts": 2, "client_services": 2, "rate_rules": 2},   // every table, zero or not
  "left_behind": {"client_services": [{"id": "…", "service_code": "VAT", "service_name": "VAT return", "fee": 15000, "frequency": "quarterly", …}],
                  "rate_rules": [{"id": "…", "kind": "charge", "rate": 6000, "effective_from": "2026-03-01", "effective_to": "2026-12-31", …}]},
  "contacts_demoted": 1, "merged_clients_repointed": 0, "rate_history_rows": 2 }
```

The relink variant has `mode: "relinked"`, `from_central_id`, `to_central_id` and `changes`.

## The sync log

`GET /integrations/sync-logs` lists one row per synchronisation attempt of the practice,
newest first (`limit` 1–100, default 25; `meta.next_cursor`, `meta.has_more`). Filters:
`integration` (`central_client` today; the other integrations reuse the table),
`status` (`ok` | `failed` | `skipped`) and `entity_id` (the local record, a client id).

```json
{ "id": "…", "integration": "central_client", "direction": "inbound",
  "operation": "sync",            // enable | link | unlink | sync | renamed | archived | restored | merged | poll
  "entity_type": "client", "entity_id": "<client id>", "external_id": "CC-0001",
  "external_event_id": null,      // set on the row that recorded an event as processed
  "status": "failed", "message": "C0001: The central client service is unavailable: …",
  "attempt": 2,                   // 1, then one more for each failed attempt in a row
  "details": {"central_id": "CC-0001", "state": "stale", "changes": {}},
  "created_at": "2026-10-18T10:17:00Z" }
```

What is logged: every enable, link, unlink and synchronisation (`ok`, or `failed` with the
reason: central down, no longer known), every event (`ok` applied, `skipped` nothing to do or a
repeat), every feed poll (`poll`: how many events were applied and skipped, `failed` when the
service was down or a handler failed) and every failed handler (its own transaction, so the row
survives the rollback). A client that is deleted keeps its log rows. The unique index on
`(organization_id, integration, external_event_id)` makes a second delivery of one event a
no-op even when two arrive at once; failed rows and repeats leave `external_event_id` null so a
retry can still be recorded.

## Audit actions

`client.central_enabled`, `client.central_linked`, `client.central_unlinked`,
`client.central_synced` (written when something changed or the state changed or is not
`synced`),
`client.central_renamed`, `client.central_archived`, `client.central_restored`,
`client.central_merged` — target `client`, with `code`, `name`, `central_id` and the details
above (`changes: {field: [old, new]}`, `from_cache`, `result`). The last four come from events and
have no actor. The audit entries of payments, credit notes, disbursements and WIP write-offs
now also carry `client_id`, so they appear in the client's activity.

## Error codes

| Code | Status | When |
|---|---|---|
| `CENTRAL_UNAVAILABLE` | 503 | The central service is down and the answer is not cached (search, enable, link). `Retry-After: 30`. |
| `CENTRAL_CLIENT_ARCHIVED` | 409 | Enabling or linking a central client that is archived centrally. |
| `CENTRAL_CLIENT_ALREADY_LINKED` | 409 | Another client of the practice is linked to that central client (`details`: `client_id`, `client_code`). |
| `CLIENT_ALREADY_LINKED` | 409 | The client is already linked (`details`: `central_id`): unlink it first. |
| `CLIENT_NOT_LINKED` | 409 | Synchronising or unlinking a client that is not linked. |
| `CLIENT_MERGED` | 409 | Synchronising or linking a client that was merged away. |
| `CENTRAL_LINK_CHANGED` | 409 | The client's link changed during a synchronisation: try again. |
| `CLIENT_FIELD_MANAGED_CENTRALLY` | 409 | Changing the name or address of a linked client. |
| `CENTRAL_CLIENT_ALREADY_ENABLED` | 422 (a row error of `IMPORT_INVALID`, or in a dry run) | A client import row whose company number belongs to an enabled central client (its own or its central one). |
| `RESOURCE_NOT_FOUND` | 404 | Unknown central id, client, or another practice's client; also the mock endpoints outside the mock or in production. |
| `PERMISSION_DENIED` | 403 | The permission in the table above is missing. |
| `VALIDATION_ERROR` | 422 | A malformed central id, event or body (`details: [{field, message}]`). |

## Notes for the UI

* **Enable flow:** search (`GET /central-clients?q=`), show `enabled` / `matching_client` /
  `status: "archived"` on each row, call enable, then open the returned client; 201 means
  created, 200 `linked_existing` should say "linked to your existing client".
* Show the state of a linked client from `central_sync_state` (`stale`: "central details last
  confirmed `central_synced_at`"; `failed`: "no longer found centrally") and offer "Synchronise"
  (`client.update`). Disable the name and address inputs of a linked client and say why; the
  409 above is the backstop.
* While the service is down `search` answers `source: "cache"`, `stale: true`: show a banner.
  The mock switch lets a demonstration show it.
* A merged-away client (`merged_into_client_id`) is read-only history: link to `merged_into`.
* CSV export and import are unchanged: the export columns are not the full client (no address,
  tax references or billing settings), so the new fields are not added to them.

## Limits and known gaps

* There is no real gateway yet, and the mock's state (outage switch, pushed events) lives in the
  API process: a restart resets it, and several API processes would each have their own.
* The bulk synchronisation handles 200 clients per call and a feed poll 100 events per call; both
  say when to call again.
* The change feed is pulled on request (`process-changes`); nothing schedules it. A scheduled call
  (or a real webhook calling `POST /central-clients/events`) is the integration's job.
* A merge does not renumber anything: the survivor keeps its code, and the merged client keeps its
  own code, archived.
