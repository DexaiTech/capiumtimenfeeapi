"""The pieces of an upload on their own: the multipart reader, file name cleaning, content
sniffing and the checks that combine them."""

import pytest

from app.core.exceptions import FileUploadError
from app.services.attachment_files import (
    AttachmentEmptyError,
    AttachmentTooLargeError,
    AttachmentTypeError,
    inspect_upload,
    sanitise_file_name,
    sniff,
)
from app.utils.multipart import MAX_HEADER_BYTES, MAX_PARTS, parse_form_data
from tests.ticket_helpers import SAMPLES, multipart_body, ole2, ooxml, zip_bytes

FORM = "multipart/form-data; boundary=B"


def form(*parts: bytes, closed: bool = True) -> bytes:
    """A body from ready-made parts (each ``headers CRLF CRLF data``)."""
    body = b"".join(b"--B\r\n" + part + b"\r\n" for part in parts)
    return body + (b"--B--\r\n" if closed else b"")


def disposition(**params: str) -> bytes:
    fields = "; ".join(f'{key}="{value}"' for key, value in params.items())
    return f"Content-Disposition: form-data; {fields}\r\n".encode()


# --- The multipart reader -------------------------------------------------------------------


def test_a_file_and_a_field_are_read_in_order():
    body, content_type = multipart_body(
        [("file", "shot.png", "image/png", b"\x89PNG data"), ("comment_id", None, None, b"abc")]
    )
    file, field = parse_form_data(content_type, body)
    assert (file.name, file.filename, file.content_type, file.data) == (
        "file",
        "shot.png",
        "image/png",
        b"\x89PNG data",
    )
    assert (field.name, field.filename, field.content_type, field.data) == (
        "comment_id",
        None,
        None,
        b"abc",
    )


def test_content_is_returned_byte_for_byte():
    awkward = b"line\r\nwith\r\n--not-the-boundary\r\n\r\n\x00\xff\r\n"
    body, content_type = multipart_body([("file", "a.bin", None, awkward)])
    [part] = parse_form_data(content_type, body)
    assert part.data == awkward
    [empty] = parse_form_data(*reversed(multipart_body([("file", "e.png", None, b"")])))
    assert empty.data == b""


def test_headers_are_case_insensitive_and_the_content_type_loses_its_parameters():
    body = form(
        b'content-disposition: form-data; NAME="file"; FileName="a.txt"\r\n'
        b"CONTENT-TYPE: Text/Plain; charset=utf-8\r\n\r\ntext"
    )
    [part] = parse_form_data(FORM, body)
    assert (part.name, part.filename, part.content_type, part.data) == (
        "file",
        "a.txt",
        "text/plain",
        b"text",
    )


def test_quotes_and_backslashes_in_names():
    body = form(
        disposition(name="file", filename="say %22hi%22.png") + b"\r\ndata",
        disposition(name="file", filename=r"say \"hi\".png") + b"\r\ndata",
        disposition(name="file", filename=r"C:\Users\me\x.png") + b"\r\ndata",
        disposition(name="file", filename=r"double\\slash.png") + b"\r\ndata",
    )
    names = [part.filename for part in parse_form_data(FORM, body)]
    assert names == ['say "hi".png', 'say "hi".png', r"C:\Users\me\x.png", "double\\slash.png"]


def test_a_semicolon_inside_a_quoted_name_does_not_start_a_new_parameter():
    body = form(disposition(name="file", filename="a; name=evil.png") + b"\r\ndata")
    [part] = parse_form_data(FORM, body)
    assert (part.name, part.filename) == ("file", "a; name=evil.png")


def test_an_rfc_5987_name_wins_and_a_bad_charset_falls_back():
    good = form(
        b'Content-Disposition: form-data; name="file"; filename="plain.png"; '
        b"filename*=UTF-8''caf%C3%A9.png\r\n\r\ndata"
    )
    assert parse_form_data(FORM, good)[0].filename == "caf\u00e9.png"
    unknown = form(
        b'Content-Disposition: form-data; name="file"; filename="plain.png"; '
        b"filename*=NO-SUCH-CHARSET''caf%C3%A9.png\r\n\r\ndata"
    )
    assert parse_form_data(FORM, unknown)[0].filename == "plain.png"


