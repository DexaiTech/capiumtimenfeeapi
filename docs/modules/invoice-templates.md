# Invoice templates

An invoice template says how a practice's invoices look: the layout, whether the logo and the bank
details are printed, and a footer text. Every practice has exactly one **default** template, and
a client may choose another (`clients.invoice_template_id`). This document is the contract for the
API and for the UI built on it.

Code: `app/api/v1/invoice_templates.py` → `app/services/invoice_templates.py` →
`app/repositories/invoice_templates.py`; schemas `app/schemas/invoice_templates.py`; model
`app/db/models/invoice_templates.py`. Migration `017` (also adds the client billing columns).
Tests: `tests/test_invoice_templates.py`, `tests/test_client_billing_settings.py`.

All paths are under `/api/v1/organizations/{organization_id}`. Responses use the standard
envelope; another practice's ids answer 404.

## Rendering is not changed yet

**The invoice PDF is still drawn with the practice letterhead and footer** (`render_invoice` in
`app/services/invoice_pdf.py`, from the practice settings): choosing a template changes nothing on
the PDF yet. The Billing release that renders invoices from templates should resolve the template
in `render_invoice_pdf` (`app/services/invoices.py`, used by `GET /invoices/{id}/pdf` and by the
email worker): the client's `invoice_template_id`, else the practice default
(`InvoiceTemplateRepository.get_default`), and pass its `layout`, `show_logo`,
`show_bank_details` and `footer_text` to `render_invoice`.

## Endpoints

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/invoice-templates?include_inactive=` | any member (`include_inactive=true`: `settings.manage`, else 403) | The active templates, the default first, then by name. Bounded: not paginated. |
| POST | `/invoice-templates` | `settings.manage` | Create (201). |
| GET | `/invoice-templates/{template_id}` | any member (an inactive template: `settings.manage`, else 404) | One template. |
| PATCH | `/invoice-templates/{template_id}` | `settings.manage` | Change any field except `is_default`. |
| DELETE | `/invoice-templates/{template_id}` | `settings.manage` | Delete (soft) a template no client uses. |
| POST | `/invoice-templates/{template_id}/make-default` | `settings.manage` | Make it the practice default. |

No new permission codes were added.

## Shapes

**InvoiceTemplateRead** (every response):

```json
{
  "id": "…", "name": "Standard",
  "layout": "standard",            // "standard" | "detailed" (every time entry) | "summary" (one line per job)
  "show_logo": true, "show_bank_details": true,
  "footer_text": null,             // up to 1000 characters
  "is_default": true,              // the template of clients that have not chosen one
  "status": "active",              // "active" | "inactive" (hidden from pickers)
  "client_count": 0,               // live clients of the practice that use it
  "created_at": "…", "updated_at": "…"
}
```

**Create** (`POST`): `name` (1–80 characters, trimmed, unique among live templates ignoring
case), `layout` (default `standard`), `show_logo` (default true), `show_bank_details` (default
true), `footer_text` (up to 1000 characters; blank means none), `is_default` (default false;
true makes it the default and unsets the previous one in the same transaction) and `status`
(default `active`; the default template must be active). Unknown fields answer 422.

**Update** (`PATCH`): any of `name`, `layout`, `show_logo`, `show_bank_details`, `footer_text`
(null or blank clears it), `status`. `null` for any other field answers 422; `is_default`
cannot be changed here (use `make-default`).

## Rules and error codes

| Code | Status | When |
|---|---|---|
| `TEMPLATE_NAME_TAKEN` | 409 | Another live template has the name (ignoring case); `details: [{field: "name", …}]`. A deleted template frees its name. |
| `TEMPLATE_IS_DEFAULT` | 409 | Deleting or deactivating the default template: make another one the default first. |
| `TEMPLATE_IN_USE` | 409 | Deleting a template a client uses (`details: {clients: n}`): deactivate it instead. Clients keep an inactive template. |
| `TEMPLATE_INACTIVE` | 409 | Making an inactive template the default. |
| `TEMPLATE_DEFAULT_CHANGED` | 409 | The default was changed by someone else at the same moment (rare): try again. |
| `RESOURCE_NOT_FOUND` | 404 | Unknown template, another practice's template, or an inactive one for a member without `settings.manage`. |
| `PERMISSION_DENIED` | 403 | A write without `settings.manage`, or `include_inactive=true` without it. |
| `VALIDATION_ERROR` | 422 | Invalid fields. |

* **Exactly one default.** A practice is created with a `Standard` template (layout `standard`,
  logo and bank details on, no footer) as its default, and migration `017` creates it for every
  practice that existed. A partial unique index allows at most one live default; changing the
  default locks the practice's templates so concurrent changes still end with exactly one.
  `make-default` on the default changes nothing.
* **Clients.** A client chooses a template with `invoice_template_id` (create or update;
  `docs/modules/clients.md`): an active template of the practice (another practice's id answers
  404, an inactive or deleted one 422/404). A client without one uses the default.
* Deleting is a soft delete: the row stays, the name becomes free.

## Audit actions

`invoice_template.created`, `.updated` (with `changes: {field: [old, new]}`; a request that
changes nothing writes nothing), `.deactivated`, `.activated`, `.made_default` (with
`previous_default_id` and `previous_default_name`), `.deleted`; target `invoice_template`, with
`name`, `layout`, `status` and `is_default` in the metadata.

## Notes for the UI

* Settings → Invoice templates: a table of `list` with a "Default" badge, `client_count`, an
  inactive filter (`include_inactive=true`, only for `settings.manage`), "Make default",
  deactivate and delete (disabled with a hint when `is_default` or `client_count > 0`).
* The client form's template picker lists the active templates (`GET /invoice-templates`, any
  member) and shows the practice default when the client has none.
