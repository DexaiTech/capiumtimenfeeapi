"""What may be uploaded as an expense receipt: a PDF, JPEG or PNG of at most 10 MiB.

The checks are those of ticket attachments (see ``attachment_files``) with the receipt rules: the
extension must be ``.pdf``, ``.jpg``, ``.jpeg`` or ``.png``, the *content* must be what the
extension says (magic bytes, never the name or the client's content type) and a content type the
client declares must fit. The stored content type is the one derived here.
"""

import hashlib
from pathlib import PurePosixPath

from app.core.exceptions import AppException
from app.db.models.expenses import MAX_RECEIPT_BYTES
from app.services.attachment_files import EXTENSIONS, InspectedFile, sanitise_file_name, sniff

# Room for the multipart envelope (boundary, part headers).
MAX_RECEIPT_BODY_BYTES = MAX_RECEIPT_BYTES + 64 * 1024
RECEIPT_EXTENSIONS = frozenset({".pdf", ".jpg", ".jpeg", ".png"})
_TYPES_MESSAGE = "Attach a PDF, JPG or PNG receipt."


class ReceiptTooLargeError(AppException):
    status_code = 413
    code = "FILE_TOO_LARGE"
    message = "The file is larger than 10 MB."

    def __init__(self) -> None:
        super().__init__(details={"max_bytes": MAX_RECEIPT_BYTES})


class ReceiptTypeError(AppException):
    status_code = 422
    code = "FILE_TYPE_NOT_ALLOWED"
    message = _TYPES_MESSAGE

    def __init__(self, message: str | None = None) -> None:
        text = message or self.message
        super().__init__(
            text, details=[{"field": "file", "code": "type_not_allowed", "message": text}]
        )


class ReceiptEmptyError(AppException):
    status_code = 422
    code = "FILE_EMPTY"
    message = "The file is empty."

    def __init__(self) -> None:
        super().__init__(
            details=[{"field": "file", "code": "empty", "message": ReceiptEmptyError.message}]
        )


def inspect_receipt(file_name: str, declared_type: str | None, data: bytes) -> InspectedFile:
    """Validate one uploaded receipt and describe it, or raise 413 / 422 with a stable code."""
    if len(data) > MAX_RECEIPT_BYTES:
        raise ReceiptTooLargeError()
    if not data:
        raise ReceiptEmptyError()
    name = sanitise_file_name(file_name)
    extension = PurePosixPath(name).suffix.lower()
    if extension not in RECEIPT_EXTENSIONS:
        raise ReceiptTypeError(f"This type of file is not allowed. {_TYPES_MESSAGE}")
    kind = EXTENSIONS[extension]
    if kind.name not in sniff(data):
        raise ReceiptTypeError("The content of the file is not what its name says.")
    declared = (declared_type or "").partition(";")[0].strip().lower()
    if declared and declared != "application/octet-stream" and declared not in kind.declared:
        raise ReceiptTypeError("The declared content type does not match the file.")
    return InspectedFile(
        file_name=name,
        content_type=kind.content_type,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
