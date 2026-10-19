"""The opening-balance time import (`POST /organizations/{id}/imports/opening-balance-time`).

Historic unbilled time from a client's previous system, imported from a CSV file: client,
staff, date, hours, activity and job are resolved by code/email/number rather than id, since
the file is written by hand. Validated like the normal time-entry create, plus the 100-row
limit of a single bulk time-entry create (which this import uses under the hood so that rate
resolution, timesheet linkage and period locks all use the same, already-correct logic as
ordinary time entry). ``dry_run`` returns the per-row errors and writes nothing; a real run is
all-or-nothing and tags every row created ``is_opening_balance``.
"""

import uuid
from datetime import date

from pydantic import BaseModel, Field

# The same 100-row cap as `TimeEntryBulkCreate`, which this import calls into: see
# `app/services/opening_balance_import.py`.
MAX_OPENING_BALANCE_ROWS = 100


class OpeningBalanceRowError(BaseModel):
    row: int = Field(description="1-based data row number (the header is row 0)")
    field: str
    code: str
    message: str


class ImportedOpeningBalanceTime(BaseModel):
    row: int
    client_code: str
    staff_email: str
    entry_date: date
    minutes: int
    id: uuid.UUID | None = None


class OpeningBalanceTimeImportResult(BaseModel):
    dry_run: bool
    total_rows: int
    valid_rows: int
    created: int
    columns: list[str]
    ignored_columns: list[str]
    errors: list[OpeningBalanceRowError]
    entries: list[ImportedOpeningBalanceTime]