def test_a_part_without_headers_and_bytes_that_are_not_utf_8():
    [bare] = parse_form_data(FORM, b"--B\r\n\r\ndata\r\n--B--\r\n")
    assert (bare.name, bare.filename, bare.content_type, bare.data) == ("", None, None, b"data")
    body = form(b'Content-Disposition: form-data; name="file"; filename="caf\xe9.png"\r\n\r\nd')
    assert parse_form_data(FORM, body)[0].filename == "caf\ufffd.png"


def test_transport_padding_a_preamble_an_epilogue_and_a_quoted_boundary_are_fine():
    body = (
        b"ignored preamble\r\n--B  \t\r\n"
        + disposition(name="a")
        + b"\r\nvalue\r\n--B--\r\nepilogue"
    )
    [part] = parse_form_data('multipart/form-data; boundary="B"', body)
    assert part.data == b"value"


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("application/json", b"{}"),
        ("text/plain", b"x"),
        ("", b""),
        ("multipart/form-data", form(disposition(name="a") + b"\r\nx")),
        ("multipart/form-data; boundary=", form(disposition(name="a") + b"\r\nx")),
        ("multipart/mixed; boundary=B", form(disposition(name="a") + b"\r\nx")),
        (FORM, b""),
        (FORM, b"just some text"),
        (FORM, form(disposition(name="a") + b"\r\nx", closed=False)),
        (FORM, b"--B\r\n" + disposition(name="a")),  # no blank line, no data, no closing
        (FORM, b"--B\r\nnot-a-header\r\nmore\r\n--B--\r\n"),
        (FORM, b"--B\n" + disposition(name="a") + b"\r\nx\r\n--B--\r\n"),  # LF instead of CRLF
        (FORM, b"--Bextra\r\n" + disposition(name="a") + b"\r\nx\r\n--B--"),
        (
            FORM,
            b"--B\r\n" + disposition(name="a") + b"\r\nx\r\n--Bx\r\n--B--\r\n",
        ),  # a stray delimiter
        (FORM, form(*[disposition(name="a") + b"\r\nx"] * (MAX_PARTS + 1))),
        (FORM, form(b"X-Big: " + b"a" * MAX_HEADER_BYTES + b"\r\n\r\nx")),
    ],
)
def test_anything_that_is_not_a_well_formed_form_is_refused(content_type, body):
    with pytest.raises(FileUploadError):
        parse_form_data(content_type, body)


def test_exactly_the_maximum_number_of_parts_is_allowed():
    body = form(*[disposition(name=f"f{i}") + b"\r\nx" for i in range(MAX_PARTS)])
    assert len(parse_form_data(FORM, body)) == MAX_PARTS


# --- File names -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("shot.png", "shot.png"),
        ("../../etc/passwd", "passwd"),
        ("..\\..\\windows\\system32\\evil.png", "evil.png"),
        ("/abs/path/file.pdf", "file.pdf"),
        ("C:\\Users\\me\\file.pdf", "file.pdf"),
        ("dir/", "file"),
        ("", "file"),
        ("   ", "file"),
        ("...", "file"),
        (". . .", "file"),
        (".hidden", "hidden"),
        ("a\x00b.png", "ab.png"),
        ("a\r\nb.png", "ab.png"),
        ("a\tb.png", "ab.png"),
        ("a\x7fb.png", "ab.png"),
        ("evil\u202egnp.png", "evilgnp.png"),
        ("zero\u200bwidth.png", "zerowidth.png"),
        ("line\u2028sep.png", "linesep.png"),
        ('a<b>c:d"e|f?g*h.png', "a_b_c_d_e_f_g_h.png"),
        ("many    spaces   here.png", "many spaces here.png"),
        ("cafe\u0301.png", "caf\u00e9.png"),
        ("\U0001f4ce clip.png", "\U0001f4ce clip.png"),
        ("name.png   ", "name.png"),
        ("name.png.", "name.png"),
    ],
)
def test_sanitise_file_name(raw, clean):
    assert sanitise_file_name(raw) == clean


