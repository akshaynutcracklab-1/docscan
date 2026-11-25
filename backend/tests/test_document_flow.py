"""End-to-end flow with the mock OCR engine and Celery in eager mode."""
import json
import os

import pikepdf
import pytest
from django.conf import settings

from documents.models import Document, Page
from processing.ocr import MOCK_TEXT
from tests.conftest import upload

pytestmark = pytest.mark.django_db


def _stored(field_file) -> bool:
    return bool(field_file.name) and field_file.storage.exists(field_file.name)


def test_full_invoice_flow(api, make_document, user):
    # Step 1: create with a type -> draft with default title.
    created = api.post("/api/documents/", {"document_type": "invoice"}, format="json")
    assert created.status_code == 201
    doc_id = created.json()["id"]
    assert created.json()["status"] == "draft"
    document = Document.objects.get(pk=doc_id)
    assert document.title.startswith("Invoice – ")

    # Step 2: upload 3 pages (one PNG, one with rotation).
    ids = []
    for i, kwargs in enumerate([{}, {"name": "p.png", "fmt": "PNG", "content_type": "image/png"}, {}]):
        data = {"image": upload(**kwargs)}
        if i == 2:
            data["rotation"] = 90
        resp = api.post(f"/api/documents/{doc_id}/pages/", data, format="multipart")
        assert resp.status_code == 201, resp.content
        assert resp.json()["order"] == i + 1
        ids.append(resp.json()["id"])
    document.refresh_from_db()
    assert document.status == "uploading"
    assert document.page_count == 3
    assert Page.objects.get(pk=ids[0]).image.name == f"users/{user.id}/documents/{doc_id}/pages/1.jpg"

    # Reorder: last page first.
    new_order = [ids[2], ids[0], ids[1]]
    resp = api.patch(f"/api/documents/{doc_id}/pages/reorder/", {"page_ids": new_order}, format="json")
    assert resp.status_code == 200
    assert [p["id"] for p in resp.json()] == new_order
    assert [p["order"] for p in resp.json()] == [1, 2, 3]

    # Step 3: submit -> runs the whole pipeline eagerly.
    resp = api.post(f"/api/documents/{doc_id}/submit/")
    assert resp.status_code == 202
    assert resp.json()["document_id"] == doc_id

    status = api.get(f"/api/documents/{doc_id}/status/").json()
    assert status["status"] == "ready", status
    assert status["progress"] == 100

    detail = api.get(f"/api/documents/{doc_id}/").json()
    assert detail["page_count"] == 3
    assert detail["pdf_url"]
    assert MOCK_TEXT.splitlines()[0] in detail["full_text"]
    assert "--- Page 1 ---" in detail["full_text"] and "--- Page 3 ---" in detail["full_text"]
    assert detail["language"] == "en"
    assert detail["average_confidence"] == pytest.approx(0.95)
    assert detail["extracted_fields"]["InvoiceId"]["value"] == "INV-1001"
    assert detail["extracted_fields"]["InvoiceTotal"]["value"]["amount"] == 1250.50
    assert detail["extracted_fields"]["Items"]["value"][0].keys() == {"Description", "Quantity", "UnitPrice", "Amount"}
    assert [p["order"] for p in detail["pages"]] == [1, 2, 3]
    assert all(p["status"] == "done" and p["image_url"] for p in detail["pages"])

    analysis = detail["analysis"]
    assert analysis["word_count"] > 10
    assert "billing@example.com" in analysis["emails"]
    assert "+919876543210" in analysis["phones"]
    assert "27AAPFU0939F1ZV" in analysis["gstins"]
    assert "AAPFU0939F" in analysis["pans"]
    assert {"raw": "15/01/2025", "date": "2025-01-15"} in analysis["dates"]
    assert any(a["currency"] == "INR" and a["value"] == "1250.50" for a in analysis["amounts"])
    assert analysis["low_confidence_pages"] == []

    document.refresh_from_db()
    assert _stored(document.pdf_file)
    assert document.pdf_file.name == f"users/{user.id}/documents/{doc_id}/document.pdf"
    with document.pdf_file.open("rb") as fh, pikepdf.Pdf.open(fh) as pdf:
        assert len(pdf.pages) == 3

    # The rotated page (now first) had its rotation baked into a portrait->landscape JPEG.
    rotated = Page.objects.get(pk=ids[2])
    assert rotated.rotation == 0
    assert (rotated.width, rotated.height) == (600, 400)
    png_page = Page.objects.get(pk=ids[1])
    assert png_page.image.name.endswith(".jpg")


