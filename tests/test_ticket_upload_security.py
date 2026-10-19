"""Upload security: the 2 MB limit enforced while reading, real content checks, hostile names,
malformed forms. Nothing a client says about a file (its name, its type) is trusted."""

import pytest

from app.db.models.tickets import MAX_ATTACHMENT_BYTES
from app.services.attachment_files import MAX_UPLOAD_BODY_BYTES
from tests.conftest import STORAGE_DIR
from tests.factories import error
from tests.ticket_helpers import (
    DOCX_TYPE,
    SAMPLES,
    XLSX_TYPE,
    attachment_rows,
    make_ticket,
    multipart_body,
    new_team,
    ole2,
    ooxml,
    pad_to,
    post_multipart,
    stored_files,
    ticket_url,
    upload,
    zip_bytes,
)
from tests.time_helpers import ok_data

PNG = SAMPLES["png"].data
JPEG = SAMPLES["jpg"].data
PDF = SAMPLES["pdf"].data
HTML = b"<!doctype html><html><body><script>alert(document.cookie)</script></body></html>"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><script>alert(1)</script></svg>'
EXE = b"MZ\x90\x00\x03" + b"\x00" * 200
ELF = b"\x7fELF\x02\x01\x01" + b"\x00" * 200
CHUNK = 64 * 1024


async def setup(client):
    t = await new_team(client)
    ticket = await make_ticket(client, t.owner, t.org)
    return t, ticket


async def try_upload(client, t, ticket, name, data, content_type="image/png"):
    return await upload(client, t.owner, t.org, ticket["id"], name, data, content_type)


def refused_for_type(response) -> None:
    assert response.status_code == 422, response.text
    assert error(response)["code"] == "ATTACHMENT_TYPE_NOT_ALLOWED"
    assert error(response)["details"][0]["field"] == "file"


# --- The size limit -------------------------------------------------------------------------


async def test_a_file_of_exactly_2_mb_is_accepted(client):
    t, ticket = await setup(client)
    response = await try_upload(client, t, ticket, "big.png", pad_to(MAX_ATTACHMENT_BYTES))
    data = ok_data(response, 201)
    assert data["size_bytes"] == MAX_ATTACHMENT_BYTES == 2_097_152
    [file] = stored_files()
    assert file.stat().st_size == MAX_ATTACHMENT_BYTES


@pytest.mark.parametrize(
    "size", [MAX_ATTACHMENT_BYTES + 1, MAX_ATTACHMENT_BYTES + 100_000, 3 * MAX_ATTACHMENT_BYTES]
)
async def test_a_bigger_file_answers_413_with_a_stable_code_and_stores_nothing(client, size):
    t, ticket = await setup(client)
    response = await try_upload(client, t, ticket, "big.png", pad_to(size))
    assert response.status_code == 413, response.text
    assert error(response)["code"] == "ATTACHMENT_TOO_LARGE"
    assert error(response)["details"] == {"max_bytes": MAX_ATTACHMENT_BYTES}
    assert stored_files() == [] and await attachment_rows() == []


async def test_reading_stops_at_the_limit_whatever_the_client_keeps_sending(client):
    t, ticket = await setup(client)
    sent = 0
    head = (
        b'--B\r\nContent-Disposition: form-data; name="file"; filename="big.png"\r\n'
        b"Content-Type: image/png\r\n\r\n" + PNG
    )

    async def endless():  # ten megabytes, if anyone keeps reading
        nonlocal sent
        yield head
        for _ in range(160):
            sent += 1
            yield b"\x00" * CHUNK
        yield b"\r\n--B--\r\n"

    response = await client.post(
        ticket_url(t.org, ticket["id"], "/attachments"),
        headers={**t.owner.headers, "Content-Type": "multipart/form-data; boundary=B"},
        content=endless(),
    )
    assert response.status_code == 413
    assert error(response)["code"] == "ATTACHMENT_TOO_LARGE"
    # The body was read up to the limit plus the envelope, and not a chunk more.
    assert sent <= MAX_UPLOAD_BODY_BYTES // CHUNK + 2
    assert sent < 160
    assert stored_files() == []


async def test_a_declared_length_over_the_limit_is_refused_without_reading(client):
    t, ticket = await setup(client)
    body, content_type = multipart_body([("file", "a.png", "image/png", PNG)])
    response = await post_multipart(
        client,
        t.owner,
        t.org,
        ticket["id"],
        body,
        content_type,
        **{"content-length": str(MAX_UPLOAD_BODY_BYTES + 1)},
    )
    assert response.status_code == 413
    assert error(response)["code"] == "ATTACHMENT_TOO_LARGE"
    assert stored_files() == []