def test_long_names_are_cut_but_keep_their_extension():
    name = sanitise_file_name("x" * 400 + ".pdf")
    assert len(name) == 150 and name.endswith(".pdf") and set(name[:-4]) == {"x"}
    assert len(sanitise_file_name("y" * 400)) == 150
    # A "suffix" that is itself huge is not kept.
    odd = sanitise_file_name("z" * 10 + "." + "q" * 300)
    assert len(odd) == 150


def test_a_cleaned_name_never_holds_a_separator_a_control_or_a_reserved_character():
    nasty = "".join(chr(i) for i in range(0, 0x80)) + "\u202e\u200b\u2028\ufeff.png"
    clean = sanitise_file_name(nasty)
    assert not (set(clean) & set('/\\<>:"|?*'))
    assert all(c.isprintable() for c in clean)
    assert clean and not clean.startswith((" ", ".")) and not clean.endswith((" ", "."))


# --- Content sniffing -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "sniffed"),
    [
        ("png", {"png"}),
        ("jpg", {"jpeg"}),
        ("gif", {"gif"}),
        ("webp", {"webp"}),
        ("pdf", {"pdf"}),
        ("docx", {"docx"}),
        ("xlsx", {"xlsx"}),
        ("doc", {"doc"}),
        ("xls", {"xls"}),
        ("csv", {"csv"}),
    ],
)
def test_each_sample_is_recognised_as_what_it_is(kind, sniffed):
    assert sniff(SAMPLES[kind].data) == sniffed


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x00",
        b"MZ\x90\x00" + b"\x00" * 100,
        b"\x7fELF" + b"\x00" * 100,
        b"<html></html>",
        b"  <svg/>",
        b"<?xml version='1.0'?><a/>",
        b"\xef\xbb\xbf<html>",
        b"PK\x03\x04garbage",
        zip_bytes({"a.txt": "x"}),
        ooxml("ppt/presentation.xml"),
        ole2("Unknown"),
        b"a,b\n\x00\n",
        bytes(range(256)),
        "a,b\n1,2\n".encode("utf-16"),
        b"#!/bin/sh\x00\n",
    ],
)
def test_content_that_is_none_of_the_allowed_kinds_is_recognised_as_nothing(data):
    assert sniff(data) == frozenset()


def test_an_office_package_is_told_apart_by_its_folders_and_may_be_both():
    assert sniff(ooxml("word/document.xml")) == {"docx"}
    assert sniff(ooxml("xl/workbook.xml")) == {"xlsx"}
    assert sniff(ooxml("word/document.xml", "xl/workbook.xml")) == {"docx", "xlsx"}


def test_text_may_be_utf_8_with_or_without_a_bom_or_windows_1252():
    assert sniff(b"a,b\n1,2\n") == {"csv"}
    assert sniff(b"\xef\xbb\xbfa,b\r\n1,2\r\n") == {"csv"}
    assert sniff("name,price\nCaf\u00e9,\u00a31\n".encode("cp1252")) == {"csv"}
    assert sniff(b"one column\n") == {"csv"}
    assert sniff(b"tabs\tand\r\nnewlines\r\n") == {"csv"}


# --- The combined check ---------------------------------------------------------------------


def test_inspect_upload_describes_a_good_file():
    sample = SAMPLES["pdf"]
    inspected = inspect_upload("  Report / Q3.pdf ", "application/pdf; x=y", sample.data)
    assert inspected.file_name == "Q3.pdf" and inspected.content_type == "application/pdf"
    assert inspected.size_bytes == len(sample.data) and len(inspected.sha256) == 64


def test_inspect_upload_raises_the_right_error_for_each_refusal():
    png = SAMPLES["png"].data
    with pytest.raises(AttachmentEmptyError):
        inspect_upload("a.png", None, b"")
    with pytest.raises(AttachmentTooLargeError):
        inspect_upload("a.png", None, png + b"\x00" * (2 * 1024 * 1024))
    for name, data, declared in (
        ("a.exe", png, None),
        ("a.png", b"<html>", None),
        ("a.png", png, "text/html"),
        ("a.jpg", png, None),
    ):
        with pytest.raises(AttachmentTypeError):
            inspect_upload(name, declared, data)