def test_general_type_has_no_fields(api, make_document):
    doc_id = make_document("general", pages=1)
    api.post(f"/api/documents/{doc_id}/submit/")
    detail = api.get(f"/api/documents/{doc_id}/").json()
    assert detail["status"] == "ready"
    assert detail["extracted_fields"] == {}


def test_form_table_saves_tables_and_markdown(api, make_document):
    doc_id = make_document("form_table", pages=2)
    api.post(f"/api/documents/{doc_id}/submit/")
    fields = api.get(f"/api/documents/{doc_id}/").json()["extracted_fields"]
    assert len(fields["tables"]) == 2  # one table per page, merged
    assert fields["tables"][0]["cells"][0] == {"row": 0, "col": 0, "content": "Item", "kind": "columnHeader"}
    assert "| Item | Qty |" in fields["markdown"]


def test_downloads(api, make_document):
    doc_id = make_document("invoice", pages=2, title="March Invoice")
    api.post(f"/api/documents/{doc_id}/submit/")

    pdf = api.get(f"/api/documents/{doc_id}/download/?format=pdf")
    assert pdf.status_code == 200
    assert pdf["Content-Type"] == "application/pdf"
    assert b"".join(pdf.streaming_content).startswith(b"%PDF")
    assert 'filename="march-invoice.pdf"' in pdf["Content-Disposition"]

    link = api.get(f"/api/documents/{doc_id}/download/?format=pdf&link=true")
    assert link.status_code == 200 and link.json()["url"].startswith("http")

    txt = api.get(f"/api/documents/{doc_id}/download/?format=txt")
    assert txt.status_code == 200 and "MOCK OCR TEXT" in txt.content.decode()

    js = api.get(f"/api/documents/{doc_id}/download/?format=json")
    payload = json.loads(js.content)
    assert payload["extracted_fields"]["VendorName"]["value"] == "Mock Supplies Pvt Ltd"
    assert "analysis" in payload

    assert api.get(f"/api/documents/{doc_id}/download/?format=docx").status_code == 400


def test_pdf_download_before_processing_is_404(api, make_document):
    doc_id = make_document("general", pages=1)
    assert api.get(f"/api/documents/{doc_id}/download/?format=pdf").status_code == 404


