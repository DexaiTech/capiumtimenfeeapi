"""A small ``multipart/form-data`` reader for the upload endpoints.

``python-multipart`` is not a dependency and an upload here is small, so the caller reads the
body into memory under its own cap and this module splits it on the boundary. It is strict:
anything that is not a well-formed form (no closing boundary, parts without headers, more than
``MAX_PARTS`` parts) is refused with ``FileUploadError``.
"""

import re
from dataclasses import dataclass
from urllib.parse import unquote

from app.core.exceptions import FileUploadError

MAX_PARTS = 10
MAX_HEADER_BYTES = 8 * 1024
_BOUNDARY = re.compile(r'boundary\s*=\s*(?:"([^"]{1,200})"|([^\s;,"]{1,200}))', re.IGNORECASE)
_PARAM = re.compile(r';\s*([A-Za-z0-9*-]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^;\s]*))')


@dataclass(frozen=True, slots=True)
class FormPart:
    name: str
    filename: str | None
    content_type: str | None
    data: bytes


def _malformed() -> FileUploadError:
    return FileUploadError("The upload is not a valid multipart form.")


def _unquote_param(quoted: str, plain: str) -> str:
    if quoted:
        # Browsers write a quote in a name as %22 (RFC 7578) and leave backslashes alone, so a
        # Windows path survives; older clients escape a backslash or a quote with a backslash.
        return quoted.replace("\\\\", "\\").replace('\\"', '"').replace("%22", '"')
    return plain


def _parse_headers(block: bytes) -> tuple[str, str | None, str | None]:
    """``(name, filename, content type)`` of a part from its header block."""
    if len(block) > MAX_HEADER_BYTES:
        raise _malformed()
    name, filename, content_type = "", None, None
    for line in block.decode("utf-8", errors="replace").split("\r\n"):
        header, _, value = line.partition(":")
        header = header.strip().lower()
        if header == "content-disposition":
            plain_name: str | None = None
            for key, quoted, bare in _PARAM.findall(";" + value.partition(";")[2]):
                key = key.lower()
                if key == "name":
                    name = _unquote_param(quoted, bare)
                elif key == "filename":
                    plain_name = _unquote_param(quoted, bare)
                elif key == "filename*":
                    # RFC 5987: charset'language'percent-encoded
                    charset, _, rest = bare.partition("'")
                    encoded = rest.partition("'")[2]
                    try:
                        filename = unquote(encoded, encoding=charset or "utf-8", errors="replace")
                    except LookupError:
                        filename = None
            if filename is None:
                filename = plain_name
        elif header == "content-type":
            content_type = value.partition(";")[0].strip().lower() or None
    return name, filename, content_type


def parse_form_data(content_type: str, body: bytes) -> list[FormPart]:
    """The parts of a ``multipart/form-data`` body, in order."""
    media_type, _, parameters = content_type.partition(";")
    if media_type.strip().lower() != "multipart/form-data":
        raise FileUploadError("Send the file as multipart/form-data in a field named 'file'.")
    found = _BOUNDARY.search(parameters)
    if found is None:
        raise _malformed()
    boundary = (found.group(1) or found.group(2)).encode("latin-1", errors="replace")
    # Every boundary is preceded by CRLF; the first may open the body, so one is added.
    segments = (b"\r\n" + body).split(b"\r\n--" + boundary)
    parts: list[FormPart] = []
    closed = False
    for segment in segments[1:]:
        if segment.startswith(b"--"):
            closed = True
            break
        segment = segment.lstrip(b" \t")
        if not segment.startswith(b"\r\n"):
            raise _malformed()
        segment = segment[2:]
        if segment.startswith(b"\r\n"):
            headers, content = b"", segment[2:]
        else:
            headers, separator, content = segment.partition(b"\r\n\r\n")
            if not separator:
                raise _malformed()
        name, filename, part_type = _parse_headers(headers)
        parts.append(FormPart(name=name, filename=filename, content_type=part_type, data=content))
        if len(parts) > MAX_PARTS:
            raise _malformed()
    if not closed:
        raise _malformed()
    return parts