async def test_an_unauthorised_upload_is_refused_before_its_body_is_read(client):
    t, ticket = await setup(client)
    viewer = await t.member("viewer")
    sent = 0

    async def body():
        nonlocal sent
        for _ in range(50):
            sent += 1
            yield b"x" * CHUNK

    response = await client.post(
        ticket_url(t.org, ticket["id"], "/attachments"),
        headers={**viewer.headers, "Content-Type": "multipart/form-data; boundary=B"},
        content=body(),
    )
    assert response.status_code == 403
    assert sent <= 1


async def test_an_empty_file_is_refused(client):
    t, ticket = await setup(client)
    response = await try_upload(client, t, ticket, "empty.png", b"")
    assert response.status_code == 422
    assert error(response)["code"] == "ATTACHMENT_EMPTY"
    assert error(response)["details"][0]["field"] == "file"
    assert stored_files() == []


# --- Extensions -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "evil.svg",
        "evil.SVG",
        "evil.html",
        "evil.htm",
        "evil.xhtml",
        "evil.js",
        "evil.exe",
        "evil.dll",
        "evil.php",
        "evil.jsp",
        "evil.zip",
        "evil.txt",
        "evil.json",
        "evil.xml",
        "evil.docm",
        "evil.xlsm",
        "evil.pptx",
        "evil.bat",
        "evil.sh",
        "evil.ps1",
        "noextension",
        ".png",
        "...",
        "evil.png.exe",
        "evil.png.html",
        "archive.tar.gz",
        "name.png/",
    ],
)
async def test_extensions_outside_the_list_are_refused_whatever_the_content(client, name):
    t, ticket = await setup(client)
    refused_for_type(await try_upload(client, t, ticket, name, PNG))
    assert stored_files() == [] and await attachment_rows() == []


# --- Content that is not what it claims -----------------------------------------------------

LIES = [
    ("evil.png", HTML, "image/png"),
    ("evil.png", SVG, "image/svg+xml"),
    ("evil.png", SVG, "image/png"),
    ("evil.jpg", HTML, "image/jpeg"),
    ("evil.gif", SVG, "image/gif"),
    ("evil.webp", HTML, "image/webp"),
    ("evil.pdf", HTML, "application/pdf"),
    ("evil.csv", HTML, "text/csv"),
    ("evil.csv", SVG, "text/csv"),
    ("evil.csv", b"\xef\xbb\xbf  \n<?xml version='1.0'?><x/>", "text/csv"),
    ("evil.csv", b"<html>", "text/plain"),
    ("evil.png", EXE, "image/png"),
    ("evil.pdf", EXE, "application/pdf"),
    ("evil.docx", EXE, DOCX_TYPE),
    ("evil.xlsx", EXE, XLSX_TYPE),
    ("evil.doc", EXE, "application/msword"),
    ("evil.xls", ELF, "application/vnd.ms-excel"),
    ("photo.jpg", PNG, "image/jpeg"),
    ("photo.png", JPEG, "image/png"),
    ("photo.webp", PNG, "image/webp"),
    ("report.pdf", PNG, "application/pdf"),
    ("shot.png", PDF, "image/png"),
    ("data.csv", PNG, "text/csv"),
    ("notes.docx", zip_bytes({"a.txt": "x"}), DOCX_TYPE),
    ("notes.docx", b"PK\x03\x04 this is not really a zip", DOCX_TYPE),
    ("notes.docx", ooxml("xl/workbook.xml"), DOCX_TYPE),
    ("sheet.xlsx", ooxml("word/document.xml"), XLSX_TYPE),
    ("slides.docx", ooxml("ppt/presentation.xml"), DOCX_TYPE),
    ("slides.xlsx", ooxml("ppt/presentation.xml"), XLSX_TYPE),
    ("old.doc", ole2("SomethingElse"), "application/msword"),
    ("old.doc", ole2("Workbook"), "application/msword"),
    ("old.xls", ole2("WordDocument"), "application/vnd.ms-excel"),
    ("old.xls", ole2("PowerPoint Document"), "application/vnd.ms-excel"),
    ("data.csv", b"a,b\n1,\x00\x002\n", "text/csv"),
    ("data.csv", b"\x00", "text/csv"),
    ("data.csv", "a,b\n1,2\n".encode("utf-16"), "text/csv"),
    ("data.csv", bytes(range(256)), "text/csv"),
    ("data.csv", b"a,b\x01\x02\n1,2\n", "text/csv"),
]


@pytest.mark.parametrize(
    ("name", "data", "declared"), LIES, ids=lambda v: v if isinstance(v, str) else ""
)
async def test_content_that_is_not_what_its_name_says_is_refused(client, name, data, declared):
    t, ticket = await setup(client)
    refused_for_type(await try_upload(client, t, ticket, name, data, declared))
    assert stored_files() == [] and await attachment_rows() == []


