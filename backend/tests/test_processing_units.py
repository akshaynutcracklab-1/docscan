import io
from unittest import mock

import pikepdf
import pytest
from PIL import Image

from processing import analysis, azure_client, image_utils, pdf_utils
from tests.azure_fakes import http_error
from tests.conftest import image_bytes


# ------------------------------------------------------------------ analysis

def test_analysis_regexes():
    text = (
        "Bill date: 05/03/2024, due 2024-04-01 or 12 March 2024. Not a date: 99/99/9999.\n"
        "Amount ₹ 12,34,567.89 and Rs. 500 plus $1,000.00 and INR 250\n"
        "Mail: Sales@Shop.in  Phones: 9876543210, +91-91234 56789, 044 2345 6789 (landline)\n"
        "GSTIN 29ABCDE1234F1Z5 PAN ABCDE1234F"
    )
    result = analysis.analyze_text(text)
    assert [d["date"] for d in result["dates"]] == ["2024-03-05", "2024-04-01", "2024-03-12"]
    assert [(a["currency"], a["value"]) for a in result["amounts"]] == [
        ("INR", "1234567.89"), ("INR", "500"), ("USD", "1000.00"), ("INR", "250"),
    ]
    assert result["emails"] == ["sales@shop.in"]
    assert result["phones"] == ["+919876543210", "+919123456789"]
    assert result["gstins"] == ["29ABCDE1234F1Z5"]
    assert result["pans"] == ["ABCDE1234F"]  # the PAN inside the GSTIN is not double counted
    assert result["word_count"] == analysis.word_count(text) > 40


def test_word_count_and_empty_text():
    assert analysis.word_count("one two, three") == 3
    assert analysis.analyze_text("")["word_count"] == 0


# ------------------------------------------------------------------ images

def _open(data):
    return Image.open(io.BytesIO(data))


def test_prepare_rotates_clockwise_and_converts_png():
    img = Image.new("RGB", (200, 100), "white")
    img.paste((255, 0, 0), (0, 0, 20, 20))  # red block top-left
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    prepared = image_utils.prepare_image(buf.getvalue(), rotation=90)
    out = _open(prepared.data)
    assert out.format == "JPEG" and out.size == (100, 200) and prepared.changed
    r, g, b = out.getpixel((95, 5))  # top-left moves to top-right on a clockwise turn
    assert r > 200 and g < 80


def test_prepare_applies_exif_orientation():
    img = Image.new("RGB", (300, 100), "white")
    exif = img.getexif()
    exif[0x0112] = 6  # rotate 90° clockwise to display
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    prepared = image_utils.prepare_image(buf.getvalue())
    assert (prepared.width, prepared.height) == (100, 300)
    assert _open(prepared.data).getexif().get(0x0112) in (None, 1)


def test_prepare_resizes_long_side_and_keeps_compliant_jpeg_untouched():
    big = image_utils.prepare_image(image_bytes(size=(4000, 2000)))
    assert max(big.width, big.height) == 3000

    ok = image_bytes(size=(800, 600))
    same = image_utils.prepare_image(ok)
    assert not same.changed and same.data == ok


def test_prepare_heic_to_jpeg_and_compress_below_limit():
    heic = image_bytes("HEIF", size=(1000, 800))
    prepared = image_utils.prepare_image(heic)
    assert _open(prepared.data).format == "JPEG"

    noisy = Image.effect_noise((2500, 2500), 120).convert("RGB")
    data = image_utils.compress_jpeg(noisy, max_bytes=300_000)
    assert len(data) <= 300_000


def test_inspect_image_rejects_non_images():
    with pytest.raises(image_utils.InvalidImage):
        image_utils.inspect_image(io.BytesIO(b"%PDF-1.4 not an image"))
    info = image_utils.inspect_image(io.BytesIO(image_bytes("WEBP")))
    assert info.format == "WEBP" and info.extension == "webp"


# ------------------------------------------------------------------ pdf

def test_merge_pdfs_keeps_order():
    a = pdf_utils.images_to_pdf([image_bytes(size=(100, 200))])
    b = pdf_utils.images_to_pdf([image_bytes(size=(300, 100)), image_bytes(size=(50, 50))])
    merged = pdf_utils.merge_pdfs([b, a])
    with pikepdf.Pdf.open(io.BytesIO(merged)) as pdf:
        widths = [float(p.mediabox[2]) for p in pdf.pages]
    assert len(widths) == 3
    assert widths[0] > widths[2]  # the 300px page comes first
    with pytest.raises(ValueError):
        pdf_utils.merge_pdfs([])


# ------------------------------------------------------------------ azure client

def test_parse_rate():
    assert azure_client.parse_rate("20/m") == (20, 60)
    assert azure_client.parse_rate("5/second") == (5, 1)
    assert azure_client.parse_rate("100/h") == (100, 3600)


def test_rate_limiter_waits_when_window_is_full(settings):
    sleeps = []
    clock = [1000.0]

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    with mock.patch("processing.azure_client.time.time", side_effect=lambda: clock[0]), \
         mock.patch("processing.azure_client.time.sleep", side_effect=fake_sleep):
        for _ in range(3):
            azure_client.acquire_rate_slot("2/m")
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 60


def test_error_classification():
    assert azure_client.is_transient(http_error(429))
    assert azure_client.is_transient(http_error(500))
    assert not azure_client.is_transient(http_error(400))
    with pytest.raises(azure_client.TransientAzureError):
        with azure_client.translate_errors("x"):
            raise http_error(503)
    with pytest.raises(azure_client.AzureProcessingError) as exc:
        with azure_client.translate_errors("x"):
            raise http_error(401)
    assert not isinstance(exc.value, azure_client.TransientAzureError)


def test_client_is_created_once(settings):
    azure_client.reset_client()
    try:
        assert azure_client.get_client() is azure_client.get_client()
    finally:
        azure_client.reset_client()
