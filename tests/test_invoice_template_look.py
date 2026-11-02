"""The look of an invoice template: its accent colour and its watermark, in the PDF."""

from io import BytesIO

import pytest
from PIL import Image

from app.services.invoice_pdf import PdfTemplate, rgb
from tests.billing_helpers import draft, issued, new_practice, org_url
from tests.conftest import STORAGE_DIR
from tests.domain_factories import make_disbursement
from tests.factories import error
from tests.test_avatar import PNG


def test_a_hex_colour_becomes_its_rgb_and_anything_else_is_none():
    assert rgb("#1F5CE6") == (31, 92, 230)
    assert rgb("#000000") == (0, 0, 0)
    for bad in (None, "", "1F5CE6", "#12345", "#GGGGGG", "red"):
        assert rgb(bad) is None
    assert PdfTemplate().accent_color is None and PdfTemplate().watermark_text is None


async def make_template(client, p, **body) -> dict:
    response = await client.post(
        org_url(p.org, "/invoice-templates"),
        headers=p.owner.headers,
        json={"name": body.pop("name", "Branded"), **body},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def issued_invoice(client, p) -> dict:
    fee = await make_disbursement(p.org, p.client)
    invoice = await draft(client, p.owner, p.org, p.client.id, disbursements=[fee])
    return await issued(client, p.owner, p.org, invoice["id"])


async def pdf_of(client, p, invoice_id: str, template_id: str | None = None) -> bytes:
    params = {"template_id": template_id} if template_id else {}
    response = await client.get(
        org_url(p.org, f"/invoices/{invoice_id}/pdf"), headers=p.owner.headers, params=params
    )
    assert response.status_code == 200, response.text
    return response.content


async def test_a_template_keeps_its_colour_and_watermark(client):
    p = await new_practice(client)
    created = await make_template(client, p, accent_color="#1F5CE6", watermark_text="Copy invoice")
    assert (created["accent_color"], created["watermark_text"]) == ("#1F5CE6", "Copy invoice")
    changed = await client.patch(
        org_url(p.org, f"/invoice-templates/{created['id']}"),
        headers=p.owner.headers,
        json={"accent_color": "#10B981", "watermark_text": "  "},
    )
    data = changed.json()["data"]
    assert (data["accent_color"], data["watermark_text"]) == ("#10B981", None)


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("accent_color", "blue"),
        ("accent_color", "#12345"),
        ("accent_color", "#GGGGGG"),
        ("watermark_text", "x" * 25),
        ("watermark_text", "no <tags>"),
        ("watermark_text", "café"),
    ],
)
async def test_a_bad_colour_or_watermark_is_refused(client, field, bad):
    p = await new_practice(client)
    response = await client.post(
        org_url(p.org, "/invoice-templates"),
        headers=p.owner.headers,
        json={"name": "Bad", field: bad},
    )
    assert response.status_code == 422
    assert error(response)["code"] == "VALIDATION_ERROR"


async def test_the_pdf_carries_the_watermark_and_the_accent_colour(client):
    p = await new_practice(client)
    invoice = await issued_invoice(client, p)
    plain = await pdf_of(client, p, invoice["id"])
    assert b"SPECIMEN" not in plain

    template = await make_template(
        client, p, name="Branded", accent_color="#1F5CE6", watermark_text="SPECIMEN"
    )
    branded = await pdf_of(client, p, invoice["id"], template["id"])
    assert branded.startswith(b"%PDF") and b"SPECIMEN" in branded
    # The accent (31, 92, 230) as a PDF fill colour: the title, the name and the table heads.
    assert b"0.1216 0.3608 0.902 rg" in branded
    assert b"0.1216 0.3608 0.902 rg" not in plain


async def test_a_draft_still_says_draft_whatever_the_template_says(client):
    p = await new_practice(client)
    fee = await make_disbursement(p.org, p.client)
    invoice = await draft(client, p.owner, p.org, p.client.id, disbursements=[fee])
    template = await make_template(client, p, watermark_text="SPECIMEN")
    pdf = await pdf_of(client, p, invoice["id"], template["id"])
    assert b"DRAFT" in pdf and b"SPECIMEN" not in pdf


# --- A picture as the watermark ----------------------------------------------------------------


def stored_files() -> list:
    return sorted(p for p in STORAGE_DIR.rglob("*") if p.is_file() and ".tmp" not in p.parts)


def jpeg() -> bytes:
    out = BytesIO()
    Image.new("RGB", (4, 2), (200, 0, 0)).save(out, "JPEG")
    return out.getvalue()


async def set_picture(
    client, p, template_id: str, data: bytes = PNG, name: str = "mark.png", kind: str = "image/png"
):
    return await client.post(
        org_url(p.org, f"/invoice-templates/{template_id}/watermark-image"),
        headers=p.owner.headers,
        files={"file": (name, data, kind)},
    )


