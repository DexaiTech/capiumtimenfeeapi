"""Expense receipts: uploading, downloading and deleting files, the 10 MiB and five-a-line limits,
what may be uploaded, hostile names and forms, permissions by role, tenancy, the states in which
a claim takes receipts, what happens to the files in the storage, and the Receipts page."""

import asyncio
import hashlib
import uuid
from urllib.parse import quote

import pytest

from app.core.config import get_settings
from app.db.models.expenses import MAX_RECEIPT_BYTES, MAX_RECEIPTS_PER_LINE
from app.middleware.hardening import _UPLOADS
from app.repositories.expense_receipts import ExpenseReceiptRepository
from app.services.receipt_files import MAX_RECEIPT_BODY_BYTES
from app.services.storage import LocalFileStorage, StorageError
from tests.billing_helpers import count_rows
from tests.clients_helpers import sql
from tests.conftest import STORAGE_DIR
from tests.domain_factories import make_client
from tests.expense_helpers import (
    act,
    add_line,
    claim_url,
    create_claim,
    expense_team,
    post_receipt_multipart,
    receipt_rows,
    receipt_url,
    upload_receipt,
)
from tests.factories import add_member, audit_entries, error
from tests.foundation_helpers import add_custom_role
from tests.ticket_helpers import SAMPLES, multipart_body, pad_to, stored_files
from tests.time_helpers import err, ok_data, set_manager

PNG = SAMPLES["png"].data
JPEG = SAMPLES["jpg"].data
PDF = SAMPLES["pdf"].data
HTML = b"<!doctype html><html><body><script>alert(document.cookie)</script></body></html>"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><script>alert(1)</script></svg>'
EXE = b"MZ\x90\x00\x03" + b"\x00" * 200
CHUNK = 64 * 1024
RECEIPT_KEYS = {
    "id",
    "expense_id",
    "file_name",
    "content_type",
    "size_bytes",
    "is_image",
    "uploaded_by",
    "uploaded_by_name",
    "created_at",
}
ITEM_KEYS = RECEIPT_KEYS | {
    "claim_id",
    "claim_status",
    "claim_title",
    "user_id",
    "user_name",
    "description",
    "incurred_on",
    "gross",
    "client",
}
DETECTED = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "pdf": "application/pdf",
}
IMAGES = {"png", "jpg", "jpeg"}


async def scene(client, lines: int = 1):
    """A practice, its Staff claimant and a draft claim of theirs with ``lines`` lines."""
    team = await expense_team(client)
    staff = await team.member("staff")
    claim = await create_claim(client, staff, team.org, lines=lines)
    return team, staff, claim, [line["id"] for line in claim["lines"]]


async def up(client, org, actor, line_id, kind="png", name=None) -> dict:
    sample = SAMPLES[kind]
    response = await upload_receipt(
        client, actor, org, line_id, name or sample.name, sample.data, sample.content_type
    )
    return ok_data(response, 201)


async def try_upload(client, team, actor, line_id, name, data, declared="image/png"):
    return await upload_receipt(client, actor, team.org, line_id, name, data, declared)


def refused_for_type(response) -> None:
    assert response.status_code == 422, response.text
    assert error(response)["code"] == "FILE_TYPE_NOT_ALLOWED"
    assert error(response)["details"][0]["field"] == "file"


async def nothing_kept() -> None:
    assert stored_files() == [] and await receipt_rows() == []


# --- Upload ---------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(DETECTED))
async def test_every_allowed_type_can_be_uploaded_and_downloaded(client, kind):
    team, staff, claim, [lid] = await scene(client)
    sample = SAMPLES[kind]
    response = await upload_receipt(
        client, staff, team.org, lid, sample.name, sample.data, sample.content_type
    )
    data = ok_data(response, 201)
    assert set(data) == RECEIPT_KEYS and response.json()["message"] == "Receipt uploaded"
    assert data["expense_id"] == lid and data["file_name"] == sample.name
    assert data["content_type"] == DETECTED[kind] and data["size_bytes"] == len(sample.data)
    assert data["is_image"] is (kind in IMAGES)
    assert data["uploaded_by"] == str(staff.id) and data["uploaded_by_name"] == staff.full_name

    [row] = await receipt_rows(lid)
    assert row["sha256"] == hashlib.sha256(sample.data).hexdigest()
    assert row["content_type"] == DETECTED[kind] and row["size_bytes"] == len(sample.data)
    [stored] = stored_files()
    assert stored.read_bytes() == sample.data
    # The practice's own folder, a two-character folder, the random key.
    assert stored.relative_to(STORAGE_DIR).parts == (
        team.id,
        row["storage_key"][:2],
        row["storage_key"],
    )

    got = await client.get(receipt_url(team.org, lid, data["id"]), headers=team.owner.headers)
    assert got.status_code == 200 and got.content == sample.data
    assert got.headers["content-type"].split(";")[0] == DETECTED[kind]
    assert got.headers["x-content-type-options"] == "nosniff"
    assert got.headers["content-length"] == str(len(sample.data))
    assert got.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in got.headers["content-security-policy"]
    assert got.headers["content-disposition"] == (
        f"{'inline' if kind in IMAGES else 'attachment'}; "
        f"filename=\"{sample.name}\"; filename*=UTF-8''{sample.name}"
    )
    # The claim shows it on the line.
    detail = ok_data(await client.get(claim_url(team.org, claim["id"]), headers=staff.headers))
    assert detail["lines"][0]["receipt_count"] == 1 and detail["lines"][0]["receipts"] == [data]


