"""CSV plumbing shared by the bulk imports and exports (clients, staff, rates, jobs).

* ``extract_csv`` / ``decode_csv``: the CSV bytes of an upload (multipart ``file`` part or a
  raw ``text/csv`` body), limits and text decoding.
* ``parse_table``: header row mapped to fields by name (case, spaces and aliases tolerated),
  unknown columns reported, rows as ``{field: value}`` records.
* Cell parsers (dates, money, percentages) and the row-error helpers.
* ``csv_bytes``: the export writer (UTF-8 BOM, CRLF, formula-injection guard).
"""

import csv
import io
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from email import policy as email_policy
from email.message import EmailMessage
from email.parser import BytesParser
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from app.core.exceptions import AppException, FileUploadError, ValidationError
from app.schemas.clients import ImportRowError

MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_IMPORT_ROWS = 5000
# Room for the multipart envelope around a file of the maximum size.
MAX_UPLOAD_BODY_BYTES = MAX_IMPORT_BYTES + 64 * 1024
CSV_CONTENT_TYPES = ("text/csv", "application/csv", "text/plain", "application/vnd.ms-excel")
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class ImportTooLargeError(AppException):
    status_code = 413
    code = "FILE_TOO_LARGE"
    message = "The file is larger than 5 MB."


class UnsupportedUploadError(AppException):
    status_code = 415
    code = "FILE_TYPE_NOT_ALLOWED"
    message = "Upload a CSV file (multipart/form-data field 'file', or a text/csv body)."


def extract_csv(content_type: str, body: bytes) -> bytes:
    """The CSV bytes of an upload: the ``file`` part of a multipart form, or a raw body."""
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type == "multipart/form-data":
        envelope = (
            b"Content-Type: " + content_type.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n"
        )
        message = BytesParser(policy=email_policy.HTTP).parsebytes(envelope + body)
        if not isinstance(message, EmailMessage) or not message.is_multipart():
            raise FileUploadError("The upload is not a valid multipart form.")
        for part in message.iter_parts():
            if not isinstance(part, EmailMessage):
                continue
            if part.get_param("name", header="content-disposition") == "file":
                payload = part.get_payload(decode=True)
                data = payload if isinstance(payload, bytes) else b""
                break
        else:
            raise FileUploadError("Send the CSV in a form field named 'file'.")
    elif media_type in CSV_CONTENT_TYPES:
        data = body
    else:
        raise UnsupportedUploadError()
    if len(data) > MAX_IMPORT_BYTES:
        raise ImportTooLargeError(details={"max_bytes": MAX_IMPORT_BYTES})
    if not data.strip():
        raise FileUploadError("The file is empty.")
    return data


