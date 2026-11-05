# Coding standards

Conventions this codebase already follows, with a file to copy from for each. When adding code,
match the module next to it; when this page and the code disagree, the code wins and this page
should be fixed.

## Module layout

A feature normally touches one file per layer, named after the resource (snake_case, plural
where the resource is a collection):

| Layer | Path | Example |
|---|---|---|
| Router | `app/api/v1/<module>.py`, included in `app/api/router.py` | `app/api/v1/grades.py` |
| Schemas | `app/schemas/<module>.py` | `app/schemas/grades.py` |
| Service | `app/services/<module>.py` | `app/services/grades.py` |
| Repository | `app/repositories/<module>.py` | `app/repositories/grades.py` |
| Model | `app/db/models/<area>.py`, exported from `app/db/models/__init__.py` | `Grade` in `app/db/models/practice.py` |
| Migration | `alembic/versions/NNN_<description>.py` | [database.md](database.md#migrations) |
| Tests | `tests/test_<module>.py`, shared helpers in `tests/<area>_helpers.py` | `tests/test_grades.py` |
| Module doc | `docs/modules/<area>.md` | `docs/modules/team-rates.md` |

Large services are split by concern into sibling modules rather than one big file
(`time_entries.py`, `time_views.py`, `time_corrections.py`, `time_rules.py`, ...). Code shared
by several services goes to `app/services/common.py`, `app/services/*_common.py` or `app/utils/`.

## Naming

| Thing | Pattern | Example |
|---|---|---|
| Service class | `<Resource>Service` (95 classes) | `GradeService` |
| Repository class | `<Resource>Repository` (89 classes) | `GradeRepository` |
| Schemas | `<Resource>Read`, `<Resource>Create`, `<Resource>Update` | `GradeRead`, `GradeCreate` |
| Service dependency | `get_<resource>_service`, alias `<Resource>ServiceDep` | `get_grade_service`, `GradeServiceDep` |
| Permission alias | `<Resource>Reader` / `<Resource>Manager` = `Annotated[OrgContext, Depends(require("..."))]` | `GradeReader`, `GradeManager` |
| Error code | `UPPER_SNAKE`, prefixed by the resource | `GRADE_CODE_TAKEN`, `TIME_LOCKED`, `PERIOD_LOCKED` |
| Audit action | `<entity>.<past tense>` | `grade.created`, `grade.deactivated` |
| Permission code | `<area>.<verb>` | `rate.manage`, `time.read_all` |
| URL paths | kebab-case plural nouns; actions as sub-resources with POST | `/activity-codes`, `/grades/{grade_id}/deactivate` |
| Tests | a sentence describing the behaviour | `test_a_new_practice_has_the_default_grades` |

In code the tenant is an `organization` (`organization_id`, `OrgContext`); in prose, error
messages and docs it is a *practice*.

## Routers

* HTTP only: no business rules, no queries. The module docstring says so
  (`"""... HTTP only."""`).
* `router = APIRouter(prefix="/organizations/{organization_id}", tags=[...])` for practice routes.
* Every route has `summary=`, a `description=` that states the permission (`Requires
  \`rate.manage\`.`), the error codes it can answer and any non-obvious rule, and
  `responses=error_responses(...)` for its error statuses. Several `tests/test_*_openapi.py`
  files enforce this per module.
* Query and path parameters use `Annotated[..., Query(description=...)]` /
  `Path(description=...)`.
* Return `ok(data, "Message")` or `ok_page(items, next_cursor)` (`app/schemas/common.py`);
  success messages are short past-tense sentences without a full stop (`"Grade created"`).
* Writes take `ctx: ContextDep` (the `RequestContext`: IP, user agent, request id) and pass it
  to the service for the audit row.
* Static paths must be registered before `{id}` paths (`app/api/router.py` comments).

## Schemas (Pydantic 2)

* Request bodies subclass `RequestModel` (`extra="forbid"`, `str_strip_whitespace=True`).
  PATCH bodies are applied with `model_dump(exclude_unset=True)` so omitted fields keep their
  value and explicit `null` clears.
* Field constraints and OpenAPI text live on the field: `Field(ge=..., le=..., description=...)`
  (about 900 `Field(... description=...)` uses in `app/schemas/`). Reusable constrained types are
  `Annotated` aliases: `Money`, `RateBp`, `SettingsCode` in `app/schemas/settings.py`.
* Field validators are shared functions attached with `field_validator("x")(fn)`
  (`upper_code`, `blank_is_null` in `app/schemas/settings.py`).
* Read models expose only what the caller may see; masking (for example cost rates without
  `rate.read`) happens when the read model is built (`GradeRead.from_model(..., show_cost=...)`).

## Services

* Constructor takes the `AsyncSession` and builds its repositories
  (`GradeService.__init__`).
* Public methods take `ctx: OrgContext` first, then ids and data, then `rctx: RequestContext`.
* Each write is one `async with unit_of_work(self.session):` block (`app/db/session.py`); read
  the row with `lock=True` (`FOR UPDATE`) before changing it. Services do not call
  `session.commit()` themselves (only `integration_logs.py` and `rate_limit.py` do, for
  records that must survive a rollback).
* `apply_changes(obj, values)` (`app/services/common.py`) sets fields and returns
  `{field: [old, new]}` for the audit row.
* Dated writes to time, expenses or WIP call `period_locks.assert_open(...)` inside the unit of
  work before writing (409 `PERIOD_LOCKED`).
* Side effects that leave the process (email, integrations) are enqueued as background jobs or
  outbox items in the same transaction, never performed inline.
* Notifications go through `app/services/notification_events.py`, which isolates failures in a
  savepoint.

## Errors

* Raise `AppException` subclasses from `app/core/exceptions.py`: `NotFoundError`,
  `ConflictError`, `ValidationError`, `AuthorizationError`, ... with `code=` for anything a
  client may act on. Never raise FastAPI `HTTPException` from a service.
* Messages are full sentences for the end user (`"Grade not found."`), never internal details.
* Field-level problems carry `details=[{"field", "code", "message"}]`:
  `ValidationError.for_field(...)`, `duplicate_error(field, message, code)`
  (`app/services/common.py`).
* Another practice's record is 404, never 403.
* New error codes go into the module doc's "Error codes" section.

## Money, rates and dates

* Money is `int` pence; arithmetic only through `app/utils/money.py` (`time_value`, `vat_on`,
  `line_net`, `percentage_bp`, `apportion`), which works in `Decimal` and rounds half up. Do not
  use `float` for money.
* Percentages and VAT rates are basis points (`*_bp`); durations are whole minutes.
* Calendar dates are in the practice's time zone (`app/utils/practice_dates.py: today_in`,
  `start_of_day_utc`); timestamps are timezone-aware UTC (`datetime.now(UTC)`).