async def test_an_upload_is_audited(client):
    team, staff, claim, [lid] = await scene(client)
    data = await up(client, team.org, staff, lid, "pdf")
    [entry] = await audit_entries(team.id, "expense_receipt.added")
    assert entry["target_type"] == "expense_receipt" and entry["target_id"] == data["id"]
    assert entry["actor_user_id"] == str(staff.id)
    assert entry["metadata"] == {
        "claim_id": claim["id"],
        "expense_id": lid,
        "file_name": "report.pdf",
        "content_type": "application/pdf",
        "size_bytes": len(PDF),
    }
    [row] = await sql(
        "SELECT ip_address::text AS ip, request_id FROM audit_logs "
        "WHERE action = 'expense_receipt.added'"
    )
    assert row["ip"] and row["request_id"]


async def test_storage_keys_are_random_and_not_derived_from_the_name(client):
    team, staff, _, [lid] = await scene(client)
    for _ in range(3):
        await up(client, team.org, staff, lid, name="invoice-0042.png")
    keys = [row["storage_key"] for row in await receipt_rows()]
    assert len(set(keys)) == 3
    assert all(len(k) == 43 and "invoice" not in k and "." not in k for k in keys)
    assert {p.name for p in stored_files()} == set(keys)


# --- What may be uploaded -------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "evil.svg",
        "evil.SVG",
        "evil.html",
        "evil.htm",
        "evil.js",
        "evil.exe",
        "evil.php",
        "evil.zip",
        "evil.txt",
        "evil.csv",
        "evil.docx",
        "evil.xlsx",
        "evil.doc",
        "evil.gif",
        "evil.webp",
        "evil.heic",
        "evil.png.exe",
        "evil.png.html",
        "noextension",
        ".png",
        "...",
    ],
)
async def test_only_pdf_jpg_and_png_are_accepted_whatever_the_content(client, name):
    team, staff, _, [lid] = await scene(client)
    refused_for_type(await try_upload(client, team, staff, lid, name, PNG))
    await nothing_kept()


LIES = [
    ("evil.png", HTML, "image/png"),
    ("evil.png", SVG, "image/svg+xml"),
    ("evil.png", SVG, "image/png"),
    ("evil.jpg", HTML, "image/jpeg"),
    ("evil.jpeg", SVG, "image/jpeg"),
    ("evil.pdf", HTML, "application/pdf"),
    ("evil.png", EXE, "image/png"),
    ("evil.pdf", EXE, "application/pdf"),
    ("photo.jpg", PNG, "image/jpeg"),
    ("photo.png", JPEG, "image/png"),
    ("photo.jpeg", PNG, "image/jpeg"),
    ("report.pdf", PNG, "application/pdf"),
    ("shot.png", PDF, "image/png"),
    ("shot.png", b"GIF89a" + b"\x00" * 20, "image/png"),
]


@pytest.mark.parametrize(
    ("name", "data", "declared"), LIES, ids=lambda v: v if isinstance(v, str) else ""
)
async def test_content_that_is_not_what_its_name_says_is_refused(client, name, data, declared):
    team, staff, _, [lid] = await scene(client)
    refused_for_type(await try_upload(client, team, staff, lid, name, data, declared))
    await nothing_kept()


@pytest.mark.parametrize(
    ("name", "data", "declared"),
    [
        ("shot.png", PNG, "text/html"),
        ("shot.png", PNG, "image/svg+xml"),
        ("shot.png", PNG, "application/pdf"),
        ("shot.png", PNG, "application/x-msdownload"),
        ("report.pdf", PDF, "image/png"),
        ("photo.jpg", JPEG, "image/png"),
    ],
)
async def test_a_declared_content_type_that_contradicts_the_file_is_refused(
    client, name, data, declared
):
    team, staff, _, [lid] = await scene(client)
    refused_for_type(await try_upload(client, team, staff, lid, name, data, declared))
    await nothing_kept()


@pytest.mark.parametrize(
    ("name", "data", "declared", "stored_as"),
    [
        ("shot.png", PNG, "image/x-png", "image/png"),
        ("UPPER.PNG", PNG, "IMAGE/PNG", "image/png"),
        ("shot.png", PNG, " image/png ", "image/png"),
        ("shot.png", PNG, "image/png; charset=binary", "image/png"),
        ("shot.png", PNG, "application/octet-stream", "image/png"),
        ("scan.JPG", JPEG, "image/jpeg", "image/jpeg"),
        ("scan.jpeg", JPEG, "image/pjpeg", "image/jpeg"),
        ("scan.jpg", JPEG, "image/jpg", "image/jpeg"),
        ("scan.pdf", PDF, "application/x-pdf", "application/pdf"),
        ("scan.pdf", PDF, "application/octet-stream", "application/pdf"),
    ],
)
async def test_honest_files_with_the_usual_quirks_are_stored_as_the_detected_type(
    client, name, data, declared, stored_as
):
    team, staff, _, [lid] = await scene(client)
    created = ok_data(await try_upload(client, team, staff, lid, name, data, declared), 201)
    assert created["content_type"] == stored_as and created["file_name"] == name
    [row] = await receipt_rows(lid)
    assert row["content_type"] == stored_as


async def test_a_file_sent_without_a_content_type_is_accepted(client):
    team, staff, _, [lid] = await scene(client)
    body, content_type = multipart_body([("file", "shot.png", None, PNG)])
    response = await post_receipt_multipart(client, staff, team.org, lid, body, content_type)
    assert ok_data(response, 201)["content_type"] == "image/png"


