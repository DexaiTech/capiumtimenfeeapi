# APT84 – WIP costing (WIP valuation sources and order): implementation plan

Status: **Approved 2026-11-06 — implementation in progress.** Decisions in §15 (the ticket's own
comment (1) has still not been seen; §15 marks which answers are assumptions to confirm).
Investigated on `origin/main` of both repositories (API `197f534`, UI `5059d4b`) on 2026-11-06,
following `CLAUDE.md` → docs → graphify graph → source. Every claim below was checked in code.

## 1. Original requirement

Ticket APT84 (Medium), "WIP costing". The practice decides which rate sources value the time on a
job, and in what order, so the WIP list shows each job at a value it trusts. A new **WIP valuation**
card on Settings → General → WIP & billing lists four sources — (1) client agreed rate, (2) service
specific rate, (3) grade specific rate, (4) staff specific rate — each with an on/off switch and a
position (drag and drop, up/down buttons for the keyboard); disabled sources sit below, greyed,
unnumbered; at least one stays on ("Keep at least one rate switched on."); default all on in that
order. Saved with the WIP & billing tab and audited. Impact: a job's WIP = its unbilled entries,
each priced at the first enabled source with a rate; totals follow the setting; each entry shows the
rate used and its source; an entry no enabled source can price shows **"No rate"** rather than £0
(open question on fallback); WIP adjustment thresholds use the valued WIP.

## 2. Requirement interpretation

Make the order of the charge-out rate ladder a **practice setting** (which sources count, in which
order), apply it to unbilled time, and show per entry which source priced it.

## 3. Existing implementation (evidence)

- **Time is priced when saved and the value is stored**: `charge_rate`, `cost_rate`,
  `charge_value`, `cost_value`, `rate_source` on each entry (`app/db/models/time.py:68-75`); priced
  on create (`services/time_entries.py:422`), edit (`:594`), correction (`time_corrections.py:374`),
  re-rate (`rerate.py:218`).
- **Every WIP view sums the stored value** (`repositories/wip_atoms.py:353-354,435`,
  `repositories/wip.py:142,263`, reports/ageing/dashboard `reports.py:263`, `dashboard.py:164,176`);
  historic WIP is frozen in `wip_snapshots`; invoices take `charge_value` as `wip_value`
  (`services/invoices.py:201,1473`).
- **Re-rate** = preview → apply on unbilled time, skipping billed, in-draft, written-off and locked
  time (`rerate.py:205-265`), capped at 20,000 entries (`:60`).
- **The ladder today** (`app/services/charge_rates.py:243-354`): client (levels 1–3: client+service+
  staff, client+service, client+staff/client) → the job's own hourly rate (4, APT54) → service (5:
  service+staff, service+grade, service) → staff (6) → grade (7, incl. the grade's default rate) →
  practice default (may be £0, `models/organization.py:136`).
- **Settings that already shape it**: `rate_mode` — staff_first / grade_only / staff_only (APT79,
  merged 2026-11-05) — and, for the recovery analysis only, `recovery_rate_order` (migration 066).
- **Bypass**: `services/job_rates.py:44-75` (client Jobs tab) repeats the ladder logic instead of
  using `RateBook`; `onboarding.py:95` reads `rate_mode`; `billing_summary.py:17` classifies by
  `rate_source`.
- **WIP adjustments**: £ threshold compares the adjustment amount (`wip_adjustments.py:176-179`); the
  % rule uses the stored ledger value (`:180-187`, `wip_ledger.py:92-110`) — both follow re-rated
  values without code change.
- **UI**: Settings tab `GeneralSettingsPage.tsx:20-26`, each card its own form and Save
  (`SettingsCard.tsx`, `BillingSettingsCard.tsx`); the save hook doesn't refresh WIP/time queries
  (`useOrganizationForm.ts:57-89`); no drag-and-drop component exists in the app; WIP Details
  already has a hidden "Rate" column (`itemColumns.tsx:118`, `WipDetailsPage.tsx:139`); the WIP item
  API has no `rate_source` (`schemas/wip.py:205`).

## 4. Root cause

Not a defect — a new setting. The ladder's order is fixed in code (adjustable only through the
three rate modes), so a practice can't choose which rates value its WIP.