* Rates and VAT are snapshotted on the record when it is saved; changing a rate never rewrites
  history (re-rating is an explicit operation, [team-rates.md](modules/team-rates.md#re-rating-unbilled-time)).

## Audit

* Every create, update, delete and state change of a business record writes an audit row in the
  same transaction: `record_audit(session, ctx, rctx, action, target_type, target_id, meta)`,
  or `record_audit_many` for bulk actions (`app/services/common.py`).
* `meta` names the record (code, number, name) and, for updates, includes `changes`. Cut long
  text with `audit_excerpt`.
* New actions or record types that need a label or a read permission go into
  `app/services/audit_catalogue.py`.

## Permissions

* Gate the route with `require("<code>")`; check own-versus-others and field visibility in the
  service with `app/services/policy.py` (`has_permission`, `ensure`, `ensure_any`).
* Check permission codes, never role names.
* A new permission needs the catalogue entry, a migration calling `sync_permissions()`, and a
  row in `docs/modules/permissions.md` ([database.md](database.md#adding-a-migration)).

## Database access

* Every repository query filters by `organization_id`; repository methods take it as the first
  argument.
* List endpoints use keyset pagination (`keyset_query` / `keyset_finish` in
  `app/repositories/time_paging.py`, cursors in `app/utils/pagination.py`).
* Use `populate_existing=True` when re-reading a row the session may already hold
  (`GradeRepository.get`).
* Raw SQL (`text(...)`) is used where it is clearer (worker housekeeping, tests); bind parameters,
  never format values into the string.

## Typing and lint

| Tool | Configuration (`pyproject.toml`) | Command |
|---|---|---|
| mypy | `strict = true`, `pydantic.mypy` plugin, `python_version = "3.14"`; `alembic/` and `tests/` excluded | `mypy app` |
| ruff lint | rules `E F W I N UP B S C4 SIM ASYNC RUF`, `S101` ignored; looser rules for `tests/**` and `alembic/**` | `ruff check .` |
| ruff format | line length 100 | `ruff format --check .` |

* Modern typing: built-in generics, `X | None`, PEP 695 generics and aliases
  (`class ApiResponse[T]`, `type AuditRow = tuple[...]`).
* `# noqa` only with the rule code and, where not obvious, a reason
  (`# noqa: N818  (the documented name)`).

## Docstrings and comments

* Every module starts with a docstring: what it is for, and for services the rules it enforces
  and the module doc it implements (`app/services/grades.py`).
* Comments explain why, not what (`# Added last = outermost: the request id exists before
  anything else runs.` in `app/main.py`).
* British English in prose and messages ("normalise", "practice").

## Commits

From `git log`:

* The main commit of a ticket: `Fix ticket APT79: a Staff rates only rate mode`; several
  tickets: `Fix tickets APT32, APT33, APT34 and APT38`.
* Follow-up commits on the same ticket: `APT74: branches are switched on, and kept on, only with
  an active branch`.
* A commit that adds a migration names it: `Fix ticket APT51: staff cost rate reflected in billing
  (migration 064)`.
* The subject states the behaviour in plain words; the body (when present) explains the
  problem, the fix and what was deliberately left unchanged. Commits made with Claude end with a
  `Co-Authored-By:` trailer.