async def test_a_template_takes_a_watermark_picture_and_replaces_it(client):
    p = await new_practice(client)
    template = await make_template(client, p)
    assert template["watermark_image"] is False

    response = await set_picture(client, p, template["id"])
    assert response.status_code == 200, response.text
    assert response.json()["data"]["watermark_image"] is True
    assert len(stored_files()) == 1
    url = org_url(p.org, f"/invoice-templates/{template['id']}/watermark-image")
    shown = await client.get(url, headers=p.owner.headers)
    assert shown.status_code == 200 and shown.content == PNG
    assert shown.headers["content-type"] == "image/png"
    assert shown.headers["x-content-type-options"] == "nosniff"

    first = stored_files()
    replaced = await set_picture(client, p, template["id"], jpeg(), "mark.jpg", "image/jpeg")
    assert replaced.status_code == 200, replaced.text
    after = stored_files()
    assert len(after) == 1 and after != first
    assert (await client.get(url, headers=p.owner.headers)).headers["content-type"] == "image/jpeg"


async def test_only_a_picture_is_a_watermark_picture(client):
    p = await new_practice(client)
    template = await make_template(client, p)
    response = await set_picture(
        client, p, template["id"], b"%PDF-1.4 hi", "a.pdf", "application/pdf"
    )
    assert response.status_code == 422
    assert error(response)["code"] == "FILE_TYPE_NOT_ALLOWED"
    assert stored_files() == []
    # A PNG header over content that does not decode would break every PDF of the template.
    broken = await set_picture(client, p, template["id"], PNG[:16] + b"not a picture" * 8)
    assert broken.status_code == 422
    assert error(broken)["code"] == "VALIDATION_ERROR"
    assert stored_files() == []


async def test_a_watermark_is_a_text_or_a_picture_not_both(client):
    p = await new_practice(client)
    with_text = await make_template(client, p, name="Text", watermark_text="COPY")
    refused = await set_picture(client, p, with_text["id"])
    assert refused.status_code == 422
    assert error(refused)["code"] == "VALIDATION_ERROR"
    assert stored_files() == []

    with_picture = await make_template(client, p, name="Picture")
    await set_picture(client, p, with_picture["id"])
    patched = await client.patch(
        org_url(p.org, f"/invoice-templates/{with_picture['id']}"),
        headers=p.owner.headers,
        json={"watermark_text": "COPY"},
    )
    assert patched.status_code == 422
    assert error(patched)["code"] == "VALIDATION_ERROR"
    # Clearing the text first lets the picture in.
    await client.patch(
        org_url(p.org, f"/invoice-templates/{with_text['id']}"),
        headers=p.owner.headers,
        json={"watermark_text": None},
    )
    assert (await set_picture(client, p, with_text["id"])).status_code == 200


async def test_removing_the_watermark_picture_removes_its_file(client):
    p = await new_practice(client)
    template = await make_template(client, p)
    await set_picture(client, p, template["id"])
    url = org_url(p.org, f"/invoice-templates/{template['id']}/watermark-image")
    removed = await client.delete(url, headers=p.owner.headers)
    assert removed.status_code == 200 and removed.json()["data"]["watermark_image"] is False
    assert stored_files() == []
    assert (await client.get(url, headers=p.owner.headers)).status_code == 404
    # Removing again is not an error, and a text can now be set.
    assert (await client.delete(url, headers=p.owner.headers)).status_code == 200
    patched = await client.patch(
        org_url(p.org, f"/invoice-templates/{template['id']}"),
        headers=p.owner.headers,
        json={"watermark_text": "COPY"},
    )
    assert patched.status_code == 200 and patched.json()["data"]["watermark_text"] == "COPY"


async def test_the_pdf_carries_the_watermark_picture_but_a_draft_says_draft(client):
    p = await new_practice(client)
    invoice = await issued_invoice(client, p)
    plain = await pdf_of(client, p, invoice["id"])
    assert b"/Subtype /Image" not in plain

    template = await make_template(client, p, name="Pictured")
    await set_picture(client, p, template["id"])
    pictured = await pdf_of(client, p, invoice["id"], template["id"])
    # The picture, painted pale (fill opacity 0.12).
    assert pictured.startswith(b"%PDF") and b"/Subtype /Image" in pictured
    assert b"/ca 0.12" in pictured

    fee = await make_disbursement(p.org, p.client)
    unissued = await draft(client, p.owner, p.org, p.client.id, disbursements=[fee])
    pdf = await pdf_of(client, p, unissued["id"], template["id"])
    assert b"DRAFT" in pdf and b"/Subtype /Image" not in pdf