@pytest.mark.parametrize(
    ("name", "data", "declared"),
    [
        ("shot.png", PNG, "text/html"),
        ("shot.png", PNG, "image/svg+xml"),
        ("shot.png", PNG, "application/pdf"),
        ("shot.png", PNG, "application/x-msdownload"),
        ("report.pdf", PDF, "image/png"),
        ("data.csv", b"a,b\n1,2\n", "application/pdf"),
        ("data.csv", b"a,b\n1,2\n", "text/html"),
    ],
)
async def test_a_declared_content_type_that_contradicts_the_file_is_refused(
    client, name, data, declared
):
    t, ticket = await setup(client)
    refused_for_type(await try_upload(client, t, ticket, name, data, declared))
    assert stored_files() == []


async def test_the_stored_type_is_the_detected_one_never_the_claimed_one(client):
    t, ticket = await setup(client)
    for declared in (
        "application/octet-stream",
        "image/x-png",
        "IMAGE/PNG",
        " image/png ",
        "image/png; charset=binary",
    ):
        data = ok_data(await try_upload(client, t, ticket, "shot.png", PNG, declared), 201)
        assert data["content_type"] == "image/png", declared
    # No content type at all.
    body, content_type = multipart_body([("file", "shot.png", None, PNG)])
    sent = await post_multipart(client, t.owner, t.org, ticket["id"], body, content_type)
    assert ok_data(sent, 201)["content_type"] == "image/png"
    rows = await attachment_rows()
    assert {r["content_type"] for r in rows} == {"image/png"}


@pytest.mark.parametrize(
    ("name", "data", "declared", "stored_as"),
    [
        ("UPPER.PNG", PNG, "image/png", "image/png"),
        ("scan.JPG", JPEG, "image/jpeg", "image/jpeg"),
        ("plain.csv", b"name,amount\nAcme,10\n", "text/plain", "text/csv"),
        ("excel.csv", b"name,amount\nAcme,10\n", "application/vnd.ms-excel", "text/csv"),
        ("one-column.CSV", b"name\nAcme\nZo\xc3\xab\n", "text/csv", "text/csv"),
        ("bom.csv", b"\xef\xbb\xbfname,amount\r\nAcme,10\r\n", "text/csv", "text/csv"),
        (
            "windows.csv",
            "name,amount\nCaf\u00e9,\u00a310\n".encode("cp1252"),
            "text/csv",
            "text/csv",
        ),
        ("zipped.docx", SAMPLES["docx"].data, "application/x-zip-compressed", DOCX_TYPE),
        ("zipped.xlsx", SAMPLES["xlsx"].data, "application/zip", XLSX_TYPE),
        ("scan.pdf", PDF, "application/x-pdf", "application/pdf"),
    ],
)
async def test_honest_files_with_the_usual_quirks_are_accepted(
    client, name, data, declared, stored_as
):
    t, ticket = await setup(client)
    created = ok_data(await try_upload(client, t, ticket, name, data, declared), 201)
    assert created["content_type"] == stored_as and created["file_name"] == name


# --- File names -----------------------------------------------------------------------------

NAMES = [
    (b"../../etc/passwd.png", "passwd.png"),
    (b"..\\..\\windows\\evil.png", "evil.png"),
    (b"/abs/path/x.png", "x.png"),
    (b"C:\\Users\\me\\Desktop\\x.png", "x.png"),
    (b"a\x00b.png", "ab.png"),
    (b"x\x01\x02y.png", "xy.png"),
    (b"tab\there.png", "tabhere.png"),
    ("evil\u202egnp.png".encode(), "evilgnp.png"),
    (b"what<is>this?.png", "what_is_this_.png"),
    (b"pipe|and:colon*.png", "pipe_and_colon_.png"),
    (b"  spaced    name .png", "spaced name .png"),
    (b"UPPER.PNG", "UPPER.PNG"),
    (b"...hidden.png", "hidden.png"),
    (b"trailing.png...", "trailing.png"),
    ("R\u00e9sum\u00e9 \U0001f4ce.png".encode(), "R\u00e9sum\u00e9 \U0001f4ce.png"),
    (b"a" * 300 + b".png", "a" * 146 + ".png"),
]


