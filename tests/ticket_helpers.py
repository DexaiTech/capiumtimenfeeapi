"""Helpers for the Application Testing tests: tickets through the API, tickets and their status
history inserted directly (for exact dashboard datasets), real minimal sample files and the
storage directory."""

import base64
import io
import itertools
import struct
import uuid
import zipfile
import zlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from httpx import AsyncClient, Response

from tests.clients_helpers import sql
from tests.conftest import STORAGE_DIR
from tests.domain_factories import practice
from tests.factories import API, Actor, add_member
from tests.time_helpers import ok_data

__all__ = [
    "DASHBOARD_KEYS",
    "DETAIL_KEYS",
    "DOCX_TYPE",
    "ITEM_KEYS",
    "SAMPLES",
    "XLSX_TYPE",
    "Sample",
    "Team",
    "add_tag",
    "attachment_rows",
    "insert_ticket",
    "make_ticket",
    "multipart_body",
    "new_team",
    "ole2",
    "ooxml",
    "org_url",
    "pad_to",
    "post_multipart",
    "stored_files",
    "tag_id",
    "ticket_url",
    "upload",
    "utc",
    "zip_bytes",
]

_numbers = itertools.count(1000)

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# The keys of a ticket in the detail and in the list, as documented.
DETAIL_KEYS = {
    "id",
    "code",
    "number",
    "category",
    "subject",
    "status",
    "priority",
    "tags",
    "assignee",
    "created_by",
    "created_by_name",
    "created_at",
    "updated_at",
    "closed_at",
    "comment_count",
    "attachment_count",
    "description",
    "attachments",
}
ITEM_KEYS = (DETAIL_KEYS - {"description", "attachments"}) | {"description_excerpt"}
# The keys of the dashboard.
DASHBOARD_KEYS = {
    "date_from",
    "date_to",
    "previous_from",
    "previous_to",
    "days",
    "kpis",
    "by_status",
    "by_priority",
    "by_tag",
    "trend",
    "top_assignees",
    "recent",
}


def org_url(org: dict, path: str = "") -> str:
    return f"{API}/organizations/{org['id']}{path}"


def ticket_url(org: dict, ticket_id: str | uuid.UUID | None = None, tail: str = "") -> str:
    base = org_url(org, "/tickets")
    return f"{base}/{ticket_id}{tail}" if ticket_id else f"{base}{tail}"


