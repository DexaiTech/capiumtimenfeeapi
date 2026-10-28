"""The look of an invoice template: its accent colour and its watermark, in the PDF."""

import pytest

from app.services.invoice_pdf import PdfTemplate, rgb
from tests.billing_helpers import draft, issued, new_practice, org_url
from tests.domain_factories import make_disbursement
from tests.factories import error


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