def decode_csv(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            # Excel on Windows saves "CSV" in the ANSI code page.
            return data.decode("cp1252")
        except UnicodeDecodeError as exc:
            raise FileUploadError("The file is not UTF-8 or Windows-1252 text.") from exc


# --- Parsing -----------------------------------------------------------------------------


def normalise_header(value: str, aliases: dict[str, str]) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return aliases.get(key, key)


@dataclass(slots=True)
class CsvRow:
    line: int
    record: dict[str, str]


@dataclass(slots=True)
class CsvTable:
    columns: list[str]
    ignored: list[str]
    rows: list[CsvRow]


def parse_table(
    text: str,
    *,
    known: Iterable[str],
    aliases: dict[str, str],
    required: Sequence[str] = (),
) -> CsvTable:
    """The header mapped to ``known`` fields and every non-blank row as a record of the
    non-empty cells. Row numbers are 1-based data rows (the header is row 0)."""
    known_set = set(known)
    try:
        reader = csv.reader(io.StringIO(text, newline=""))
        header = next(reader, None)
        if not header or not any(h.strip() for h in header):
            raise FileUploadError("The file has no header row.")
        fields = [normalise_header(h, aliases) for h in header]
        used = [f for f in fields if f in known_set]
        if len(used) != len(set(used)):
            dupes = sorted({f for f in used if used.count(f) > 1})
            raise ValidationError(
                "Two columns map to the same field.",
                code="IMPORT_DUPLICATE_COLUMNS",
                details={"columns": dupes},
            )
        missing = [f for f in required if f not in used]
        if missing:
            raise ValidationError(
                "The file needs " + ", ".join(f"a '{f}' column" for f in missing) + ".",
                code="IMPORT_MISSING_COLUMNS",
                details={"missing": missing},
            )
        ignored = [h for h, f in zip(header, fields, strict=True) if f not in known_set]
        rows: list[CsvRow] = []
        for line, values in enumerate(reader, start=1):
            if not any(v.strip() for v in values):
                continue
            if len(rows) >= MAX_IMPORT_ROWS:
                raise ValidationError(
                    f"The file has more than {MAX_IMPORT_ROWS} rows.",
                    code="IMPORT_TOO_MANY_ROWS",
                    details={"max_rows": MAX_IMPORT_ROWS},
                )
            record = {
                f: v.strip()
                for f, v in zip(fields, values, strict=False)
                if f in known_set and v.strip()
            }
            rows.append(CsvRow(line, record))
    except csv.Error as exc:
        raise FileUploadError(f"The CSV could not be read: {exc}.") from exc
    return CsvTable(used, ignored, rows)


# --- Cell parsers (raise ValueError with a message fit for a row error) -------------------

_DATE_DMY = re.compile(r"^(\d{1,2})[/.](\d{1,2})[/.](\d{4})$")


def parse_date(value: str) -> date:
    """``YYYY-MM-DD`` or ``DD/MM/YYYY``."""
    text = value.strip()
    try:
        match = _DATE_DMY.match(text)
        if match:
            day, month, year = (int(g) for g in match.groups())
            return date(year, month, day)
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("Use YYYY-MM-DD or DD/MM/YYYY.") from None


def _decimal(value: str) -> Decimal:
    text = re.sub(r"[£\s]", "", value.strip())
    if "," in text and "." not in text and re.fullmatch(r"\d+,\d{1,2}", text):
        text = text.replace(",", ".")  # 48,50
    text = text.replace(",", "")
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ValueError("Use a number, e.g. 48.00.") from None
    if not number.is_finite():
        raise ValueError("Use a number, e.g. 48.00.")
    return number


def parse_pence(value: str) -> int:
    """An amount in pounds (``48``, ``48.5``, ``£1,234.50``) as pence."""
    number = _decimal(value)
    if number < 0:
        raise ValueError("The amount cannot be negative.")
    pence = number * 100
    if pence != pence.to_integral_value():
        raise ValueError("Use at most two decimal places.")
    if pence > 10**12:
        raise ValueError("The amount is too large.")
    return int(pence)


def parse_percent_bp(value: str) -> int:
    """A percentage (``75`` or ``75%``) as basis points (7500)."""
    number = _decimal(value.replace("%", ""))
    if number < 0 or number > 100:
        raise ValueError("Use a percentage from 0 to 100.")
    return int((number * 100).to_integral_value())


def parse_int(value: str, *, low: int, high: int) -> int:
    try:
        number = int(value.strip())
    except ValueError:
        raise ValueError("Use a whole number.") from None
    if number < low or number > high:
        raise ValueError(f"Use a whole number from {low} to {high}.")
    return number


def split_list(value: str) -> list[str]:
    return [p.strip() for p in re.split(r"[;|,]", value) if p.strip()]


def enum_value(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


# --- Row errors --------------------------------------------------------------------------


def row_error(line: int, field: str, code: str, message: str) -> ImportRowError:
    return ImportRowError(row=line, field=field, code=code, message=message)


def pydantic_row_errors(
    line: int, exc: PydanticValidationError, rename: dict[str, str] | None = None
) -> list[ImportRowError]:
    """The row errors of a request model that rejected a row (``rename``: model field -> column)."""
    found: list[ImportRowError] = []
    for err in exc.errors():
        loc = [str(p) for p in err.get("loc", ())]
        message = str(err.get("msg", "Invalid value.")).removeprefix("Value error, ")
        field = ".".join(loc)
        if not field and ": " in message:
            candidate, _, rest = message.partition(": ")
            if candidate.isidentifier():
                field, message = candidate, rest
        field = (rename or {}).get(field, field)
        found.append(row_error(line, field or "row", str(err.get("type", "invalid")), message))
    return found


def app_row_errors(
    line: int, exc: AppException, default_field: str, rename: dict[str, str] | None = None
) -> list[ImportRowError]:
    """A domain error raised while writing one row, as row errors."""
    renames = rename or {}
    details = exc.details
    if isinstance(details, list) and details and all(isinstance(d, dict) for d in details):
        return [
            row_error(
                line,
                renames.get(str(d.get("field", default_field)), str(d.get("field", default_field))),
                str(d.get("code", exc.code)),
                str(d.get("message", exc.message)),
            )
            for d in details
        ]
    if isinstance(details, dict) and isinstance(details.get("field"), str):
        field = str(details["field"])
        return [row_error(line, renames.get(field, field), exc.code, exc.message)]
    return [row_error(line, default_field, exc.code, exc.message)]


def apply_caster(
    line: int,
    record: dict[str, str],
    field: str,
    cast: Callable[[str], Any],
    errors: list[ImportRowError],
) -> Any:
    """``cast(record[field])`` or None; a ValueError becomes a row error on ``field``."""
    raw = record.get(field)
    if raw is None:
        return None
    try:
        return cast(raw)
    except ValueError as exc:
        errors.append(row_error(line, field, "invalid", str(exc)))
        return None


# --- Export ------------------------------------------------------------------------------


def csv_cell(value: Any) -> Any:
    """Neutralise spreadsheet formulas in text cells (CSV injection)."""
    if value is None:
        return ""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, str) and value.startswith(CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


def csv_bytes(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> bytes:
    """A CSV file (UTF-8 with BOM for Excel, CRLF line ends, formula-safe cells)."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(csv_cell(v) for v in row)
    return out.getvalue().encode("utf-8-sig")


def pounds(pence: int | None) -> str:
    """Pence as pounds with two decimals (``4850`` -> ``48.50``); blank for None."""
    if pence is None:
        return ""
    return f"{pence // 100}.{pence % 100:02d}"
