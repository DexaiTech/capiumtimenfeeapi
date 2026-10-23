"""The write phase shared by the staff, rate and job imports.

Rows are written one by one, each inside a savepoint, in one transaction. A row that breaks a
rule of the domain (the same rules and messages as the matching create endpoint) rolls back to its
savepoint and becomes a row error. At the end a dry run, or a file with any error, discards the
whole transaction (``Discard``); only a clean real run commits. Because the rows are really
written, checks against earlier rows of the same file (duplicate emails, overlapping rates,
generated numbers) need no special code.
"""

from collections.abc import Awaitable, Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException, ValidationError
from app.schemas.clients import ImportRowError
from app.schemas.imports import BulkImportResult, ImportedRecord
from app.services.import_csv import CsvTable, app_row_errors


class Discard(Exception):  # noqa: N818  (control flow, not an error)
    """Raised inside the unit of work to roll everything back without failing the request."""


async def write_row[T](
    session: AsyncSession,
    line: int,
    default_field: str,
    errors: list[ImportRowError],
    write: Callable[[], Awaitable[T]],
    *,
    rename: dict[str, str] | None = None,
    integrity: Callable[[IntegrityError], AppException | None] | None = None,
) -> T | None:
    """Run ``write`` in a savepoint; a domain error rolls the row back and is recorded."""
    try:
        async with session.begin_nested():
            return await write()
    except AppException as exc:
        errors.extend(app_row_errors(line, exc, default_field, rename))
    except IntegrityError as exc:
        mapped = integrity(exc) if integrity else None
        if mapped is None:
            raise
        errors.extend(app_row_errors(line, mapped, default_field, rename))
    return None


def invalid_error(errors: list[ImportRowError]) -> ValidationError:
    bad_rows = {e.row for e in errors}
    return ValidationError(
        f"{len(bad_rows)} row(s) have errors; nothing was imported.",
        code="IMPORT_INVALID",
        details=[e.model_dump() for e in errors],
    )


def build_result(
    table: CsvTable,
    errors: list[ImportRowError],
    items: list[ImportedRecord],
    *,
    dry_run: bool,
    total_rows: int,
    created: int = 0,
    counts: dict[str, int] | None = None,
) -> BulkImportResult:
    errors.sort(key=lambda e: (e.row, e.field))
    bad_rows = {e.row for e in errors}
    return BulkImportResult(
        dry_run=dry_run,
        total_rows=total_rows,
        valid_rows=total_rows - len(bad_rows),
        created=created,
        columns=table.columns,
        ignored_columns=table.ignored,
        errors=errors,
        items=items,
        counts=counts or {},
    )
