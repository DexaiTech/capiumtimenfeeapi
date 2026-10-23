"""Result of the bulk CSV imports of staff, rates and jobs (``POST /imports/{staff,rates,jobs}``).

The clients import keeps its own ``ImportResult`` (``app/schemas/clients.py``); the row error
shape is shared.
"""

import uuid

from pydantic import BaseModel, Field

from app.schemas.clients import ImportRowError

__all__ = ["BulkImportResult", "ImportRowError", "ImportedRecord"]


class ImportedRecord(BaseModel):
    row: int = Field(description="1-based data row number")
    code: str | None = Field(
        description="Staff code or job number (also generated ones); null for rates"
    )
    name: str = Field(description="Staff name, job name or a one-line description of the rate")
    id: uuid.UUID | None = Field(default=None, description="Null on a dry run")


class BulkImportResult(BaseModel):
    dry_run: bool
    total_rows: int
    valid_rows: int = Field(description="Rows without an error")
    created: int = Field(description="Records written (0 on a dry run or when rows had errors)")
    columns: list[str] = Field(description="Recognised columns, as the importer names them")
    ignored_columns: list[str] = Field(description="Header cells that match no column")
    errors: list[ImportRowError]
    items: list[ImportedRecord] = Field(
        description="One entry per valid row, in file order (a preview on a dry run)"
    )
    counts: dict[str, int] = Field(
        default_factory=dict,
        description="Further totals: `rates` (staff import), `cost` and `charge` (rates import)",
    )
