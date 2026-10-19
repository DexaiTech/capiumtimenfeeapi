"""The audit-log export: one row per entry, as CSV or as an Excel workbook.

The export holds the newest ``MAX_EXPORT_ROWS`` entries that match the filters; a larger result
is cut (newest kept) and says so (a note in the workbook, ``X-Export-Truncated`` on the response).
Timestamps are ISO 8601 in UTC. Text a spreadsheet would read as a formula is prefixed with an
apostrophe by the shared CSV and Excel writers.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel

from app.schemas.audit import AuditChange, AuditFilters
from app.services import report_csv
from app.services.xlsx_export import XLSX_MEDIA_TYPE, XlsxColumn, render_xlsx

MAX_EXPORT_ROWS = 50_000
CSV_MEDIA_TYPE = "text/csv; charset=utf-8"
# Excel holds 32,767 characters in a cell.
CELL_LENGTH = 30_000

ExportFormat = Literal["csv", "xlsx"]

# (key, header, width in the workbook)
_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("time", "Time (UTC)", 21),
    ("user", "User", 24),
    ("email", "Email", 30),
    ("action", "Action", 26),
    ("event", "Event", 30),
    ("summary", "Summary", 50),
    ("record_type", "Record type", 16),
    ("record_id", "Record id", 38),
    ("changes", "Changes", 50),
    ("ip_address", "IP address", 16),
    ("device", "Device", 32),
    ("request_id", "Request id", 24),
    ("details", "Details (JSON)", 60),
)


class AuditExportRow(BaseModel):
    time: str
    user: str
    email: str
    action: str
    event: str
    summary: str
    record_type: str
    record_id: str
    changes: str
    ip_address: str
    device: str
    request_id: str
    details: str


@dataclass(frozen=True, slots=True)
class AuditExport:
    content: bytes
    media_type: str
    filename: str
    row_count: int
    truncated: bool


def _cut(text: str) -> str:
    return text if len(text) <= CELL_LENGTH else text[: CELL_LENGTH - 1] + "…"


def _show(value: Any) -> str:
    if value is None:
        return "(empty)"
    if isinstance(value, str):
        return value if value else "(empty)"
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def format_changes(changes: list[AuditChange]) -> str:
    """``status: planned → in_progress; fee: 100 → 120``."""
    return _cut("; ".join(f"{c.field}: {_show(c.before)} → {_show(c.after)}" for c in changes))


def timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_row(
    *,
    at: datetime,
    user: str | None,
    email: str | None,
    action: str,
    event: str,
    summary: str,
    record_type: str | None,
    record_id: str | None,
    changes: list[AuditChange],
    ip_address: str | None,
    device: str | None,
    request_id: str | None,
    details: dict[str, Any],
) -> AuditExportRow:
    return AuditExportRow(
        time=timestamp(at),
        user=user or "",
        email=email or "",
        action=action,
        event=event,
        summary=summary,
        record_type=record_type or "",
        record_id=record_id or "",
        changes=format_changes(changes),
        ip_address=ip_address or "",
        device=_cut(device or ""),
        request_id=request_id or "",
        details=_cut(json.dumps(details, ensure_ascii=False, separators=(",", ":"), default=str)),
    )


def describe_filters(filters: AuditFilters, actor_name: str | None) -> str:
    """The filters in words, for the notes of the workbook ("none" when there are none)."""
    parts = [
        ("Action", filters.action),
        ("User", actor_name or (str(filters.actor_user_id) if filters.actor_user_id else None)),
        ("From", filters.created_from.isoformat() if filters.created_from else None),
        ("To", filters.created_to.isoformat() if filters.created_to else None),
        ("Record type", filters.target_type),
        ("Record", str(filters.target_id) if filters.target_id else None),
        ("Search", filters.q),
    ]
    return ", ".join(f"{name}: {value}" for name, value in parts if value) or "none"


def filename(fmt: ExportFormat, today: str) -> str:
    return f"audit-log-{today}.{fmt}"


def render(
    fmt: ExportFormat, rows: list[AuditExportRow], *, notes: list[str] | None = None
) -> tuple[bytes, str]:
    """The file and its media type. Pure CPU work: run it off the event loop."""
    if fmt == "xlsx":
        columns = [XlsxColumn(key, header, "text", width) for key, header, width in _COLUMNS]
        content = render_xlsx(
            columns, rows, sheet="Audit log", title="Audit log", notes=notes or ()
        )
        return content, XLSX_MEDIA_TYPE
    csv_columns = [report_csv.Column(key, header, "text") for key, header, _ in _COLUMNS]
    return report_csv.render(csv_columns, rows, None), CSV_MEDIA_TYPE