@pytest.mark.parametrize(("raw", "expected"), NAMES, ids=[repr(r)[:40] for r, _ in NAMES])
async def test_hostile_names_are_cleaned_and_never_reach_the_file_system(client, raw, expected):
    t, ticket = await setup(client)
    body, content_type = multipart_body([("file", raw, "image/png", PNG)])
    response = await post_multipart(client, t.owner, t.org, ticket["id"], body, content_type)
    data = ok_data(response, 201)
    assert data["file_name"] == expected
    # The file lives under a random key in the practice's folder, whatever it was called.
    [file] = stored_files()
    assert file.relative_to(STORAGE_DIR).parts[0] == t.id
    assert len(file.name) == 43 and file.name.isascii()
    assert list(STORAGE_DIR.rglob("*passwd*")) == [] and list(STORAGE_DIR.rglob("*evil*")) == []
    assert not (STORAGE_DIR.parent / "passwd.png").exists()
    assert not (STORAGE_DIR.parent / "evil.png").exists()
    downloaded = await client.get(
        ticket_url(t.org, ticket["id"], f"/attachments/{data['id']}"), headers=t.owner.headers
    )
    assert downloaded.status_code == 200
    disposition = downloaded.headers["content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition and "\x00" not in disposition
    assert disposition.startswith('inline; filename="')


async def test_a_name_with_a_quote_cannot_break_out_of_the_header(client):
    t, ticket = await setup(client)
    # httpx writes a quote in a file name as %22, like a browser.
    response = await try_upload(client, t, ticket, 'say "hi"; filename=evil.exe.png', PNG)
    data = ok_data(response, 201)
    assert '"' not in data["file_name"]
    got = await client.get(
        ticket_url(t.org, ticket["id"], f"/attachments/{data['id']}"), headers=t.owner.headers
    )
    assert got.headers["content-disposition"].count('"') == 2


async def test_a_name_that_tries_to_split_the_part_header_stores_nothing_odd(client):
    t, ticket = await setup(client)
    body, content_type = multipart_body([("file", b"a.png\r\nX-Evil: 1", "image/png", PNG)])
    response = await post_multipart(client, t.owner, t.org, ticket["id"], body, content_type)
    assert response.status_code in (201, 400, 422)
    assert all(row["file_name"] for row in await attachment_rows())


async def test_an_rfc_5987_name_is_decoded_and_wins_over_the_plain_one(client):
    t, ticket = await setup(client)
    head = (
        b'--B\r\nContent-Disposition: form-data; name="file"; filename="plain.png"; '
        b"filename*=UTF-8''caf%C3%A9%20%E2%82%AC.png\r\nContent-Type: image/png\r\n\r\n"
    )
    body = head + PNG + b"\r\n--B--\r\n"
    response = await post_multipart(
        client, t.owner, t.org, ticket["id"], body, "multipart/form-data; boundary=B"
    )
    assert ok_data(response, 201)["file_name"] == "caf\u00e9 \u20ac.png"


# --- Malformed forms ------------------------------------------------------------------------


def _parts_form(*parts):
    return multipart_body(list(parts))


FORMS = {
    "json body": (b'{"file": "x"}', "application/json"),
    "plain text": (PNG, "text/plain"),
    "no boundary": (_parts_form(("file", "a.png", "image/png", PNG))[0], "multipart/form-data"),
    "empty body": (b"", "multipart/form-data; boundary=B"),
    "garbage": (b"this is not a form", "multipart/form-data; boundary=B"),
    "no file part": _parts_form(("comment_id", None, None, b"x")),
    "two files": _parts_form(
        ("file", "a.png", "image/png", PNG), ("file", "b.png", "image/png", PNG)
    ),
    "a field called file": _parts_form(("file", None, None, PNG)),
    "empty file name": _parts_form(("file", "", "image/png", PNG)),
    "blank file name": _parts_form(("file", "   ", "image/png", PNG)),
    "wrong field name": _parts_form(("upload", "a.png", "image/png", PNG)),
    "unterminated": (
        _parts_form(("file", "a.png", "image/png", PNG))[0].removesuffix(b"--\r\n"),
        "multipart/form-data; boundary=----capium-test-boundary",
    ),
    "too many parts": _parts_form(
        *[("x", None, None, b"1")] * 11, ("file", "a.png", "image/png", PNG)
    ),
}


@pytest.mark.parametrize("case", list(FORMS))
async def test_a_malformed_upload_answers_400_and_stores_nothing(client, case):
    t, ticket = await setup(client)
    body, content_type = FORMS[case]
    response = await post_multipart(client, t.owner, t.org, ticket["id"], body, content_type)
    assert response.status_code == 400, response.text
    assert error(response)["code"] == "FILE_INVALID"
    assert stored_files() == [] and await attachment_rows() == []


async def test_a_boundary_may_be_quoted_and_a_preamble_is_ignored(client):
    t, ticket = await setup(client)
    body = (
        b"this preamble is ignored\r\n--B\r\n"
        b'Content-Disposition: form-data; name="file"; filename="ok.png"\r\n'
        b"Content-Type: image/png\r\n\r\n" + PNG + b"\r\n--B--\r\nand so is the epilogue"
    )
    response = await post_multipart(
        client, t.owner, t.org, ticket["id"], body, 'multipart/form-data; boundary="B"'
    )
    assert ok_data(response, 201)["file_name"] == "ok.png"
    [file] = stored_files()
    assert file.read_bytes() == PNG