def utc(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


@dataclass
class Team:
    """A practice and its Owner; ``member`` adds a person with a system role on first use."""

    owner: Actor
    org: dict
    members: dict[str, Actor] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return str(self.org["id"])

    def url(self, path: str = "") -> str:
        return org_url(self.org, path)

    async def member(self, role: str) -> Actor:
        if role == "owner":
            return self.owner
        if role not in self.members:
            self.members[role] = await add_member(self.org["id"], role)
        return self.members[role]


async def new_team(client: AsyncClient, name: str = "Acme Accountants") -> Team:
    owner, org = await practice(client, name)
    return Team(owner, org)


async def make_ticket(
    client: AsyncClient, actor: Actor, org: dict, subject: str = "Login page crashes", **body: Any
) -> dict:
    """Create a ticket through the API; returns ``data`` (the detail)."""
    response = await client.post(
        ticket_url(org), headers=actor.headers, json={"subject": subject, **body}
    )
    return ok_data(response, 201)


async def tag_id(org: dict, name: str) -> str:
    rows = await sql(
        "SELECT id FROM ticket_tags WHERE organization_id = :o AND lower(name) = lower(:n)",
        o=org["id"],
        n=name,
    )
    assert rows, f"no tag {name}"
    return str(rows[0]["id"])


async def add_tag(org: dict, name: str, color: str = "grey") -> str:
    """A tag inserted directly (a new practice already has seven)."""
    new_id = uuid.uuid4()
    await sql(
        "INSERT INTO ticket_tags (id, organization_id, name, color) VALUES (:i, :o, :n, :c)",
        i=new_id,
        o=org["id"],
        n=name,
        c=color,
    )
    return str(new_id)


async def insert_ticket(
    org: dict,
    created_by: Actor,
    *,
    created_at: datetime,
    status: str = "open",
    timeline: Sequence[tuple[str, datetime]] | None = None,
    priority: str = "not_set",
    subject: str | None = None,
    number: int | None = None,
    assignee: Actor | None = None,
    tags: Iterable[str] = (),
    description: str | None = None,
) -> str:
    """A ticket written straight into the tables at a chosen time; returns its id.

    ``timeline`` is the whole status history as ``(status, when)``: the first entry is the
    status the ticket was created with (at ``created_at``), the last is its current status.
    Without it the ticket is created in ``status`` and never changes. ``tags`` are names of
    the practice's tags.
    """
    steps = list(timeline) if timeline else [(status, created_at)]
    assert steps[0][1] == created_at
    final = steps[-1][0]
    closed_at = steps[-1][1] if final == "closed" else None
    number = number or next(_numbers)
    ticket_id = uuid.uuid4()
    await sql(
        "INSERT INTO tickets (id, organization_id, number, code, subject, description, status, "
        "priority, assignee_user_id, created_by, created_at, closed_at) VALUES (:i, :o, :n, :c, "
        ":s, :d, :st, :p, :a, :u, :at, :cl)",
        i=ticket_id,
        o=org["id"],
        n=number,
        c=f"APT{number}",
        s=subject or f"Ticket {number}",
        d=description,
        st=final,
        p=priority,
        a=assignee.id if assignee else None,
        u=created_by.id,
        at=created_at,
        cl=closed_at,
    )
    previous: str | None = None
    for to_status, when in steps:
        await sql(
            "INSERT INTO ticket_status_history (id, organization_id, ticket_id, from_status, "
            "to_status, changed_by, changed_at) VALUES (:i, :o, :t, :f, :s, :u, :at)",
            i=uuid.uuid4(),
            o=org["id"],
            t=ticket_id,
            f=previous,
            s=to_status,
            u=created_by.id,
            at=when,
        )
        previous = to_status
    for name in tags:
        await sql(
            "INSERT INTO ticket_tag_links (organization_id, ticket_id, tag_id) "
            "SELECT :o, :t, id FROM ticket_tags WHERE organization_id = :o AND name = :n",
            o=org["id"],
            t=ticket_id,
            n=name,
        )
    return str(ticket_id)


# --- Files ------------------------------------------------------------------------------


def _png() -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )


def _zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def zip_bytes(files: dict[str, str]) -> bytes:
    """A ZIP archive of ``files`` (name -> text)."""
    return _zip(files)


_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////"
    "////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA="
)
_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00,"
    b"\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)
_WEBP = base64.b64decode("UklGRhoAAABXRUJQVlA4TA0AAAAvAAAAEAcQERGIiP4HAA==")
_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 3 3]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)
_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/'
    'package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/></Types>'
)
_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.'
    'org/package/2006/relationships"></Relationships>'
)
_OLE2_HEAD = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def ole2(stream: str) -> bytes:
    """An OLE2 header and a directory entry that names ``stream``, as Word and Excel files do."""
    return _OLE2_HEAD + b"\x00" * 504 + stream.encode("utf-16-le") + b"\x00" * 64


def ooxml(*parts: str) -> bytes:
    """An Office Open XML package that holds the given part names (``word/document.xml``)."""
    files = {"[Content_Types].xml": _CONTENT_TYPES, "_rels/.rels": _RELS}
    files.update(dict.fromkeys(parts, "<x/>"))
    return _zip(files)


class Sample:
    """A real minimal file of one type: a name, the bytes and the content type a client sends."""

    def __init__(self, name: str, data: bytes, content_type: str) -> None:
        self.name = name
        self.data = data
        self.content_type = content_type


