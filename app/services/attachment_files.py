"""What may be attached to a ticket: file names, types and the checks of an upload.

A file is accepted only when its extension is on the allow-list, its *content* is what the
extension says (magic bytes, never the name or the client's content type) and the content type
the client declared, if it declared a specific one, is one that kind of file really has. The
stored content type is the one this module derives, so a download can never be served as
something the file is not.
"""

import hashlib
import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

from app.core.exceptions import AppException
from app.db.models.tickets import MAX_ATTACHMENT_BYTES

# Room for the multipart envelope (boundary, part headers, the other form fields).
MAX_UPLOAD_BODY_BYTES = MAX_ATTACHMENT_BYTES + 64 * 1024
MAX_FILE_NAME_LENGTH = 150
_ALLOWED_TYPES_MESSAGE = "Attach images, PDF, DOC/DOCX, XLS/XLSX or CSV files."

_PNG = b"\x89PNG\r\n\x1a\n"
_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP = b"PK\x03\x04"
# The directory of an OLE2 file names its streams in UTF-16: a Word file has ``WordDocument``,
# an Excel file ``Workbook`` (or ``Book`` before Excel 97).
_WORD_STREAM = "WordDocument".encode("utf-16-le")
_EXCEL_STREAMS = tuple(name.encode("utf-16-le") for name in ("Workbook", "Book"))
_WORD = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_SHEET = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_ZIPS = ("application/zip", "application/x-zip-compressed")
# Categories dropped from a file name: control and format characters (a right-to-left override
# can make ``exe.png`` read as ``gnp.exe``), line and paragraph separators, unassigned code points.
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
_WHITESPACE = re.compile(r"\s+")
_RESERVED = re.compile(r'[<>:"|?*]')
# What a text file must not contain: NUL and the other control characters but tab, CR and LF.
_TEXT_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class AttachmentTooLargeError(AppException):
    status_code = 413
    code = "ATTACHMENT_TOO_LARGE"
    message = "The file is larger than 2 MB."

    def __init__(self) -> None:
        super().__init__(details={"max_bytes": MAX_ATTACHMENT_BYTES})


class AttachmentTypeError(AppException):
    status_code = 422
    code = "ATTACHMENT_TYPE_NOT_ALLOWED"
    message = _ALLOWED_TYPES_MESSAGE

    def __init__(self, message: str | None = None) -> None:
        text = message or self.message
        super().__init__(
            text, details=[{"field": "file", "code": "type_not_allowed", "message": text}]
        )


class AttachmentEmptyError(AppException):
    status_code = 422
    code = "ATTACHMENT_EMPTY"
    message = "The file is empty."


@dataclass(frozen=True, slots=True)
class FileKind:
    name: str
    content_type: str
    # Content types a client may declare for this kind of file (besides none or the generic
    # ``application/octet-stream``).
    declared: frozenset[str]


_KINDS = {
    kind.name: kind
    for kind in (
        FileKind("png", "image/png", frozenset({"image/png", "image/x-png"})),
        FileKind("jpeg", "image/jpeg", frozenset({"image/jpeg", "image/jpg", "image/pjpeg"})),
        FileKind("gif", "image/gif", frozenset({"image/gif"})),
        FileKind("webp", "image/webp", frozenset({"image/webp"})),
        FileKind("pdf", "application/pdf", frozenset({"application/pdf", "application/x-pdf"})),
        FileKind("docx", _WORD, frozenset({_WORD, *_ZIPS})),
        FileKind("xlsx", _SHEET, frozenset({_SHEET, *_ZIPS})),
        FileKind(
            "doc", "application/msword", frozenset({"application/msword", "application/x-msword"})
        ),
        FileKind(
            "xls",
            "application/vnd.ms-excel",
            frozenset(
                {
                    "application/vnd.ms-excel",
                    "application/x-msexcel",
                    "application/excel",
                    "application/x-excel",
                }
            ),
        ),
        FileKind(
            "csv",
            "text/csv",
            frozenset(
                {
                    "text/csv",
                    "application/csv",
                    "text/x-csv",
                    "application/x-csv",
                    "text/comma-separated-values",
                    "text/plain",
                    # Windows registers Excel for .csv, and browsers then declare this.
                    "application/vnd.ms-excel",
                }
            ),
        ),
    )
}
# The extension allow-list.
EXTENSIONS: dict[str, FileKind] = {
    ".png": _KINDS["png"],
    ".jpg": _KINDS["jpeg"],
    ".jpeg": _KINDS["jpeg"],
    ".gif": _KINDS["gif"],
    ".webp": _KINDS["webp"],
    ".pdf": _KINDS["pdf"],
    ".docx": _KINDS["docx"],
    ".xlsx": _KINDS["xlsx"],
    ".doc": _KINDS["doc"],
    ".xls": _KINDS["xls"],
    ".csv": _KINDS["csv"],
}