def test_list_filter_search_and_pagination(api, make_document):
    inv = make_document("invoice", pages=1, title="Electricity bill")
    make_document("receipt", pages=1, title="Lunch")
    make_document("general", title="Notes")
    api.post(f"/api/documents/{inv}/submit/")

    resp = api.get("/api/documents/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 3 and len(body["results"]) == 3
    assert {"id", "title", "status", "thumbnail_url", "document_type"} <= body["results"][0].keys()

    assert [d["id"] for d in api.get("/api/documents/?type=invoice&status=ready").json()["results"]] == [inv]
    assert api.get("/api/documents/?type=receipt&status=ready").json()["count"] == 0
    assert [d["title"] for d in api.get("/api/documents/?q=electricity").json()["results"]] == ["Electricity bill"]
    assert api.get("/api/documents/?q=gstin").json()["count"] == 1  # matches OCR text
    assert api.get("/api/documents/?status=bogus").status_code == 400


def test_patch_rename_type_and_text(api, make_document):
    doc_id = make_document("invoice", pages=1)
    resp = api.patch(f"/api/documents/{doc_id}/", {"title": "Renamed", "document_type": "receipt"}, format="json")
    assert resp.status_code == 200
    assert resp.json()["title"] == "Renamed"
    assert resp.json()["document_type"]["code"] == "receipt"

    api.post(f"/api/documents/{doc_id}/submit/")
    locked = api.patch(f"/api/documents/{doc_id}/", {"document_type": "invoice"}, format="json")
    assert locked.status_code == 409

    edited = api.patch(f"/api/documents/{doc_id}/", {"full_text": "corrected text"}, format="json")
    assert edited.status_code == 200 and edited.json()["full_text"] == "corrected text"
    assert api.patch(f"/api/documents/{doc_id}/", {"title": "  "}, format="json").status_code == 400


def test_delete_document_removes_files(api, make_document):
    doc_id = make_document("invoice", pages=2)
    api.post(f"/api/documents/{doc_id}/submit/")
    document = Document.objects.get(pk=doc_id)
    paths = [document.pdf_file.path] + [p for page in document.pages.all() for p in (page.image.path, page.page_pdf.path)]
    assert all(os.path.exists(p) for p in paths)

    assert api.delete(f"/api/documents/{doc_id}/").status_code == 204
    assert not Document.objects.filter(pk=doc_id).exists()
    assert not Page.objects.filter(document_id=doc_id).exists()
    assert not any(os.path.exists(p) for p in paths)


def test_bulk_upload_rotate_and_delete_page(api, make_document):
    doc_id = make_document("general")
    files = [upload(f"g{i}.jpg") for i in range(3)]
    resp = api.post(f"/api/documents/{doc_id}/pages/bulk/", {"images[]": files}, format="multipart")
    assert resp.status_code == 201, resp.content
    pages = resp.json()
    assert [p["order"] for p in pages] == [1, 2, 3]

    # Insert at position 1 shifts the others.
    inserted = api.post(f"/api/documents/{doc_id}/pages/", {"image": upload(), "order": 1}, format="multipart")
    assert inserted.json()["order"] == 1
    assert list(Page.objects.filter(document_id=doc_id).values_list("id", flat=True).order_by("order")) == [
        inserted.json()["id"], *[p["id"] for p in pages]
    ]

    rotated = api.patch(f"/api/pages/{pages[0]['id']}/", {"rotation": 180}, format="json")
    assert rotated.status_code == 200 and rotated.json()["rotation"] == 180
    assert api.patch(f"/api/pages/{pages[0]['id']}/", {"rotation": 45}, format="json").status_code == 400

    assert api.delete(f"/api/pages/{inserted.json()['id']}/").status_code == 204
    orders = list(Page.objects.filter(document_id=doc_id).order_by("order").values_list("id", "order"))
    assert orders == [(p["id"], i) for i, p in enumerate(pages, start=1)]
    assert Document.objects.get(pk=doc_id).page_count == 3


def test_reprocess_after_adding_page(api, make_document):
    doc_id = make_document("general", pages=1)
    api.post(f"/api/documents/{doc_id}/submit/")
    first_pdf = Page.objects.get(document_id=doc_id).page_pdf.name

    assert api.post(f"/api/documents/{doc_id}/pages/", {"image": upload()}, format="multipart").status_code == 201
    resp = api.post(f"/api/documents/{doc_id}/reprocess/")
    assert resp.status_code == 202

    detail = api.get(f"/api/documents/{doc_id}/").json()
    assert detail["status"] == "ready" and detail["page_count"] == 2
    assert "--- Page 2 ---" in detail["full_text"]
    # The already-processed page was not OCR'd again.
    assert Page.objects.get(document_id=doc_id, order=1).page_pdf.name == first_pdf
    # reprocess is only for ready documents.
    Document.objects.filter(pk=doc_id).update(status="draft")
    assert api.post(f"/api/documents/{doc_id}/reprocess/").status_code == 409


def test_swagger_docs_available(client):
    assert client.get("/api/docs/").status_code == 200
    assert client.get("/api/schema/").status_code == 200


def test_media_root_is_temporary():
    assert "docscan-test-media" in str(settings.MEDIA_ROOT)