## 5. Affected screens and modules

Settings → General → WIP & billing (new card) and Rates (the rate-mode select it supersedes); WIP
Summary/Details (rate source, "No rate"); Rate Preview; the client Jobs tab; Staff list rate column;
time entry pricing everywhere (the stored value is shared by timesheets, budgets, invoices); re-rate;
recovery; onboarding.

## 6. Components and files involved (expected)

API: migration (new `organizations.wip_valuation_sources`), `models/organization.py`,
`schemas/organization.py`, `services/organizations.py`, `charge_rates.py` (`RateBook.charge_steps`
in the configured order), `job_rates.py`, `onboarding.py`, `rerate.py` (a change of source counts as
a change), `schemas/wip.py` + `wip_atoms.py` (expose `rate_source`), docs `team-rates.md`,
`wip.md`, `foundation.md`.
UI: new `WipValuationCard` (reorderable list, up/down buttons, `Switch`es, "Not used" group),
`GeneralSettingsPage`, WIP Details rate-source column + "No rate" flag, `RatePreviewPage`,
`RateModeCard` (remove the rate-mode select if §11.5 = yes), query invalidation after save.

## 7. Proposed solution (to be confirmed after §11)

1. Store the enabled sources in order: `organizations.wip_valuation_sources text[]` (1–4 distinct
   values, CHECK like migration 066), **backfilled from each practice's current `rate_mode`** so no
   practice's values change on release.
2. `RateBook.charge_steps` tries the groups in that order, skipping disabled ones (each step keeps
   `applicable: false` + reason, so Rate Preview explains it). One shared place — `job_rates.py`
   switches to it instead of its own copy.
3. Saving a new order **re-values unbilled time** through the existing re-rate engine, after an
   impact preview ("N entries, WIP £X → £Y"), skipping billed/in-draft/locked time; audited.
4. WIP items return `rate_source`; WIP Details shows it and a "No rate" flag.
5. UI card as the ticket describes; up/down buttons for keyboard use; drag and drop via the
   already-installed `motion` package (`Reorder`), or up/down only if that proves unreliable.

Not proposed: valuing WIP live on every read (would rewrite ~15 SQL sums, break frozen snapshots and
the invoice link, and be slow).

## 8–10. UI / API / Database changes

As above: 1 migration (column + backfill), organization schema/route, RateBook, re-rate and WIP
item changes; 1 new settings card, WIP Details column, Rate Preview update.

## 11. Questions for the client

1. Does the **job's own hourly rate** (the "client rate" agreed on the client's Jobs tab, APT54)
   count as **Client agreed**?
2. The service source has "service + staff" and "service + grade" rates. Do they need the Staff /
   Grade sources switched on to apply?
3. **No enabled source applies**: fall back to the practice default rate, or "No rate" (valued £0)?
   Should a practice default of £0 also show "No rate"?
4. **Existing practices**: keep today's order (taken from their rate mode), and use the ticket's
   default (client, service, **grade, staff**) only for new practices? (Today's normal mode puts
   staff before grade; applying the ticket's default everywhere would re-price anyone with their
   own rate and a grade rate.)
5. Does this **replace the "Rate mode" setting** (APT79)? Keeping both would conflict.
6. Does the order price **all time** (timesheets, budgets, invoices) or only the WIP list? We
   recommend all time, because the stored value is shared.
7. On save, re-value unbilled time automatically after an impact preview (billed, draft and locked
   time unchanged)?
8. Should the recovery analysis follow this order, or keep its own "Recovery rate priority"?
9. One Save for the whole WIP & billing tab, or the card's own Save like the other cards?

## 12. Regression risks

Changing the order re-prices unbilled time (intended, behind a preview); a practice-wide re-value
can exceed the 20,000-entry cap; locked periods keep old values; tests that pin today's ladder and
rate modes (`test_charge_rates`, `test_rates_v2`, `test_rate_mode`, `test_job_rates`,
`test_recovery`, `test_rerate`, `test_billing_summary`, `test_onboarding`, settings/OpenAPI tests;
UI General settings and Rate Preview tests) are updated only where the approved behaviour changes.