async def test_an_empty_file_is_refused(client):
    team, staff, _, [lid] = await scene(client)
    response = await try_upload(client, team, staff, lid, "empty.png", b"")
    assert response.status_code == 422 and error(response)["code"] == "FILE_EMPTY"
    assert error(response)["details"][0]["field"] == "file"
    await nothing_kept()


# --- The size limit -------------------------------------------------------------------------


async def test_a_file_of_exactly_10_mib_is_accepted(client):
    team, staff, _, [lid] = await scene(client)
    assert MAX_RECEIPT_BYTES == 10 * 1024 * 1024 == 10_485_760
    response = await try_upload(client, team, staff, lid, "big.png", pad_to(MAX_RECEIPT_BYTES))
    data = ok_data(response, 201)
    assert data["size_bytes"] == MAX_RECEIPT_BYTES
    [file] = stored_files()
    assert file.stat().st_size == MAX_RECEIPT_BYTES


@pytest.mark.parametrize("extra", [1, 100_000])
async def test_a_bigger_file_answers_413_with_a_stable_code_and_stores_nothing(client, extra):
    team, staff, _, [lid] = await scene(client)
    response = await try_upload(
        client, team, staff, lid, "big.png", pad_to(MAX_RECEIPT_BYTES + extra)
    )
    assert response.status_code == 413, response.text
    assert error(response)["code"] == "FILE_TOO_LARGE"
    assert error(response)["details"] == {"max_bytes": MAX_RECEIPT_BYTES}
    await nothing_kept()


async def test_reading_stops_at_the_limit_whatever_the_client_keeps_sending(client):
    team, staff, _, [lid] = await scene(client)
    sent = 0
    head = (
        b'--B\r\nContent-Disposition: form-data; name="file"; filename="big.png"\r\n'
        b"Content-Type: image/png\r\n\r\n" + PNG
    )

    async def endless():  # twenty mebibytes, if anyone keeps reading
        nonlocal sent
        yield head
        for _ in range(320):
            sent += 1
            yield b"\x00" * CHUNK
        yield b"\r\n--B--\r\n"

    response = await client.post(
        receipt_url(team.org, lid),
        headers={**staff.headers, "Content-Type": "multipart/form-data; boundary=B"},
        content=endless(),
    )
    assert response.status_code == 413 and error(response)["code"] == "FILE_TOO_LARGE"
    # The body was read up to the limit plus the envelope, and not a chunk more.
    assert sent <= MAX_RECEIPT_BODY_BYTES // CHUNK + 2
    assert sent < 320
    await nothing_kept()


async def test_a_declared_length_over_the_limit_is_refused_without_reading(client):
    team, staff, _, [lid] = await scene(client)
    body, content_type = multipart_body([("file", "a.png", "image/png", PNG)])
    response = await post_receipt_multipart(
        client,
        staff,
        team.org,
        lid,
        body,
        content_type,
        **{"content-length": str(MAX_RECEIPT_BODY_BYTES + 1)},
    )
    assert response.status_code == 413 and error(response)["code"] == "FILE_TOO_LARGE"
    await nothing_kept()


async def drip(client, team, actor, line_id) -> tuple:
    """Upload a body of fifty chunks; returns the response and how many chunks were pulled."""
    sent = 0

    async def body():
        nonlocal sent
        for _ in range(50):
            sent += 1
            yield b"x" * CHUNK

    response = await client.post(
        receipt_url(team.org, line_id),
        headers={**actor.headers, "Content-Type": "multipart/form-data; boundary=B"},
        content=body(),
    )
    return response, sent


async def test_an_upload_that_cannot_succeed_is_refused_before_its_body_is_read(client):
    team, staff, claim, [lid] = await scene(client)
    # Not allowed.
    response, sent = await drip(client, team, await team.member("viewer"), lid)
    assert response.status_code == 403 and sent <= 1
    response, sent = await drip(client, team, await team.member("manager"), lid)
    assert response.status_code == 403 and sent <= 1
    # No such line.
    response, sent = await drip(client, team, staff, uuid.uuid4())
    assert response.status_code == 404 and sent <= 1
    # A claim that waits for a decision takes no receipts.
    await act(client, staff, team.org, claim["id"], "submit")
    response, sent = await drip(client, team, team.owner, lid)
    assert response.status_code == 409 and sent <= 1
    assert error(response)["code"] == "EXPENSE_CLAIM_LOCKED"
    await nothing_kept()


async def test_a_receipt_is_not_held_to_the_global_body_cap_but_json_writes_still_are(
    client, monkeypatch
):
    team, staff, _, [lid] = await scene(client)
    monkeypatch.setattr(get_settings(), "max_request_body_kb", 1)
    created = await try_upload(client, team, staff, lid, "big.png", pad_to(5_000))
    assert ok_data(created, 201)["size_bytes"] == 5_000
    big = await client.post(claim_url(team.org), headers=staff.headers, json={"title": "x" * 5_000})
    assert big.status_code == 413 and error(big)["code"] == "REQUEST_TOO_LARGE"


def test_only_the_receipt_upload_is_exempt_from_the_body_cap():
    base = "/api/v1/organizations/0198c1f2-6c7e-7d3a-9b1e-2f6f0a4d5e11"
    assert _UPLOADS.match(f"{base}/expenses/0198c1f2-6c7e-7d3a-9b1e-2f6f0a4d5e12/receipts")
    for tail in (
        "/expenses/x/receipts/y",
        "/expenses/x/receipts/",
        "/expenses/x/receipts/y/extra",
        "/expenses/x",
        "/expenses",
        "/expenses/a/b/receipts",
        "/expense-claims",
        "/expense-claims/x/lines",
        "/expense-claims/x/submit",
        "/expense-receipts",
    ):
        assert not _UPLOADS.match(base + tail), tail


