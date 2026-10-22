# Tickets (Application Testing): tickets, tags, comments, files and the dashboard

Code: `app/api/v1/{tickets,ticket_tags}.py` → `app/services/{tickets,ticket_reader,ticket_comments,ticket_attachments,attachment_files,ticket_tags,ticket_dashboard,storage}.py`
→ `app/repositories/{tickets,ticket_files,ticket_tags,ticket_dashboard}.py`; `app/utils/multipart.py` (the upload parser);
schemas in `app/schemas/{tickets,ticket_dashboard}.py`; models `app/db/models/tickets.py`.
Tests: `tests/test_tickets.py`, `test_tickets_list.py`, `test_tickets_bulk.py`, `test_ticket_tags.py`, `test_ticket_comments.py`,
`test_ticket_attachments.py`, `test_ticket_upload_security.py`, `test_upload_parsing.py`, `test_file_storage.py`,
`test_ticket_dashboard.py`, `test_tickets_openapi.py`, `test_ticket_migration.py` (helpers in `tests/ticket_helpers.py`).
Migration: `015` (tables `ticket_tags`, `tickets`, `ticket_tag_links`, `ticket_comments`, `ticket_attachments`,
`ticket_status_history`; the `ticket` number sequence; the default tags; four permissions).
**Permissions added**: `ticket.read`, `ticket.create`, `ticket.update`, `ticket.delete` (tags and comment/file moderation
only — see [Permissions](#permissions)). The UI must add the four codes to `src/constants/permissions.ts` and to
`OWNER_PERMISSIONS` in `src/test/orgFixtures.ts`.

"Application Testing" is the practice's own tracker for bugs and enhancements found while using the application: its people raise
tickets, tag them, assign them, comment on them and attach screenshots and documents. Everything is scoped to one practice.

All paths are under `/api/v1/organizations/{organization_id}`. Another practice's ids answer 404, never 403. The rules behind the
endpoints are in `docs/domain.md` (Tickets). The interactive schema is `/openapi.json`; every route there has a summary, a
description and its error responses.

## Conventions

| Thing | Representation |
|---|---|
| Envelope | Success `{"success": true, "data": …, "message": "…" or null, "meta": … or null}`. Error `{"success": false, "error": {"code", "message", "details", "request_id"}}`. |
| Authentication | `Authorization: Bearer <access token>` on **every** call, file downloads included (see [Downloading and showing a file](#downloading-and-showing-a-file)). |
| `error.details` | 422: a list of `{field, code, message}`; `field` is the request field (`subject`, `tag_ids`, `assignee_id`, `value`, `file`, `comment_id`, `attachment_ids`, `body`, `cursor`, …). A **query parameter** that fails the schema check (`limit=0`, `status=foo`, `date_from=abc`) is named `query.<param>` (`query.limit`, `query.status`); one that breaks a rule of the service (a start after the end, `unassigned` with `assignee_id`, a cursor of another sort) is named by its plain name. 409 duplicates: `[{field, code: "duplicate", message}]`. Other errors: an object or `null`, listed with each [error code](#error-codes). |
| Lists | `GET /tickets` and `GET /tickets/{id}/comments` are cursor-paginated: query `limit` (1–100, default 25) and `cursor`; response `meta {next_cursor, has_more}`. Tags, a ticket's files and the dashboard are bounded and not paginated. |
| Ids | UUIDs. A ticket also has a `number` and a `code` (`APT271`): show the code, address the ticket by `id`. |
| Timestamps and days | Timestamps are UTC (`2026-10-18T02:53:47.804669Z`). Day filters (`created_from`, `created_to`, the dashboard range) are the practice's calendar days in its time zone. |
| Write responses | Creates answer 201 with the record, updates 200 with the record, deletes 200 with `data: null` (tag: `{id, tickets_untagged}`), bulk 200 with `{action, affected, changed}`. `message` is a short sentence (`Ticket created`). |
| Request bodies | JSON objects; unknown fields answer 422. Strings are trimmed. PATCH bodies hold any subset of the fields. An upload is `multipart/form-data`. |

## Endpoint index

| Method | Path | Permission |
|---|---|---|
| GET | `/tickets/dashboard` | `ticket.read` |
| POST | `/tickets/bulk` | `ticket.update` |
| GET | `/tickets` | `ticket.read` |
| POST | `/tickets` | `ticket.create` |
| GET | `/tickets/{ticket_id}` | `ticket.read` |
| PATCH | `/tickets/{ticket_id}` | `ticket.update` |
| GET | `/tickets/{ticket_id}/comments` | `ticket.read` |
| POST | `/tickets/{ticket_id}/comments` | `ticket.update` |
| PATCH | `/tickets/{ticket_id}/comments/{comment_id}` | `ticket.update`, and the caller is the author |
| DELETE | `/tickets/{ticket_id}/comments/{comment_id}` | the author with `ticket.update`, or anyone with `ticket.delete` |
| GET | `/tickets/{ticket_id}/attachments` | `ticket.read` |
| POST | `/tickets/{ticket_id}/attachments` | `ticket.update` (a file on someone else's comment: `ticket.delete`) |
| GET | `/tickets/{ticket_id}/attachments/{attachment_id}` | `ticket.read` |
| DELETE | `/tickets/{ticket_id}/attachments/{attachment_id}` | the uploader with `ticket.update`, or anyone with `ticket.delete` |
| GET | `/ticket-tags` | `ticket.read` |
| POST | `/ticket-tags` | `ticket.update` |
| PATCH | `/ticket-tags/{tag_id}` | `ticket.update` |
| DELETE | `/ticket-tags/{tag_id}` | `ticket.delete` |
| GET, PATCH | `/sequences`, `/sequences/ticket` (existing endpoints) | any member, `organization.update` (the `ticket` kind: prefix `APT`, padding 1; forward-only) |

`/tickets/dashboard` and `/tickets/bulk` are declared before `/tickets/{ticket_id}`, so those words are never read as an id.

## Vocabulary

| Thing | Values |
|---|---|
| Status (`status`) | `open` (Open), `hold` (Hold), `in_progress` (In Progress), `reopen` (Reopen), `closed` (Closed). Default `open`. Any status may follow any other: there is no workflow. Lists and charts use this order. |
| Priority (`priority`) | `high` (High), `medium` (Medium), `low` (Low), `not_set` (Not Set). Default `not_set`. |
| Category (`category`) | `application` (the badge "Application"). The only value today; every ticket has it. |
| Tag colour (`color`) | `violet`, `blue`, `green`, `red`, `orange`, `grey` (default `grey`). Names of design-system tones: map each to the badge tone of the same name. The API carries no colour for status or priority: the UI chooses those tones. |
| Default tags | Every practice starts with `Bug` (red), `Enhancement` (violet), `UI` (blue), `Reporting` (green), `Accounts` (blue), `Validation` (red), `Trial Balance` (violet). Anyone with `ticket.update` may add, rename, recolour them; `ticket.delete` removes them. |

## Limits

| What | Limit |
|---|---|
| Subject | 1–200 characters after trimming; runs of white space (also line breaks, tabs) become one space; control characters are refused |
| Description | at most 10,000 characters; line breaks are kept (`\r\n` → `\n`); blank becomes `null`; other control characters are refused |
| Comment | at most 5,000 characters, same rules as the description; may be empty only with files |
| Tags | name 1–40 characters, unique in the practice ignoring case; at most 200 tags per practice; at most 20 per ticket |
| Bulk | 1–200 distinct ticket ids per call |
| List | `limit` 1–100 (default 25); `q` at most 200 characters; `status` at most 5 values, `priority` at most 4, `tag_id` at most 50 |
| File | at most **2 MB** (2,097,152 bytes) per file; at most 25 files per ticket; `file_name` is cut to 150 characters (the extension is kept) |
| Dashboard | a range of at most 366 days; 7 tags (+ `Others`), 10 assignees (+ `Unassigned`), 7 recent tickets, 14 trend days |
| Description excerpt | the list shows the first 200 characters of the description |

---

# The ticket

## Objects

`TicketListItem` (every item of `GET /tickets`, and of `recent` on the dashboard):

```json
{
  "id": "01a14ced-fd9d-70a8-b218-74162d54b125",
  "code": "APT1",
  "number": 1,
  "category": "application",
  "subject": "Trial balance does not agree",
  "status": "in_progress",
  "priority": "high",
  "tags": [
    {"id": "01a14ced-fb43-726c-9041-6eaefccfa32c", "name": "Accounts", "color": "blue"},
    {"id": "01a14ced-fb43-726c-9041-6eaa007888a8", "name": "Bug", "color": "red"}
  ],
  "assignee": {"id": "01a14ced-fa33-77b2-9492-dd63f257b501", "full_name": "User 001"},
  "created_by": "01a14ced-fa33-77b2-9492-dd63f257b501",
  "created_by_name": "User 001",
  "created_at": "2026-10-18T02:53:47.804669Z",
  "updated_at": "2026-10-18T02:53:47.804669Z",
  "closed_at": null,
  "comment_count": 1,
  "attachment_count": 1,
  "description_excerpt": "The trial balance for year end shows a difference of GBP 12.50.\nSee the attached screenshot."
}
```

| Field | Type | Meaning |
|---|---|---|
| `id` | uuid | |
| `code` | string | `APT271`: the practice's prefix and the number at the moment of creation. Changing the prefix in Settings does not rename old tickets. |
| `number` | int | The sequence number (271). Sort by it. |
| `category` | `"application"` | |
| `subject` | string | |
| `status`, `priority` | enums | See [Vocabulary](#vocabulary). |
| `tags` | `{id, name, color}[]` | Sorted by name (ignoring case). `[]` when none. |
| `assignee` | `{id, full_name}` or null | An active member when assigned; the name stays after the person leaves. |
| `created_by`, `created_by_name` | uuid, string or null | Who raised it. The person stays a member of the practice (a member that tickets refer to cannot be deleted), so the name is always there; the type allows null. |
| `created_at`, `updated_at` | timestamp | `updated_at` equals `created_at` on a new ticket and moves when the subject, description, status, priority or assignee change. Changing tags, comments or files does not touch it. |
| `closed_at` | timestamp or null | Set when the status becomes `closed`, null again when it moves to any other status. |
| `comment_count` | int | Comments on the ticket. |
| `attachment_count` | int | Every file on the ticket, the comments' files included. |
| `description_excerpt` | string or null | The first 200 characters of the description (`null` without one). The full text is in the detail. |

`TicketDetail` (`GET /tickets/{id}`, and the answer of create and update): the same keys **without** `description_excerpt` and **with**

| Field | Type | Meaning |
|---|---|---|
| `description` | string or null | The full text. |
| `attachments` | `Attachment[]` | Every file of the ticket, oldest first; the ticket's own have `comment_id: null`, a comment's carry its id. |

`Attachment` (also inside comments):

```json
{
  "id": "01a14ced-fe5d-71f8-a620-e9700fdbf375",
  "ticket_id": "01a14ced-fd9d-70a8-b218-74162d54b125",
  "comment_id": "01a14ced-fe8a-74be-b693-e59e17f1dff4",
  "file_name": "screenshot.png",
  "content_type": "image/png",
  "size_bytes": 69,
  "is_image": true,
  "uploaded_by": "01a14ced-fa33-77b2-9492-dd63f257b501",
  "uploaded_by_name": "User 001",
  "created_at": "2026-10-18T02:53:47.961895Z"
}
```

`content_type` is what the content really is (detected on upload), not what the client claimed; `is_image` is true for `image/*`
(show a thumbnail). `file_name` is the sanitised name. `comment_id` is null for a file on the ticket itself.

`Comment`:

```json
{
  "id": "01a14ced-fe8a-74be-b693-e59e17f1dff4",
  "ticket_id": "01a14ced-fd9d-70a8-b218-74162d54b125",
  "author": {"id": "01a14ced-fa33-77b2-9492-dd63f257b501", "full_name": "User 001"},
  "body": "Reproduced on the demo practice.",
  "edited_at": null,
  "created_at": "2026-10-18T02:53:48.022114Z",
  "attachments": [ /* Attachment[] */ ]
}
```

`edited_at` is null until the author changes the text.

`Tag` (`GET /ticket-tags`, create, update):

```json
{
  "id": "01a14ced-fb43-726c-9041-6eaefccfa32c",
  "name": "Accounts",
  "color": "blue",
  "ticket_count": 1,
  "created_at": "2026-10-18T02:53:47.108786Z",
  "updated_at": "2026-10-18T02:53:47.108786Z"
}
```

## GET /tickets

Permission `ticket.read`. Cursor-paginated. Query:

| Param | Type | Meaning |
|---|---|---|
| `q` | string ≤ 200 | Case-insensitive substring of the **code**, the **subject** or the **description** (`%` and `_` are ordinary characters). Blank is no search. |
| `status` | `open`, `hold`, `in_progress`, `reopen`, `closed`; repeat for several (≤ 5) | Any of them. `?status=open&status=reopen`. |
| `priority` | `high`, `medium`, `low`, `not_set`; repeatable (≤ 4) | Any of them. |
| `tag_id` | uuid, repeatable (≤ 50) | Tickets that carry **any** of the tags. An unknown tag matches nothing. |
| `assignee_id` | uuid | Assigned to this person. |
| `unassigned` | bool | `true`: only tickets without an assignee. Not together with `assignee_id` (422 on `unassigned`). |
| `created_from`, `created_to` | date `YYYY-MM-DD` | Created on or after / on or before, whole days in the practice's time zone. From after to: 422 on `created_from`. |
| `sort` | `created_at` (default), `number` (alias `code`), `subject`, `status`, `priority` | `subject` ignores case; `priority` orders high, medium, low, not set; `status` orders alphabetically by its code. Ties are broken by `id`. |
| `order` | `asc`, `desc` | Default `desc` for `created_at` and `number` (newest first), `asc` for the others. |
| `limit`, `cursor` | | See Conventions. Use a cursor only with the same filters, `sort` and `order`; a cursor that was made for another sort, or is not one, answers 422 on `cursor`. |

Response: `TicketListItem[]` with `meta {next_cursor, has_more}`. One page costs a fixed number of queries whatever its size.

```json
{
  "success": true,
  "data": [ /* TicketListItem, TicketListItem */ ],
  "message": null,
  "meta": {"next_cursor": "eyJzIjoiY3JlYXRlZF9hdCIs…", "has_more": true}
}
```

The "All Tags / Priority / Status" dropdowns map to `tag_id`, `priority` and `status`; "Clear" drops them; the search box (debounce
300 ms) maps to `q`; the "Ticket No." header sorts by `number`; "Created on" by `created_at`. Fill the "All Tags" dropdown from
`GET /ticket-tags`.

## POST /tickets

Permission `ticket.create`. Response 201: `TicketDetail`.

| Field | Type | Required | Notes |
|---|---|---|---|
| `subject` | string 1–200 | yes | See [Limits](#limits). |
| `description` | string ≤ 10,000 or null | no | Blank is stored as null. |
| `status` | enum | no | Default `open`. A ticket created `closed` has `closed_at` set. |
| `priority` | enum | no | Default `not_set`. |
| `tag_ids` | uuid[] ≤ 20 | no | Tags of **this practice** (422 on `tag_ids` otherwise); repeated ids count once. |
| `assignee_id` | uuid or null | no | An **active member** of the practice (422 on `assignee_id` otherwise: unknown, another practice's, inactive). |

The code is taken from the practice's gapless `ticket` sequence in the same transaction, so concurrent creates never share a number and
a failed create leaves no gap. The first status-history row (`from_status` null) is written in the same transaction.

```json
{"subject": "Trial balance does not agree", "description": "Year end shows a difference of £12.50.", "priority": "high",
 "tag_ids": ["01a14ced-fb43-726c-9041-6eaa007888a8"], "assignee_id": "01a14ced-fa33-77b2-9492-dd63f257b501"}
```

## GET /tickets/{ticket_id}

Permission `ticket.read`. Response `TicketDetail`. Comments come from `GET /tickets/{id}/comments`.

## PATCH /tickets/{ticket_id}

Permission `ticket.update`. Response `TicketDetail`. Partial: send only what changes (the inline selects send `{"status": "hold"}`).

| Field | Semantics |
|---|---|
| `subject` | 1–200 characters. `null` answers 422. |
| `description` | `null` or blank clears it. |
| `status` | Any status. `null` answers 422. A change writes a `ticket_status_history` row, sets `closed_at` (to `closed`) or clears it (from `closed`) and is audited as `ticket.status_changed`. |
| `priority` | `null` answers 422. |
| `assignee_id` | `null` unassigns; otherwise an active member (422). An assignee who has since become inactive does not block other changes. |
| `tag_ids` | **Replaces** the ticket's tags (`[]` removes them all); `null` answers 422. To add one tag without knowing the others use `POST /tickets/bulk` with `add_tag`. |

A PATCH that changes nothing writes nothing (no audit row, no history row). 404 for an unknown or another practice's ticket.

## POST /tickets/bulk

Permission `ticket.update`. All or nothing, in one transaction.

| Field | Type | Notes |
|---|---|---|
| `ticket_ids` | uuid[] 1–200 | Duplicates count once. If **any** id is not the practice's: 404 `RESOURCE_NOT_FOUND` with `details: {"ticket_ids": [the missing ids]}` and nothing changes. |
| `action` | `set_status`, `set_priority`, `add_tag`, `remove_tag` | |
| `value` | string | `set_status`: a status; `set_priority`: a priority; `add_tag` / `remove_tag`: a tag id (422 on `value` when the tag is not the practice's). Required for every action. A wrong value is 422 on `value`. |

Response `{"action": "set_status", "affected": 2, "changed": 2}` with message `2 tickets updated`: `affected` is the number of
distinct tickets selected, `changed` how many of them actually changed (a ticket already in the wanted state, or already carrying
the tag, does not count). Each ticket that changed gets **its own** history row and audit row, as if it had been changed alone,
marked `bulk: true` in the audit meta.

## Status history

Every status of a ticket is recorded: when it is created (`from_status` null) and on each change, by PATCH or by bulk. The dashboard's
trend is rebuilt from this history. There is no endpoint that lists the history; the audit log (`GET /audit-logs`, `audit_log.read`)
shows `ticket.created` and `ticket.status_changed` rows with the before and after.

---

# Comments

## GET /tickets/{ticket_id}/comments

Permission `ticket.read`. Oldest first, cursor-paginated (`limit`, `cursor`). Response `Comment[]` with `meta`. Each comment carries
its author and its files.

## POST /tickets/{ticket_id}/comments

Permission `ticket.update`. Response 201: `Comment`, message `Comment added`.

| Field | Type | Notes |
|---|---|---|
| `body` | string ≤ 5,000, default `""` | May be empty **only** when `attachment_ids` is not (422 on `body` otherwise). |
| `attachment_ids` | uuid[] ≤ 25 | Files of **this ticket** that are not on a comment yet (uploaded earlier with `POST …/attachments` and no `comment_id`). 422 on `attachment_ids` for any other id. They move onto the new comment. |

## PATCH /tickets/{ticket_id}/comments/{comment_id}

Permission `ticket.update`, and the caller must be the **author** (403 for anyone else, moderators included). Body `{"body": "…"}`.
`edited_at` is set when the text really changes. The text may be empty only while the comment has files (422 on `body`). Audited as
`ticket_comment.updated`.

## DELETE /tickets/{ticket_id}/comments/{comment_id}

The author (with `ticket.update`) or anyone with `ticket.delete` (moderation). Deletes the comment and its files, from the storage
too. Response 200, `data: null`.

---

# Attachments

Files live on the ticket: either on the ticket itself (`comment_id: null`) or on one of its comments. A ticket holds at most **25**
files (409 `ATTACHMENT_LIMIT_REACHED`).

## What is accepted

An upload is accepted only when **all** of these hold, whatever the client says:

| Kind | Extensions | Stored `content_type` | The content must |
|---|---|---|---|
| PNG | `.png` | `image/png` | start with the PNG signature |
| JPEG | `.jpg`, `.jpeg` | `image/jpeg` | start with `FF D8 FF` |
| GIF | `.gif` | `image/gif` | start with `GIF87a` or `GIF89a` |
| WebP | `.webp` | `image/webp` | be a RIFF file of type WEBP |
| PDF | `.pdf` | `application/pdf` | start with `%PDF` |
| Word (new) | `.docx` | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | be a ZIP with `[Content_Types].xml` and a `word/` folder |
| Excel (new) | `.xlsx` | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | be a ZIP with `[Content_Types].xml` and an `xl/` folder |
| Word (old) | `.doc` | `application/msword` | be an OLE2 file that has a `WordDocument` stream |
| Excel (old) | `.xls` | `application/vnd.ms-excel` | be an OLE2 file that has a `Workbook` (or `Book`) stream |
| CSV | `.csv` | `text/csv` | be plain text (UTF-8, with or without BOM, or Windows-1252) without control characters other than tab, CR and LF, and not start with `<` |

* The **extension** must be on the list **and** the **content** must be what the extension says; a mismatch (`virus.exe` renamed to
  `.png`, an HTML or SVG file named `.csv`) is refused. SVG, HTML and every other type are refused.
* A `Content-Type` the client declares for the file part must fit the kind (for example `image/png` for a PNG; `text/plain` or
  `application/vnd.ms-excel` are accepted for a CSV, `application/zip` for `.docx`/`.xlsx`); a contradicting one is refused. Missing
  or `application/octet-stream` is fine. The stored `content_type` always comes from the table, never from the client.
* Password-protected `.docx`/`.xlsx` (an OLE2 container) are refused: the content is not readable as a package.
* The size is at most **2 MB** (2,097,152 bytes, the limit inclusive); the request body is read under that cap, so an oversized
  upload is stopped early. An empty file is refused.
* The file name is sanitised: no path (only the last segment), no control, format or line-separator characters, none of `<>:"|?*`
  (replaced by `_`), white space collapsed, no leading or trailing dots or spaces, at most 150 characters, never empty (`file`).
* There is **no virus scanning**; files are stored and served as they are, with the protections under [Downloading and showing a file](#downloading-and-showing-a-file).

## POST /tickets/{ticket_id}/attachments

Permission `ticket.update`. `multipart/form-data`:

| Part | Required | Notes |
|---|---|---|
| `file` | yes | Exactly one file part named `file` with a file name. |
| `comment_id` | no | The id of one of **this ticket's** comments, as a form field (or the query parameter `comment_id`). Without it the file belongs to the ticket. A comment of someone else needs `ticket.delete` (403 otherwise); a comment that is not this ticket's is 422 on `comment_id`. |

Response 201: `Attachment`, message `File uploaded`.

```text
POST /api/v1/organizations/{organization_id}/tickets/{ticket_id}/attachments
Authorization: Bearer <access token>
Content-Type: multipart/form-data; boundary=…

--…
Content-Disposition: form-data; name="file"; filename="screenshot.png"
Content-Type: image/png

<bytes>
--…--
```

Do not set the `Content-Type` header yourself with `fetch`/`FormData`: the browser adds the boundary.

Errors: 413 `ATTACHMENT_TOO_LARGE`, 422 `ATTACHMENT_TYPE_NOT_ALLOWED`, 422 `ATTACHMENT_EMPTY`, 409 `ATTACHMENT_LIMIT_REACHED`, 400
`FILE_INVALID` (not multipart, no `file`, two files), 503 `STORAGE_UNAVAILABLE`, plus the usual 401/403/404 (see [Error codes](#error-codes)).
Nothing is stored when any check fails, and a file whose database write fails is removed again.

## GET /tickets/{ticket_id}/attachments

Permission `ticket.read`. Every file of the ticket, comments' included, oldest first. Response `Attachment[]` (not paginated, at most 25).

## GET /tickets/{ticket_id}/attachments/{attachment_id}

Permission `ticket.read`. Streams the file:

| Header | Value |
|---|---|
| `Content-Type` | the stored (detected) type, e.g. `image/png` |
| `Content-Length` | `size_bytes` |
| `Content-Disposition` | `inline; filename="screenshot.png"; filename*=UTF-8''screenshot.png` for **images**, `attachment; …` for everything else. The `filename` form is ASCII (other characters become `_`), `filename*` carries the real name percent-encoded. |
| `X-Content-Type-Options` | `nosniff` |
| `Cache-Control` | `no-store` |
| `Content-Security-Policy` | `default-src 'none'; frame-ancestors 'none'` (every API response) |

404 `RESOURCE_NOT_FOUND` for an attachment of another ticket or practice; 404 `ATTACHMENT_FILE_MISSING` when the record exists but the
storage has lost the file.

### Downloading and showing a file

The request is authenticated with the Bearer token, which a browser does not send for `<img src>`, `<a href>` or `window.open`. So the UI
**fetches the bytes with the token and shows them from a blob URL**:

```ts
const response = await fetch(`${API}/organizations/${orgId}/tickets/${ticketId}/attachments/${attachment.id}`, {
  headers: { Authorization: `Bearer ${accessToken}` },
})
if (!response.ok) throw await response.json()        // the error envelope: show error.message
const blob = await response.blob()                    // blob.type is the stored content type
const url = URL.createObjectURL(blob)
// image (attachment.is_image): <img src={url} alt={attachment.file_name} /> scaled by CSS, no thumbnail endpoint
// anything else: <a href={url} download={attachment.file_name}>…</a>, or click it programmatically
// release it when the element goes away: URL.revokeObjectURL(url)
```

Use `file_name` and `content_type` of the attachment record, not the response headers (a cross-origin `fetch` cannot read
`Content-Disposition`; behind the nginx proxy of `docs/deployment.md` the origin is the same). Cache the blob URL per attachment while the
ticket is open. If the UI ever sets a Content-Security-Policy, `img-src` must allow `blob:`.

## DELETE /tickets/{ticket_id}/attachments/{attachment_id}

The uploader (with `ticket.update`) or anyone with `ticket.delete`; 403 otherwise. Removes the record and the stored file. 409
`COMMENT_WOULD_BE_EMPTY` when it is the only content of a comment that has no text: delete the comment instead. Response 200, `data: null`.

## Pasting and attaching in a comment (Ctrl+V)

Two equivalent flows; the first is the simplest for the UI:

1. **Upload, then comment**: upload each pasted or dropped file with `POST …/attachments` (no `comment_id`), keep the returned ids, then
   `POST …/comments` with `{"body": "…", "attachment_ids": [ids]}`. If the user discards the draft, `DELETE` the uploaded files: a file
   that is never put on a comment stays on the ticket as one of its own attachments.
2. **Comment, then upload**: `POST …/comments`, then upload each file with `comment_id` set to the new comment (a comment may be created
   with text only).

---

# Tags

Labels of the practice (`ticket.read` to see them). The list is bounded (at most 200), sorted by name ignoring case, and not paginated.

## GET /ticket-tags

Response `Tag[]`; `ticket_count` is the number of tickets that carry the tag.

## POST /ticket-tags

Permission `ticket.update`. Response 201: `Tag`. Body `{"name": "Mobile", "color": "orange"}`; `color` defaults to `grey`.
409 `TAG_NAME_TAKEN` (details `[{field: "name", code: "duplicate", message}]`) when a tag of that name exists, ignoring case; 409
`TAG_LIMIT_REACHED` at 200 tags.

## PATCH /ticket-tags/{tag_id}

Permission `ticket.update`. `{"name"?, "color"?}` (null answers 422). Tickets show the new name at once (tags are referenced by id). 409
`TAG_NAME_TAKEN` for a duplicate name; renaming to the same name in another case is allowed.

## DELETE /ticket-tags/{tag_id}

Permission `ticket.delete`. An unused tag is deleted. A tag that tickets carry answers 409 `TAG_IN_USE` with `details: {"ticket_count": 3}`;
confirm with the user and repeat with `?force=true`, which takes the tag off those tickets and deletes it. Response 200:
`{"id": "…", "tickets_untagged": 3}`.

---

# Dashboard

## GET /tickets/dashboard

Permission `ticket.read` (the same as the list). Query:

| Param | Type | Meaning |
|---|---|---|
| `date_from` | date | First day of the range. Default: `date_to` minus 18 days, so the **last 19 days**. |
| `date_to` | date | Last day, inclusive. Default: today in the practice's time zone. |

A start after the end is 422 on `date_from`; a range over **366 days** is 422 on `date_to`; a malformed date is 422 on
`query.date_from` / `query.date_to`. The response carries the range it used (`date_from`, `date_to`, `days`), so the date picker can
show it (`30-Sep-2026 - 18-Oct-2026`).

**What is counted.** Every figure is about the tickets **created in the range** (the practice's calendar days), each in its **current**
status, priority, tags and assignee, except the trend, which looks at the past. The **previous period** is the same number of days
immediately before the range: `previous_to` is the day before `date_from`, `previous_from` is `days` days earlier.

| Field | Meaning |
|---|---|
| `kpis.total`, `.open`, `.hold`, `.in_progress`, `.reopen`, `.closed` | The six cards. `value`: tickets of the range (in that status). `previous`: the same for the previous period. `change`: how to show "vs last period" (below). |
| `by_status` | Always the five statuses in the order `open, hold, in_progress, reopen, closed`. `percent`: the share of all tickets of the range to **one decimal, half up** (`Closed 251 (95.1%)`); `null` when the range holds no ticket. The rounded figures may add up to 99.9 or 100.1. |
| `by_priority` | Always the four, in the order `high, medium, low, not_set`, with `count` and `percent` as above. |
| `by_tag` | The **7 most used** tags of the range, most used first, ties by name ignoring case; a ticket counts under each of its tags, so the counts can add up to more than the tickets. Tags no ticket of the range carries are not listed. When more than 7 tags are in use, a final entry `Others`: `{tag_id: null, name: "Others", color: null, count, is_other: true}` where `count` is the number of **distinct tickets** that carry at least one tag outside the seven (a ticket with two of them counts once). No `percent` here (the counts overlap): draw bars. |
| `trend` | **14 entries**, the 14 days ending on `date_to`, oldest first: each ticket of the range counted in the status it had **at the end of that day** (practice time zone), rebuilt from the status history. A ticket created later than that day is not counted; `total` is the number counted. For a range that ends today, the last entry equals `by_status`; for an earlier `date_to` it shows the statuses at the end of that day. |
| `top_assignees` | The 10 people with the most tickets of the range, plus a row `{user_id: null, full_name: "Unassigned", count}` when any ticket has no assignee (it is not cut off by the 10). Sorted by `count` descending, then name. A person who has left keeps their name. |
| `recent` | The 7 newest tickets of the range (`created_at` descending), as `TicketListItem`. "View All" links to the list. |

**`change`** (per KPI): `kind: "new"` when `previous` is 0 and `value` is above 0, `percent` is null: show `New`. `kind: "percent"`:
`percent` is the whole-number change `(value - previous) × 100 / previous`, rounded half away from zero (3 → 4 is `33`, 2 → 1 is `-50`,
1 → 4 is `300`): show it signed, `+12%`, `-5%`, `0%`. `kind: "none"` when both are 0: show `0%`.

Exact response for the 30 days 1 to 30 September (16 tickets in the range, 10 in the previous period; `recent` shortened to its first
item, the endpoint returns up to 7):

```json
{
  "date_from": "2026-09-01",
  "date_to": "2026-09-30",
  "previous_from": "2026-08-02",
  "previous_to": "2026-08-31",
  "days": 30,
  "kpis": {
    "total":       {"value": 16, "previous": 10, "change": {"kind": "percent", "percent": 60}},
    "open":        {"value": 6, "previous": 4, "change": {"kind": "percent", "percent": 50}},
    "hold":        {"value": 2, "previous": 0, "change": {"kind": "new", "percent": null}},
    "in_progress": {"value": 3, "previous": 3, "change": {"kind": "percent", "percent": 0}},
    "reopen":      {"value": 1, "previous": 2, "change": {"kind": "percent", "percent": -50}},
    "closed":      {"value": 4, "previous": 1, "change": {"kind": "percent", "percent": 300}}
  },
  "by_status": [
    {"status": "open", "count": 6, "percent": 37.5},
    {"status": "hold", "count": 2, "percent": 12.5},
    {"status": "in_progress", "count": 3, "percent": 18.8},
    {"status": "reopen", "count": 1, "percent": 6.3},
    {"status": "closed", "count": 4, "percent": 25.0}
  ],
  "by_priority": [
    {"priority": "high", "count": 4, "percent": 25.0},
    {"priority": "medium", "count": 4, "percent": 25.0},
    {"priority": "low", "count": 3, "percent": 18.8},
    {"priority": "not_set", "count": 5, "percent": 31.3}
  ],
  "by_tag": [
    {"tag_id": "01a14cf2-1d74-715f-a7de-0ff6a8dd417d", "name": "Bug", "color": "red", "count": 6, "is_other": false},
    {"tag_id": "01a14cf2-1d74-715f-a7de-0ff968a62e4c", "name": "Reporting", "color": "green", "count": 3, "is_other": false},
    {"tag_id": "01a14cf2-1d74-715f-a7de-0ffa8555cc17", "name": "Accounts", "color": "blue", "count": 2, "is_other": false},
    {"tag_id": "2ced8f3a-a4c0-4140-9908-c83ab7ab2362", "name": "Docs", "color": "blue", "count": 2, "is_other": false},
    {"tag_id": "01a14cf2-1d74-715f-a7de-0ff7523cb6d2", "name": "Enhancement", "color": "violet", "count": 2, "is_other": false},
    {"tag_id": "a337c66e-6a53-4188-851b-8cc6ec6b95fa", "name": "Security", "color": "red", "count": 2, "is_other": false},
    {"tag_id": "01a14cf2-1d74-715f-a7de-0ff8d2d01eb6", "name": "UI", "color": "blue", "count": 2, "is_other": false},
    {"tag_id": null, "name": "Others", "color": null, "count": 1, "is_other": true}
  ],
  "trend": [
    {"date": "2026-09-17", "open": 6, "hold": 2, "in_progress": 1, "reopen": 0, "closed": 0, "total": 9},
    {"date": "2026-09-18", "open": 6, "hold": 2, "in_progress": 2, "reopen": 0, "closed": 0, "total": 10},
    {"date": "2026-09-19", "open": 6, "hold": 2, "in_progress": 2, "reopen": 0, "closed": 0, "total": 10},
    {"date": "2026-09-20", "open": 7, "hold": 2, "in_progress": 2, "reopen": 0, "closed": 0, "total": 11},
    {"date": "2026-09-21", "open": 6, "hold": 2, "in_progress": 3, "reopen": 0, "closed": 0, "total": 11},
    {"date": "2026-09-22", "open": 7, "hold": 2, "in_progress": 3, "reopen": 0, "closed": 0, "total": 12},
    {"date": "2026-09-23", "open": 6, "hold": 2, "in_progress": 3, "reopen": 0, "closed": 1, "total": 12},
    {"date": "2026-09-24", "open": 7, "hold": 2, "in_progress": 3, "reopen": 0, "closed": 1, "total": 13},
    {"date": "2026-09-25", "open": 7, "hold": 2, "in_progress": 3, "reopen": 1, "closed": 0, "total": 13},
    {"date": "2026-09-26", "open": 7, "hold": 2, "in_progress": 3, "reopen": 1, "closed": 1, "total": 14},
    {"date": "2026-09-27", "open": 6, "hold": 2, "in_progress": 4, "reopen": 1, "closed": 1, "total": 14},
    {"date": "2026-09-28", "open": 6, "hold": 2, "in_progress": 4, "reopen": 1, "closed": 2, "total": 15},
    {"date": "2026-09-29", "open": 6, "hold": 2, "in_progress": 3, "reopen": 1, "closed": 3, "total": 15},
    {"date": "2026-09-30", "open": 6, "hold": 2, "in_progress": 3, "reopen": 1, "closed": 4, "total": 16}
  ],
  "top_assignees": [
    {"user_id": null, "full_name": "Unassigned", "count": 5},
    {"user_id": "01a14cf2-1d93-763c-b637-77d98585eef3", "full_name": "Alice Adams", "count": 4},
    {"user_id": "01a14cf2-1da9-7523-bd2a-c28ed2e06449", "full_name": "Bob Brown", "count": 4},
    {"user_id": "01a14cf2-1dbe-75f6-9cdf-e544f1d1deaf", "full_name": "Carol Clark", "count": 3}
  ],
  "recent": [
    {
      "id": "412c58f7-2f2c-464f-b6b3-9a7f8a7e377f",
      "code": "APT16",
      "number": 16,
      "category": "application",
      "subject": "September 16",
      "status": "closed",
      "priority": "not_set",
      "tags": [{"id": "a337c66e-6a53-4188-851b-8cc6ec6b95fa", "name": "Security", "color": "red"}],
      "assignee": {"id": "01a14cf2-1da9-7523-bd2a-c28ed2e06449", "full_name": "Bob Brown"},
      "created_by": "01a14cf2-1c62-7197-b126-b4036663dad9",
      "created_by_name": "User 001",
      "created_at": "2026-09-30T12:00:00Z",
      "updated_at": "2026-10-18T02:58:18.570285Z",
      "closed_at": "2026-09-30T18:00:00Z",
      "comment_count": 0,
      "attachment_count": 0,
      "description_excerpt": null
    }
  ]
}
```

(Wrapped as usual in `{"success": true, "data": …, "message": null, "meta": null}`.) This is the dataset of
`tests/test_ticket_dashboard.py`, whose assertions are the exact numbers above. A range with no tickets answers zeros: every `value`
and `previous` 0, every `change` `{kind: "none", percent: null}`, every `percent` null, empty `by_tag`, `top_assignees` and `recent`,
and 14 all-zero trend entries.

---

# Permissions

| Code | Allows |
|---|---|
| `ticket.read` | Lists, the detail, comments, the files (and downloading them), tags and the dashboard |
| `ticket.create` | Raising tickets |
| `ticket.update` | Editing tickets (status, priority, tags, assignee, text), bulk changes, commenting, uploading files, creating and editing tags, editing **one's own** comments and removing **one's own** comments and files |
| `ticket.delete` | Deleting tags, and removing **anyone's** comments and files (moderation) |

| Role | Codes |
|---|---|
| Owner, Admin, Partner, Manager | all four |
| Staff, Billing | `ticket.read`, `ticket.create`, `ticket.update` |
| Viewer | `ticket.read` |

Custom roles get exactly the codes they are given. Hide or disable what the caller cannot do, and still handle a 403.

# Error codes

| HTTP | `error.code` | When | `error.details` |
|---|---|---|---|
| 400 | `FILE_INVALID` | An upload that is not `multipart/form-data`, has no `file` part, has more than one, or is malformed | null |
| 401 | `AUTH_REQUIRED` | No or an invalid access token | null |
| 403 | `PERMISSION_DENIED` | The role lacks the code; or editing someone else's comment; or deleting someone else's comment or file without `ticket.delete` | null |
| 404 | `RESOURCE_NOT_FOUND` | Unknown id, or another practice's; a bulk call with such an id | null; bulk: `{"ticket_ids": [ids]}` |
| 404 | `ATTACHMENT_FILE_MISSING` | The attachment record exists but the stored file is gone | null |
| 409 | `TAG_NAME_TAKEN` | A tag with that name exists (ignoring case) | `[{field: "name", code: "duplicate", message}]` |
| 409 | `TAG_LIMIT_REACHED` | The practice has 200 tags | null |
| 409 | `TAG_IN_USE` | Deleting a tag that tickets carry without `force=true` | `{"ticket_count": n}` |
| 409 | `ATTACHMENT_LIMIT_REACHED` | The ticket already holds 25 files | `{"max_attachments": 25}` |
| 409 | `COMMENT_WOULD_BE_EMPTY` | Deleting the last file of a comment that has no text | null |
| 409 | `SEQUENCE_BACKWARDS` | Moving the `ticket` sequence's next number backwards in Settings | |
| 413 | `ATTACHMENT_TOO_LARGE` | A file above 2 MB (also when the body is bigger than the file limit plus its envelope) | `{"max_bytes": 2097152}` |
| 413 | `REQUEST_TOO_LARGE` | Any other write whose body exceeds `MAX_REQUEST_BODY_KB` | |
| 422 | `VALIDATION_ERROR` | A field or parameter is invalid (see Conventions for the field names) | `[{field, code, message}]` |
| 422 | `ATTACHMENT_TYPE_NOT_ALLOWED` | Extension not on the list, content not what the extension says, or a contradicting declared type | `[{field: "file", code: "type_not_allowed", message}]` |
| 422 | `ATTACHMENT_EMPTY` | A zero-byte file | `[{field: "file", code: "empty", message}]` |
| 429 | `RATE_LIMITED` | The global request ceiling | `{"retry_after": seconds}` |
| 503 | `STORAGE_UNAVAILABLE` | The file store cannot take the file (upload) | null |

# Audit

Every write is audited in the same transaction (`GET /audit-logs`, `audit_log.read`), with the caller, the IP address, the user agent
and the request id. A bulk call writes the same rows as single changes, one **per ticket that changed**, with `bulk: true` in the meta
and the request id they share. A change that changes nothing writes nothing.

| Action | `target_type` | Meta |
|---|---|---|
| `ticket.created` | `ticket` | `code`, `subject`, `status`, `priority`, `assignee_id`, `tags` (names) |
| `ticket.updated` | `ticket` | `code`, `subject`, `changes: {subject, description, priority, assignee_id, tags: [old, new]}` (only what changed; a long text is cut at 500 characters; `tags` are lists of names) |
| `ticket.status_changed` | `ticket` | `code`, `subject`, `from`, `to`, `changes: {status: [from, to]}` |
| `ticket_comment.created` | `ticket_comment` | `ticket_id`, `code`, `attachments` |
| `ticket_comment.updated` | `ticket_comment` | `ticket_id`, `code`, `changes: {body: [old, new]}` |
| `ticket_comment.deleted` | `ticket_comment` | `ticket_id`, `code`, `attachments` |
| `ticket_attachment.added` | `ticket_attachment` | `ticket_id`, `code`, `file_name`, `content_type`, `size_bytes`, `comment_id` |
| `ticket_attachment.deleted` | `ticket_attachment` | `ticket_id`, `code`, `file_name`, `size_bytes` |
| `ticket_tag.created` | `ticket_tag` | `name`, `color` |
| `ticket_tag.updated` | `ticket_tag` | `name`, `changes: {name, color: [old, new]}` |
| `ticket_tag.deleted` | `ticket_tag` | `name`, `color`, `tickets_untagged` |

# File storage

Files are kept by `app/services/storage.py` (`FileStorage`: `put`, `open`, `delete`; `LocalFileStorage` today) under the directory
`STORAGE_DIR`.

* **Setting**: `STORAGE_DIR` (`Settings.storage_dir`). Empty: `storage` next to `app/` (git-ignored); a relative path is resolved
  against the project root; an absolute path is used as it is. The Docker image sets `/var/lib/capium/storage` and declares it a
  volume: mount persistent storage there, writable by uid 10001 (`docs/deployment.md`). The directory is created on first use.
* **Layout**: `<STORAGE_DIR>/<practice id>/<first two characters of the key>/<key>`. The key is 43 random URL-safe characters, never
  derived from the file name; a file is only reachable with its own practice's id, so a record of another practice cannot reach it.
* **Writes** are atomic (a temporary `.part` file in `<STORAGE_DIR>/.tmp`, on the same file system, then a rename). A file is written
  inside the transaction that records it and removed if that transaction fails; deleting an attachment, a comment, a ticket or a bulk
  selection removes the files **after** the commit. A deletion that cannot remove a file is logged and never fails the request.
* **Back up** the directory together with the database; a database restored without it leaves attachment records whose download answers
  404 `ATTACHMENT_FILE_MISSING`.
* The test suite points `STORAGE_DIR` at a temporary directory and never uses `storage/` of the repository.

# Numbers

The `ticket` sequence (prefix `APT`, no zero padding: `APT1`, `APT271`) is gapless and locked per practice like the other kinds and is
created with every practice (existing practices got it from migration `015`). Settings can change its prefix, padding and next number
(`PATCH /sequences/ticket`, `organization.update`); the next number can only move forward. A ticket keeps the code it was created with.

# Screens and the calls behind them

| Screen | Calls |
|---|---|
| Application Testing Dashboard | `GET /tickets/dashboard?date_from=&date_to=` (send the picker's range; without it the last 19 days). KPI cards from `kpis`, donut from `by_status`, bars from `by_priority` and `by_tag`, line chart from `trend`, "Top Assignees" from `top_assignees`, "Recent Tickets" from `recent`. |
| All Tickets | `GET /tickets` with `q`, `status`, `priority`, `tag_id`, `sort`, `order`, `cursor`; the tag filter from `GET /ticket-tags`. Inline status and priority: `PATCH /tickets/{id}`. "Add tags": `PATCH /tickets/{id}` with the full `tag_ids`, or `POST /tickets/bulk` `add_tag` for one. Selection: `POST /tickets/bulk`. "+ Application Testing": `POST /tickets`. |
| Ticket detail | `GET /tickets/{id}` and `GET /tickets/{id}/comments`; inline subject, description, status, priority, tags: `PATCH /tickets/{id}`; the files panel: upload `POST …/attachments`, show with the blob flow, remove `DELETE …/attachments/{id}`; comments: `POST/PATCH/DELETE …/comments`. |

# Types at a glance

```ts
type TicketStatus = 'open' | 'hold' | 'in_progress' | 'reopen' | 'closed'
type TicketPriority = 'high' | 'medium' | 'low' | 'not_set'
type TagColor = 'violet' | 'blue' | 'green' | 'red' | 'orange' | 'grey'

interface TagRef { id: string; name: string; color: TagColor }
interface Person { id: string; full_name: string }
interface Attachment {
  id: string; ticket_id: string; comment_id: string | null; file_name: string; content_type: string
  size_bytes: number; is_image: boolean; uploaded_by: string; uploaded_by_name: string | null; created_at: string
}
interface TicketListItem {
  id: string; code: string; number: number; category: 'application'; subject: string
  status: TicketStatus; priority: TicketPriority; tags: TagRef[]; assignee: Person | null
  created_by: string; created_by_name: string | null; created_at: string; updated_at: string
  closed_at: string | null; comment_count: number; attachment_count: number; description_excerpt: string | null
}
interface TicketDetail extends Omit<TicketListItem, 'description_excerpt'> {
  description: string | null; attachments: Attachment[]
}
interface TicketComment {
  id: string; ticket_id: string; author: Person; body: string; edited_at: string | null
  created_at: string; attachments: Attachment[]
}
interface TicketTag { id: string; name: string; color: TagColor; ticket_count: number; created_at: string; updated_at: string }
interface BulkResult { action: 'set_status' | 'set_priority' | 'add_tag' | 'remove_tag'; affected: number; changed: number }

interface Change { kind: 'new' | 'percent' | 'none'; percent: number | null }
interface Kpi { value: number; previous: number; change: Change }
interface TicketDashboard {
  date_from: string; date_to: string; previous_from: string; previous_to: string; days: number
  kpis: Record<'total' | TicketStatus, Kpi>
  by_status: { status: TicketStatus; count: number; percent: number | null }[]
  by_priority: { priority: TicketPriority; count: number; percent: number | null }[]
  by_tag: { tag_id: string | null; name: string; color: TagColor | null; count: number; is_other: boolean }[]
  trend: ({ date: string; total: number } & Record<TicketStatus, number>)[]
  top_assignees: { user_id: string | null; full_name: string; count: number }[]
  recent: TicketListItem[]
}
```