## 13. Testing strategy

API: each order and on/off combination, backfill per rate mode, "No rate" vs default, re-value
preview/apply (skips billed/draft/locked), audit, migration up/down. UI: card (reorder by buttons and
drag, last-source guard, Not used group), save + refresh, WIP Details source column and flag,
keyboard use. Full API (CI) and UI suites, type check, lint, format, production build; local run
and live check.

## 14. Acceptance criteria

Defaults reproduce today's values for every existing practice; changing the order or switching a
source off changes WIP values exactly as the ladder says, after a preview; each WIP entry shows its
source; unpriced entries show "No rate"; at least one source always stays on; the change is audited.

**Size (honest): Medium–Large** — about 3–4 API dev-days and 2–3 UI dev-days, plus docs. It
overlaps APT79 (rate modes) and APT81 (overtime pricing in the same `RateBook`), so it should be
scheduled after the client answers §11.4–11.5.

## 15. Decisions (2026-11-06) and the agreed contract

Decided by the developer / project lead:

- **Replaces "Rate mode".** Each practice keeps the order it has today (backfill: `staff_first` →
  client, service, staff, grade; `grade_only` → client, service, grade (staff off); `staff_only` →
  client, service, staff (grade off)), so no value changes on release. The ticket's default (client,
  service, grade, staff) is used for new practices.
- **No enabled source applies**: the practice default rate when it is above £0; otherwise the entry
  is valued £0 with `rate_source` `none`, shown as **"No rate"**.
- Delivered before APT81; "role" in APT81 means grade.

Assumed from §11 recommendations (to confirm with the client; easy to change):

- §11.1 the job's own hourly rate (APT54, ladder level 4) belongs to **Client agreed**.
- §11.2 inside **Service**: `service_grade` applies only while **Grade** is on; `service_staff` and
  `service` always apply (this reproduces today's three rate modes exactly).
- §11.6 the order prices **all time** (the stored value is shared by WIP, timesheets, budgets,
  invoices).
- §11.7 changing the order re-values **unbilled** time after an impact preview; billed, in-draft,
  written-off and locked time keep their values.
- §11.8 the recovery analysis keeps its own "Recovery rate priority".
- §11.9 the card has its own Save, like the other cards on the tab.

### API contract

- `organizations.wip_valuation_sources text[] NOT NULL`: the **enabled** sources in priority order,
  values `client`, `service`, `grade`, `staff`, 1–4 distinct (CHECK). Backfilled from `rate_mode` as
  above; new practices `{client,service,grade,staff}`.
- `time_entries.rate_source` CHECK gains `none` ("No rate").
- `GET /organizations/{id}` returns `wip_valuation_sources`. `rate_mode` stays in the response,
  **derived** from the sources (backward compatible for one release), and `PATCH` still accepts
  `rate_mode` (mapped to sources) so an older UI keeps working; documented as deprecated.
- `POST /organizations/{id}/wip-valuation/preview` `{sources}` → `{entries_changed, wip_before,
  wip_after, skipped: {billed, in_draft, locked, written_off}, too_many}` (nothing saved).
- `PUT /organizations/{id}/wip-valuation` `{sources, revalue: bool}` → saves the order (422
  `WIP_VALUATION_SOURCES` "Keep at least one rate switched on." when empty/invalid) and, with
  `revalue`, re-values unbilled time through the re-rate engine in the same transaction (refused
  with 422 when above the re-rate cap, pointing to Team → Rates → Change Rates on Past Time). Audit:
  `organization.updated` (changes `wip_valuation_sources`) + `rate.rerun` when re-valued.
- `RateBook.charge_steps` follows the configured order; disabled groups come back with
  `applicable: false` and a reason ("Switched off in WIP valuation."), so Rate Preview explains it;
  `job_rates.py` uses the same rule; `onboarding.py` no longer reads `rate_mode`.
- Re-rate treats a change of `rate_source` as a change (fixes stale sources).
- WIP items return `rate_source` (and `charge_rate`).