# --- File names and forms -------------------------------------------------------------------

NAMES = [
    (b"../../etc/passwd.png", "passwd.png"),
    (b"..\\..\\windows\\evil.png", "evil.png"),
    (b"C:\\Users\\me\\Desktop\\x.png", "x.png"),
    (b"a\x00b.png", "ab.png"),
    ("evil\u202egnp.png".encode(), "evilgnp.png"),
    (b"what<is>this?.png", "what_is_this_.png"),
    (b"  spaced    name .png", "spaced name .png"),
    (b"...hidden.png", "hidden.png"),
    ("R\u00e9sum\u00e9 \U0001f4ce.png".encode(), "R\u00e9sum\u00e9 \U0001f4ce.png"),
    (b"a" * 300 + b".png", "a" * 146 + ".png"),
]


@pytest.mark.parametrize(("raw", "expected"), NAMES, ids=[repr(r)[:40] for r, _ in NAMES])
async def test_hostile_names_are_cleaned_and_never_reach_the_file_system(client, raw, expected):
    team, staff, _, [lid] = await scene(client)
    body, content_type = multipart_body([("file", raw, "image/png", PNG)])
    response = await post_receipt_multipart(client, staff, team.org, lid, body, content_type)
    data = ok_data(response, 201)
    assert data["file_name"] == expected
    [file] = stored_files()
    assert file.relative_to(STORAGE_DIR).parts[0] == team.id
    assert len(file.name) == 43 and file.name.isascii()
    assert list(STORAGE_DIR.rglob("*passwd*")) == [] and list(STORAGE_DIR.rglob("*evil*")) == []
    assert not (STORAGE_DIR.parent / "passwd.png").exists()
    downloaded = await client.get(receipt_url(team.org, lid, data["id"]), headers=staff.headers)
    assert downloaded.status_code == 200
    disposition = downloaded.headers["content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition and "\x00" not in disposition
    assert disposition.startswith('inline; filename="')


async def test_a_non_ascii_name_is_sent_in_both_header_forms(client):
    team, staff, _, [lid] = await scene(client)
    name = "Caf\u00e9 receipt \u2013 \u201cQ3\u201d.pdf"
    data = ok_data(await try_upload(client, team, staff, lid, name, PDF, "application/pdf"), 201)
    assert data["file_name"] == name
    got = await client.get(receipt_url(team.org, lid, data["id"]), headers=staff.headers)
    assert got.headers["content-disposition"] == (
        f"attachment; filename=\"Caf_ receipt _ _Q3_.pdf\"; filename*=UTF-8''{quote(name, safe='')}"
    )


async def test_a_name_with_a_quote_cannot_break_out_of_the_header(client):
    team, staff, _, [lid] = await scene(client)
    response = await try_upload(client, team, staff, lid, 'say "hi"; filename=evil.exe.png', PNG)
    data = ok_data(response, 201)
    assert '"' not in data["file_name"]
    got = await client.get(receipt_url(team.org, lid, data["id"]), headers=staff.headers)
    assert got.headers["content-disposition"].count('"') == 2


FORMS = {
    "json body": (b'{"file": "x"}', "application/json"),
    "plain text": (PNG, "text/plain"),
    "no boundary": (
        multipart_body([("file", "a.png", "image/png", PNG)])[0],
        "multipart/form-data",
    ),
    "empty body": (b"", "multipart/form-data; boundary=B"),
    "garbage": (b"this is not a form", "multipart/form-data; boundary=B"),
    "no file part": multipart_body([("note", None, None, b"x")]),
    "two files": multipart_body(
        [("file", "a.png", "image/png", PNG), ("file", "b.png", "image/png", PNG)]
    ),
    "a field called file": multipart_body([("file", None, None, PNG)]),
    "empty file name": multipart_body([("file", "", "image/png", PNG)]),
    "blank file name": multipart_body([("file", "   ", "image/png", PNG)]),
    "wrong field name": multipart_body([("upload", "a.png", "image/png", PNG)]),
}


@pytest.mark.parametrize("case", list(FORMS))
async def test_a_malformed_upload_answers_400_and_stores_nothing(client, case):
    team, staff, _, [lid] = await scene(client)
    body, content_type = FORMS[case]
    response = await post_receipt_multipart(client, staff, team.org, lid, body, content_type)
    assert response.status_code == 400, response.text
    assert error(response)["code"] == "FILE_INVALID"
    await nothing_kept()


# --- Five receipts a line -------------------------------------------------------------------


async def test_a_line_holds_at_most_five_receipts(client):
    team, staff, _, [first, second] = await scene(client, lines=2)
    assert MAX_RECEIPTS_PER_LINE == 5
    for _ in range(5):
        await up(client, team.org, staff, first)
    response = await upload_receipt(client, staff, team.org, first)
    assert response.status_code == 409
    assert error(response)["code"] == "RECEIPT_LIMIT_REACHED"
    assert error(response)["details"] == {"max_receipts": 5}
    assert len(stored_files()) == 5 and len(await receipt_rows()) == 5
    # The other line has its own five, and a place is free again once one is deleted.
    assert (await upload_receipt(client, staff, team.org, second)).status_code == 201
    [victim, *_] = await receipt_rows(first)
    deleted = await client.delete(receipt_url(team.org, first, victim["id"]), headers=staff.headers)
    assert deleted.status_code == 200
    assert (await upload_receipt(client, staff, team.org, first)).status_code == 201


async def test_two_uploads_cannot_both_take_the_last_place(client):
    team, staff, _, [lid] = await scene(client)
    for _ in range(4):
        await up(client, team.org, staff, lid)
    responses = await asyncio.gather(
        *[upload_receipt(client, staff, team.org, lid) for _ in range(4)]
    )
    assert sorted(r.status_code for r in responses) == [201, 409, 409, 409]
    assert len(await receipt_rows()) == 5 and len(stored_files()) == 5


# --- The state of the claim -----------------------------------------------------------------


async def test_receipts_change_only_while_the_claim_is_a_draft_or_returned(client):
    team, staff, claim, [lid] = await scene(client)
    org, owner = team.org, team.owner
    first = await up(client, org, staff, lid)
    await act(client, staff, org, claim["id"], "submit")
    # Waiting for a decision: closed to the claimant and to the Owner alike.
    for actor in (staff, owner):
        up_refused = await upload_receipt(client, actor, org, lid)
        assert up_refused.status_code == 409
        assert error(up_refused)["code"] == "EXPENSE_CLAIM_LOCKED"
        assert error(up_refused)["details"] == {"status": "submitted"}
        gone = await client.delete(receipt_url(org, lid, first["id"]), headers=actor.headers)
        assert gone.status_code == 409 and error(gone)["code"] == "EXPENSE_CLAIM_LOCKED"
    assert len(stored_files()) == 1
    # Returned: open again.
    await act(client, owner, org, claim["id"], "return", json={"reason": "Add the till slip"})
    second = await up(client, org, staff, lid, "pdf")
    deleted = await client.delete(receipt_url(org, lid, first["id"]), headers=staff.headers)
    assert deleted.status_code == 200
    # Approved: closed for good.
    await act(client, staff, org, claim["id"], "submit")
    await act(client, owner, org, claim["id"], "approve")
    refused = await upload_receipt(client, owner, org, lid)
    assert refused.status_code == 409 and error(refused)["details"] == {"status": "approved"}
    gone = await client.delete(receipt_url(org, lid, second["id"]), headers=owner.headers)
    assert gone.status_code == 409
    [file] = stored_files()
    assert file.read_bytes() == PDF
    # Recalling opens it again, an approved claim never.
    waiting = await create_claim(client, staff, org, lines=1)
    waiting_line = waiting["lines"][0]["id"]
    await act(client, staff, org, waiting["id"], "submit")
    await act(client, staff, org, waiting["id"], "recall")
    assert (await upload_receipt(client, staff, org, waiting_line)).status_code == 201


# --- Delete ---------------------------------------------------------------------------------


async def test_the_claimant_deletes_a_receipt_and_it_leaves_the_storage(client):
    team, staff, claim, [lid] = await scene(client)
    kept = await up(client, team.org, staff, lid, "pdf")
    gone = await up(client, team.org, staff, lid, "png")
    assert len(stored_files()) == 2
    response = await client.delete(receipt_url(team.org, lid, gone["id"]), headers=staff.headers)
    assert response.status_code == 200 and response.json()["message"] == "Receipt deleted"
    detail = ok_data(await client.get(claim_url(team.org, claim["id"]), headers=staff.headers))
    assert [r["id"] for r in detail["lines"][0]["receipts"]] == [kept["id"]]
    [file] = stored_files()
    assert file.read_bytes() == PDF
    [entry] = await audit_entries(team.id, "expense_receipt.deleted")
    assert entry["target_type"] == "expense_receipt" and entry["target_id"] == gone["id"]
    assert entry["actor_user_id"] == str(staff.id)
    assert entry["metadata"] == {
        "claim_id": claim["id"],
        "expense_id": lid,
        "file_name": "shot.png",
        "size_bytes": len(PNG),
    }
    again = await client.delete(receipt_url(team.org, lid, gone["id"]), headers=staff.headers)
    assert again.status_code == 404


async def test_deleting_a_line_or_a_claim_removes_its_files_from_the_storage(client):
    team, staff, claim, [first, second] = await scene(client, lines=2)
    org = team.org
    await up(client, org, staff, first, "png")
    await up(client, org, staff, first, "pdf")
    await up(client, org, staff, second, "jpg")
    other = await create_claim(client, staff, org, lines=1)
    await up(client, org, staff, other["lines"][0]["id"], "png", name="keep.png")
    assert len(stored_files()) == 4
    deleted = await client.delete(
        claim_url(org, claim["id"], f"/lines/{first}"), headers=staff.headers
    )
    assert deleted.status_code == 200
    assert len(stored_files()) == 2 and len(await receipt_rows()) == 2
    gone = await client.delete(claim_url(org, claim["id"]), headers=staff.headers)
    assert gone.status_code == 200
    [file] = stored_files()
    assert file.read_bytes() == PNG
    [row] = await receipt_rows()
    assert row["file_name"] == "keep.png"


async def test_a_file_that_cannot_be_removed_does_not_fail_the_delete(client, monkeypatch):
    team, staff, _, [lid] = await scene(client)
    data = await up(client, team.org, staff, lid)

    async def stuck(self, organization_id, key):
        raise StorageError("read-only")

    monkeypatch.setattr(LocalFileStorage, "delete", stuck)
    response = await client.delete(receipt_url(team.org, lid, data["id"]), headers=staff.headers)
    assert response.status_code == 200
    assert await receipt_rows() == []
    assert len(stored_files()) == 1  # an orphan: logged, nothing the caller can do


# --- Download -------------------------------------------------------------------------------


async def test_a_file_the_store_lost_answers_404_with_a_stable_code(client):
    team, staff, _, [lid] = await scene(client)
    data = await up(client, team.org, staff, lid)
    stored_files()[0].unlink()
    response = await client.get(receipt_url(team.org, lid, data["id"]), headers=staff.headers)
    assert response.status_code == 404
    assert error(response)["code"] == "RECEIPT_FILE_MISSING"


async def test_unknown_receipts_are_not_found_and_ids_must_be_ids(client):
    team, staff, _, [first, second] = await scene(client, lines=2)
    org = team.org
    data = await up(client, org, staff, first)
    for call in (
        client.get(receipt_url(org, first, uuid.uuid4()), headers=staff.headers),
        client.get(receipt_url(org, second, data["id"]), headers=staff.headers),  # not its line
        client.delete(receipt_url(org, second, data["id"]), headers=staff.headers),
        client.get(receipt_url(org, uuid.uuid4(), data["id"]), headers=staff.headers),
        client.delete(receipt_url(org, uuid.uuid4(), data["id"]), headers=staff.headers),
        upload_receipt(client, staff, org, uuid.uuid4()),
    ):
        assert (await call).status_code == 404
    for call in (
        client.get(receipt_url(org, first, "nope"), headers=staff.headers),
        client.get(receipt_url(org, "nope", data["id"]), headers=staff.headers),
        client.delete(receipt_url(org, first, "nope"), headers=staff.headers),
        upload_receipt(client, staff, org, "nope"),
    ):
        assert (await call).status_code == 422
    assert len(stored_files()) == 1


# --- Failures leave nothing behind ----------------------------------------------------------


async def test_a_failure_after_the_file_was_stored_removes_it_again(client, monkeypatch):
    team, staff, _, [lid] = await scene(client)

    async def boom(self, receipt):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(ExpenseReceiptRepository, "add", boom)
    response = await upload_receipt(client, staff, team.org, lid)
    assert response.status_code == 500
    monkeypatch.undo()
    await nothing_kept()
    assert (await upload_receipt(client, staff, team.org, lid)).status_code == 201


async def test_a_store_that_cannot_write_answers_503_and_keeps_no_row(client, monkeypatch):
    team, staff, _, [lid] = await scene(client)

    async def disk_full(self, organization_id, key, data):
        raise StorageError("disk full")

    monkeypatch.setattr(LocalFileStorage, "put", disk_full)
    response = await upload_receipt(client, staff, team.org, lid)
    assert response.status_code == 503 and error(response)["code"] == "STORAGE_UNAVAILABLE"
    assert "disk full" not in response.text
    assert (
        await receipt_rows() == [] and await audit_entries(team.id, "expense_receipt.added") == []
    )


# --- Permissions and tenancy ----------------------------------------------------------------

# role -> (upload, download an existing receipt, delete a receipt) on someone else's draft claim
EXPECTED_BY_ROLE = {
    "owner": (201, 200, 200),
    "admin": (201, 200, 200),
    "partner": (201, 200, 200),
    "manager": (403, 200, 403),  # reads everything, writes only their own
    "staff": (403, 403, 403),  # another member of staff
    "billing": (403, 200, 403),
    "viewer": (403, 403, 403),
}


@pytest.mark.parametrize("role", list(EXPECTED_BY_ROLE))
async def test_receipt_permissions_per_system_role(client, role):
    team = await expense_team(client)
    org = team.org
    claimant = await add_member(org["id"], "staff", full_name="Clara Claimant")
    claim = await create_claim(client, team.owner, org, lines=1, user_id=str(claimant.id))
    lid = claim["lines"][0]["id"]
    existing = await up(client, org, claimant, lid, "pdf")
    actor = team.owner if role == "owner" else await add_member(org["id"], role)
    uploaded = await upload_receipt(client, actor, org, lid)
    downloaded = await client.get(receipt_url(org, lid, existing["id"]), headers=actor.headers)
    target = uploaded.json()["data"]["id"] if uploaded.status_code == 201 else existing["id"]
    deleted = await client.delete(receipt_url(org, lid, target), headers=actor.headers)
    got = (uploaded.status_code, downloaded.status_code, deleted.status_code)
    assert got == EXPECTED_BY_ROLE[role]


async def test_the_claimants_manager_may_look_at_the_receipts_but_not_change_them(client):
    team = await expense_team(client)
    org = team.org
    await add_custom_role(
        org["id"],
        "Approver",
        ["organization.read", "member.read", "expense.log", "expense.approve"],
    )
    boss = await add_member(org["id"], "Approver")
    stranger = await add_member(org["id"], "Approver")
    staff = await add_member(org["id"], "staff")
    await set_manager(org, staff, boss)
    claim = await create_claim(client, staff, org, lines=1)
    lid = claim["lines"][0]["id"]
    data = await up(client, org, staff, lid)
    seen = await client.get(receipt_url(org, lid, data["id"]), headers=boss.headers)
    assert seen.status_code == 200 and seen.content == PNG
    assert (await upload_receipt(client, boss, org, lid)).status_code == 403
    deleted = await client.delete(receipt_url(org, lid, data["id"]), headers=boss.headers)
    assert deleted.status_code == 403
    # An approver of somebody else's staff sees nothing of it.
    shut = await client.get(receipt_url(org, lid, data["id"]), headers=stranger.headers)
    assert shut.status_code == 403
    assert len(stored_files()) == 1


async def test_another_practices_receipts_are_not_found(client):
    mine = await expense_team(client)
    theirs = await expense_team(client, "Their Practice")
    their_claim = await create_claim(client, theirs.owner, theirs.org, lines=1)
    their_line = their_claim["lines"][0]["id"]
    secret = await up(client, theirs.org, theirs.owner, their_line, "pdf")
    my_claim = await create_claim(client, mine.owner, mine.org, lines=1)
    my_line = my_claim["lines"][0]["id"]
    for call in (
        # Their line and receipt under my practice's path.
        client.get(receipt_url(mine.org, their_line, secret["id"]), headers=mine.owner.headers),
        client.delete(receipt_url(mine.org, their_line, secret["id"]), headers=mine.owner.headers),
        upload_receipt(client, mine.owner, mine.org, their_line),
        # Their receipt under my line.
        client.get(receipt_url(mine.org, my_line, secret["id"]), headers=mine.owner.headers),
        # Their practice's path: not a practice I belong to.
        client.get(receipt_url(theirs.org, their_line, secret["id"]), headers=mine.owner.headers),
        client.delete(
            receipt_url(theirs.org, their_line, secret["id"]), headers=mine.owner.headers
        ),
        upload_receipt(client, mine.owner, theirs.org, their_line),
        client.get(theirs.url("/expense-receipts"), headers=mine.owner.headers),
    ):
        assert (await call).status_code == 404
    assert len(stored_files()) == 1  # theirs, untouched
    got = await client.get(
        receipt_url(theirs.org, their_line, secret["id"]), headers=theirs.owner.headers
    )
    assert got.status_code == 200 and got.content == PDF
    # The Receipts page of each practice holds its own receipts only.
    listing = await client.get(mine.url("/expense-receipts"), headers=mine.owner.headers)
    assert ok_data(listing) == []


# --- The Receipts page ----------------------------------------------------------------------


async def receipts_scene(client):
    team = await expense_team(client)
    org = team.org
    alice = await add_member(org["id"], "staff", full_name="Alice Adams")
    bob = await add_member(org["id"], "staff", full_name="Bob Brown")
    acme = await make_client(org, "Acme Ltd")
    a_claim = await create_claim(client, alice, org, title="Alice trip")
    a1 = await add_line(
        client,
        alice,
        org,
        a_claim["id"],
        incurred_on="2026-09-01",
        description="Train ticket",
        supplier="LNER",
        net=1000,
        client_id=str(acme.id),
    )
    a2 = await add_line(
        client,
        alice,
        org,
        a_claim["id"],
        incurred_on="2026-09-10",
        description="Hotel stay",
        net=2500,
    )
    b_claim = await create_claim(client, bob, org, title="Bob parking")
    b1 = await add_line(
        client, bob, org, b_claim["id"], incurred_on="2026-08-20", description="Car park", net=500
    )
    uploads = {}
    for name, line, kind in (
        ("train.pdf", a1, "pdf"),
        ("ticket.png", a1, "png"),
        ("hotel.pdf", a2, "pdf"),
        ("parking.jpg", b1, "jpg"),
    ):
        actor = bob if line is b1 else alice
        uploads[name] = await up(client, org, actor, line["id"], kind, name=name)
    await act(client, bob, org, b_claim["id"], "submit")
    return (
        team,
        alice,
        bob,
        acme,
        {"a": a_claim, "b": b_claim, "a1": a1, "a2": a2, "b1": b1},
        uploads,
    )


async def page(client, team, actor=None, **params) -> list[dict]:
    response = await client.get(
        team.url("/expense-receipts"),
        headers=(actor or team.owner).headers,
        params={"limit": 100, **params},
    )
    return ok_data(response)


def names(items: list[dict]) -> list[str]:
    return [item["file_name"] for item in items]


async def test_the_receipts_page_shows_each_receipt_with_its_line_and_claim(client):
    team, alice, _bob, acme, refs, uploads = await receipts_scene(client)
    items = await page(client, team)
    assert len(items) == 4 and all(set(item) == ITEM_KEYS for item in items)
    # Newest upload first.
    assert names(items) == ["parking.jpg", "hotel.pdf", "ticket.png", "train.pdf"]
    by_name = {item["file_name"]: item for item in items}
    train = by_name["train.pdf"]
    assert train["id"] == uploads["train.pdf"]["id"] and train["expense_id"] == refs["a1"]["id"]
    assert train["claim_id"] == refs["a"]["id"] and train["claim_status"] == "draft"
    assert train["claim_title"] == "Alice trip" and train["user_id"] == str(alice.id)
    assert train["user_name"] == "Alice Adams" and train["description"] == "Train ticket"
    assert train["incurred_on"] == "2026-09-01" and train["gross"] == 1000
    assert train["content_type"] == "application/pdf" and train["is_image"] is False
    assert train["client"] == {"id": str(acme.id), "code": acme.code, "name": "Acme Ltd"}
    parking = by_name["parking.jpg"]
    assert parking["claim_status"] == "submitted" and parking["user_name"] == "Bob Brown"
    assert parking["is_image"] is True and parking["gross"] == 500 and parking["client"] is None


async def test_the_receipts_page_filters(client):
    team, alice, bob, acme, refs, _ = await receipts_scene(client)

    async def got(actor=None, **params) -> set[str]:
        return set(names(await page(client, team, actor, **params)))

    assert await got(claim_id=refs["a"]["id"]) == {"train.pdf", "ticket.png", "hotel.pdf"}
    assert await got(expense_id=refs["a1"]["id"]) == {"train.pdf", "ticket.png"}
    assert await got(expense_id=refs["b1"]["id"]) == {"parking.jpg"}
    assert await got(user_id=str(bob.id)) == {"parking.jpg"}
    assert await got(client_id=str(acme.id)) == {"train.pdf", "ticket.png"}
    assert await got(status="submitted") == {"parking.jpg"}
    assert await got(status="draft") == {"train.pdf", "ticket.png", "hotel.pdf"}
    assert await got(status=["draft", "submitted"]) == {
        "train.pdf",
        "ticket.png",
        "hotel.pdf",
        "parking.jpg",
    }
    assert await got(status="approved") == set()
    # Dates are those of the line, inclusive.
    assert await got(date_from="2026-09-05") == {"hotel.pdf"}
    assert await got(date_to="2026-08-31") == {"parking.jpg"}
    assert await got(date_from="2026-09-01", date_to="2026-09-01") == {"train.pdf", "ticket.png"}
    # Search: the file name, the line, the supplier, the claim, the claimant.
    assert await got(q="ticket.png") == {"ticket.png"}
    assert await got(q="TRAIN") == {"train.pdf", "ticket.png"}
    assert await got(q="lner") == {"train.pdf", "ticket.png"}
    assert await got(q="hotel") == {"hotel.pdf"}
    assert await got(q="alice") == {"train.pdf", "ticket.png", "hotel.pdf"}
    assert await got(q="bob parking") == {"parking.jpg"}
    assert await got(q="no such thing") == set()
    assert len(await got(q="  ")) == 4
    # Mine.
    assert await got(alice, mine="true") == {"train.pdf", "ticket.png", "hotel.pdf"}
    assert await got(bob, mine="true") == {"parking.jpg"}
    assert await got(mine="true") == set()
    assert await got(alice, mine="true", user_id=str(bob.id)) == set()


async def test_the_receipts_page_sorts_and_pages_in_one_order(client):
    team, *_ = await receipts_scene(client)
    url = team.url("/expense-receipts")
    keys = {"created_at": "created_at", "incurred_on": "incurred_on", "gross": "gross"}
    for sort, key in keys.items():
        for order in ("desc", "asc"):
            params = {"sort": sort, "order": order, "limit": 100}
            everything = ok_data(await client.get(url, headers=team.owner.headers, params=params))
            values = [item[key] for item in everything]
            assert values == sorted(values, reverse=order == "desc"), (sort, order)
            seen, cursor = [], None
            for _ in range(6):
                page_params = {**params, "limit": 1, **({"cursor": cursor} if cursor else {})}
                response = await client.get(url, headers=team.owner.headers, params=page_params)
                seen += [item["id"] for item in ok_data(response)]
                cursor = response.json()["meta"]["next_cursor"]
                if not cursor:
                    break
            assert len(seen) == 4 and len(set(seen)) == 4, (sort, order)
            assert seen == [item["id"] for item in everything], (sort, order)
    first = await client.get(url, headers=team.owner.headers, params={"sort": "gross", "limit": 2})
    assert first.json()["meta"]["has_more"] is True
    cursor = first.json()["meta"]["next_cursor"]
    for params in (
        {"cursor": cursor},
        {"cursor": cursor, "sort": "incurred_on"},
        {"cursor": cursor, "sort": "gross", "order": "asc"},
        {"cursor": "bogus", "sort": "gross"},
    ):
        response = await client.get(url, headers=team.owner.headers, params=params)
        assert err(response, 422)["details"][0]["field"] == "cursor", params


async def test_the_receipts_page_shows_only_what_the_caller_may_see(client):
    team, alice, bob, _, _refs, _ = await receipts_scene(client)
    org = team.org
    assert set(names(await page(client, team, alice))) == {"train.pdf", "ticket.png", "hotel.pdf"}
    assert names(await page(client, team, bob)) == ["parking.jpg"]
    assert await page(client, team, alice, user_id=str(bob.id)) == []
    assert len(await page(client, team, await team.member("manager"))) == 4
    assert len(await page(client, team, await team.member("billing"))) == 4
    assert await page(client, team, await add_member(org["id"], "staff")) == []
    refused = await client.get(
        team.url("/expense-receipts"), headers=(await team.member("viewer")).headers
    )
    assert err(refused, 403)["code"] == "PERMISSION_DENIED"
    # An approver sees the receipts of the people they manage.
    await add_custom_role(
        org["id"],
        "Approver",
        ["organization.read", "member.read", "expense.log", "expense.approve"],
    )
    boss = await add_member(org["id"], "Approver")
    assert await page(client, team, boss) == []
    await set_manager(org, alice, boss)
    assert set(names(await page(client, team, boss))) == {"train.pdf", "ticket.png", "hotel.pdf"}
    assert await count_rows("SELECT count(*) FROM expense_receipts") == 4


@pytest.mark.parametrize(
    "bad",
    [
        {"status": "paid"},
        {"date_from": "2026-09-10", "date_to": "2026-09-01"},
        {"date_from": "yesterday"},
        {"sort": "size"},
        {"order": "up"},
        {"limit": 0},
        {"limit": 101},
        {"q": "x" * 201},
        {"claim_id": "x"},
        {"expense_id": "x"},
        {"user_id": "x"},
        {"client_id": "x"},
        {"mine": "perhaps"},
    ],
)
async def test_the_receipts_page_refuses_bad_parameters(client, bad):
    team = await expense_team(client)
    response = await client.get(
        team.url("/expense-receipts"), headers=team.owner.headers, params=bad
    )
    assert response.status_code == 422