SAMPLES: dict[str, Sample] = {
    "png": Sample("shot.png", _png(), "image/png"),
    "jpg": Sample("photo.jpg", _JPEG, "image/jpeg"),
    "jpeg": Sample("photo.jpeg", _JPEG, "image/jpeg"),
    "gif": Sample("anim.gif", _GIF, "image/gif"),
    "webp": Sample("pic.webp", _WEBP, "image/webp"),
    "pdf": Sample("report.pdf", _PDF, "application/pdf"),
    "docx": Sample(
        "notes.docx",
        _zip(
            {
                "[Content_Types].xml": _CONTENT_TYPES,
                "_rels/.rels": _RELS,
                "word/document.xml": "<w/>",
            }
        ),
        DOCX_TYPE,
    ),
    "xlsx": Sample(
        "sheet.xlsx",
        _zip(
            {
                "[Content_Types].xml": _CONTENT_TYPES,
                "_rels/.rels": _RELS,
                "xl/workbook.xml": "<w/>",
            }
        ),
        XLSX_TYPE,
    ),
    "doc": Sample("old.doc", ole2("WordDocument"), "application/msword"),
    "xls": Sample("old.xls", ole2("Workbook"), "application/vnd.ms-excel"),
    "csv": Sample("data.csv", "name,amount\nAcme,10\nZoë,20\n".encode(), "text/csv"),
}


def pad_to(size: int, head: bytes = b"\x89PNG\r\n\x1a\n") -> bytes:
    """``size`` bytes that start like a PNG: the cheapest file of an exact length."""
    assert size >= len(head)
    return head + b"\x00" * (size - len(head))


async def upload(
    client: AsyncClient,
    actor: Actor,
    org: dict,
    ticket_id: str | uuid.UUID,
    name: str = "shot.png",
    data: bytes | None = None,
    content_type: str | None = "image/png",
    *,
    comment_id: str | uuid.UUID | None = None,
    comment_in_form: bool = True,
) -> Response:
    """POST one file as ``multipart/form-data``; the comment id is a form field by default."""
    files = {"file": (name, SAMPLES["png"].data if data is None else data, content_type)}
    form: dict[str, str] = {}
    params: dict[str, str] = {}
    if comment_id is not None:
        (form if comment_in_form else params)["comment_id"] = str(comment_id)
    return await client.post(
        ticket_url(org, ticket_id, "/attachments"),
        headers=actor.headers,
        files=files,
        data=form,
        params=params,
    )


def multipart_body(
    parts: Sequence[tuple[str, str | bytes | None, str | None, bytes]],
    boundary: str = "----capium-test-boundary",
) -> tuple[bytes, str]:
    """A ``multipart/form-data`` body built by hand, byte for byte, as an attacker would send it.

    Each part is ``(field name, file name or None, content type or None, data)``; a file name
    may be bytes, to put control characters in it. Returns the body and the Content-Type.
    """
    out = bytearray()
    for name, filename, content_type, data in parts:
        out += b"--" + boundary.encode() + b"\r\n"
        out += b'Content-Disposition: form-data; name="' + name.encode() + b'"'
        if filename is not None:
            raw = filename if isinstance(filename, bytes) else filename.encode()
            out += b'; filename="' + raw + b'"'
        out += b"\r\n"
        if content_type is not None:
            out += b"Content-Type: " + content_type.encode() + b"\r\n"
        out += b"\r\n" + data + b"\r\n"
    out += b"--" + boundary.encode() + b"--\r\n"
    return bytes(out), f"multipart/form-data; boundary={boundary}"


async def post_multipart(
    client: AsyncClient,
    actor: Actor,
    org: dict,
    ticket_id: str | uuid.UUID,
    body: bytes,
    content_type: str,
    **headers: str,
) -> Response:
    """POST a ready-made body to the upload endpoint."""
    return await client.post(
        ticket_url(org, ticket_id, "/attachments"),
        headers={**actor.headers, "Content-Type": content_type, **headers},
        content=body,
    )


def stored_files() -> list[Path]:
    """Every file in the test storage (the scratch directory of the atomic writes excluded)."""
    return sorted(
        p
        for p in STORAGE_DIR.rglob("*")
        if p.is_file() and ".tmp" not in p.relative_to(STORAGE_DIR).parts
    )


async def attachment_rows(ticket_id: str | uuid.UUID | None = None) -> list[dict]:
    where = "WHERE ticket_id = :t" if ticket_id else ""
    params = {"t": ticket_id} if ticket_id else {}
    return await sql(
        f"SELECT * FROM ticket_attachments {where} ORDER BY created_at, id",  # noqa: S608
        **params,
    )
