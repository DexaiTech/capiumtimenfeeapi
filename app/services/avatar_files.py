"""What may be a profile picture: a PNG, JPEG, WebP or GIF of at most 2 MB.

The checks are those of ticket attachments (see ``attachment_files``) with the picture rules: the
extension must be an image one, the *content* must be what the extension says (magic bytes, never
the name or the client's content type) and a content type the client declares must fit. The stored
content type is the one derived here.
"""

import hashlib
from pathlib import PurePosixPath

from app.core.exceptions import AppException
from app.services.attachment_files import EXTENSIONS, InspectedFile, sanitise_file_name, sniff

MAX_AVATAR_BYTES = 2 * 1024 * 1024
# Room for the multipart envelope (boundary, part headers).
MAX_AVATAR_BODY_BYTES = MAX_AVATAR_BYTES + 64 * 1024
AVATAR_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
_TYPES_MESSAGE = "Choose a PNG, JPG, WebP or GIF picture."


class AvatarTooLargeError(AppException):
    status_code = 413
    code = "FILE_TOO_LARGE"
    message = "The picture is larger than 2 MB."

    def __init__(self) -> None:
        super().__init__(details={"max_bytes": MAX_AVATAR_BYTES})


class AvatarTypeError(AppException):
    status_code = 422
    code = "FILE_TYPE_NOT_ALLOWED"
    message = _TYPES_MESSAGE

    def __init__(self, message: str | None = None) -> None:
        text = message or self.message
        super().__init__(
            text, details=[{"field": "file", "code": "type_not_allowed", "message": text}]
        )


class AvatarEmptyError(AppException):
    status_code = 422
    code = "FILE_EMPTY"
    message = "The picture is empty."

    def __init__(self) -> None:
        super().__init__(
            details=[{"field": "file", "code": "empty", "message": AvatarEmptyError.message}]
        )


def inspect_avatar(file_name: str, declared_type: str | None, data: bytes) -> InspectedFile:
    """Validate one uploaded picture and describe it, or raise 413 / 422 with a stable code."""
    if len(data) > MAX_AVATAR_BYTES:
        raise AvatarTooLargeError()
    if not data:
        raise AvatarEmptyError()
    name = sanitise_file_name(file_name)
    extension = PurePosixPath(name).suffix.lower()
    if extension not in AVATAR_EXTENSIONS:
        raise AvatarTypeError(f"This type of file is not allowed. {_TYPES_MESSAGE}")
    kind = EXTENSIONS[extension]
    if kind.name not in sniff(data):
        raise AvatarTypeError("The content of the file is not what its name says.")
    declared = (declared_type or "").partition(";")[0].strip().lower()
    if declared and declared != "application/octet-stream" and declared not in kind.declared:
        raise AvatarTypeError("The declared content type does not match the file.")
    return InspectedFile(
        file_name=name,
        content_type=kind.content_type,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