@dataclass(frozen=True, slots=True)
class InspectedFile:
    file_name: str
    content_type: str
    size_bytes: int
    sha256: str


def sanitise_file_name(raw: str) -> str:
    """A name that is safe to store and to show: no path, no control or format characters, none
    of ``<>:"|?*``, collapsed spaces, no leading or trailing dots or spaces, at most
    ``MAX_FILE_NAME_LENGTH`` characters (the extension is kept). Never empty."""
    name = unicodedata.normalize("NFC", raw).replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if unicodedata.category(c) not in _DROPPED_CATEGORIES)
    name = _WHITESPACE.sub(" ", _RESERVED.sub("_", name)).strip(" .")
    if len(name) > MAX_FILE_NAME_LENGTH:
        suffix = PurePosixPath(name).suffix
        suffix = suffix if len(suffix) <= 20 else ""
        name = name[: MAX_FILE_NAME_LENGTH - len(suffix)].rstrip(" .") + suffix
    return name or "file"


def _package_kinds(data: bytes) -> frozenset[str]:
    """The Office Open XML kinds of a ZIP: it needs ``[Content_Types].xml`` and the folder of
    its application (``word/`` or ``xl/``); any other ZIP is not a document."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
    except (zipfile.BadZipFile, ValueError, NotImplementedError):
        return frozenset()
    if "[Content_Types].xml" not in names:
        return frozenset()
    kinds = set()
    if any(name.startswith("word/") for name in names):
        kinds.add("docx")
    if any(name.startswith("xl/") for name in names):
        kinds.add("xlsx")
    return frozenset(kinds)


def _is_csv(data: bytes) -> bool:
    """Plain text (UTF-8, or Windows-1252 as Excel saves it) without control characters, that
    is not markup: HTML, SVG and XML start with ``<``."""
    if not data:
        return False
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        return not _TEXT_CONTROLS.search(text) and not text.lstrip().startswith("<")
    return False


def sniff(data: bytes) -> frozenset[str]:
    """The kinds of file the content can be, from its first bytes (and, for text, all of it)."""
    if data.startswith(_PNG):
        return frozenset({"png"})
    if data.startswith(b"\xff\xd8\xff"):
        return frozenset({"jpeg"})
    if data.startswith((b"GIF87a", b"GIF89a")):
        return frozenset({"gif"})
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return frozenset({"webp"})
    if data.startswith(b"%PDF"):
        return frozenset({"pdf"})
    if data.startswith(_OLE2):
        kinds = set()
        if _WORD_STREAM in data:
            kinds.add("doc")
        if any(stream in data for stream in _EXCEL_STREAMS):
            kinds.add("xls")
        return frozenset(kinds)
    if data.startswith(_ZIP):
        return _package_kinds(data)
    return frozenset({"csv"}) if _is_csv(data) else frozenset()


def inspect_upload(file_name: str, declared_type: str | None, data: bytes) -> InspectedFile:
    """Validate one uploaded file and describe it, or raise 413 / 422 with a stable code."""
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise AttachmentTooLargeError()
    if not data:
        raise AttachmentEmptyError(
            details=[{"field": "file", "code": "empty", "message": AttachmentEmptyError.message}]
        )
    name = sanitise_file_name(file_name)
    kind = EXTENSIONS.get(PurePosixPath(name).suffix.lower())
    if kind is None:
        raise AttachmentTypeError(f"This type of file is not allowed. {_ALLOWED_TYPES_MESSAGE}")
    if kind.name not in sniff(data):
        raise AttachmentTypeError("The content of the file is not what its name says.")
    declared = (declared_type or "").partition(";")[0].strip().lower()
    if declared and declared != "application/octet-stream" and declared not in kind.declared:
        raise AttachmentTypeError("The declared content type does not match the file.")
    return InspectedFile(
        file_name=name,
        content_type=kind.content_type,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
