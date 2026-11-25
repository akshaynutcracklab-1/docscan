"""Ownership, state rules and upload validation."""
from unittest import mock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from documents.models import Document, Page
from tests.conftest import upload

pytestmark = pytest.mark.django_db


def test_other_users_documents_are_invisible(api, other_api, make_document):
    doc_id = make_document("invoice", pages=1)
    page_id = Page.objects.get(document_id=doc_id).id

    assert other_api.get("/api/documents/").json()["count"] == 0
    for method, url in [
        ("get", f"/api/documents/{doc_id}/"),
        ("get", f"/api/documents/{doc_id}/status/"),
        ("get", f"/api/documents/{doc_id}/download/?format=txt"),
        ("patch", f"/api/documents/{doc_id}/"),
        ("delete", f"/api/documents/{doc_id}/"),
        ("post", f"/api/documents/{doc_id}/submit/"),
        ("patch", f"/api/pages/{page_id}/"),
        ("delete", f"/api/pages/{page_id}/"),
    ]:
        assert getattr(other_api, method)(url, {}, format="json").status_code == 404, (method, url)
    upload_resp = other_api.post(f"/api/documents/{doc_id}/pages/", {"image": upload()}, format="multipart")
    assert upload_resp.status_code == 404
    assert Document.objects.filter(pk=doc_id).exists()


def test_malformed_id_is_404(api):
    assert api.get("/api/documents/not-a-uuid/").status_code == 404


def test_cannot_submit_twice(api, make_document):
    doc_id = make_document("invoice", pages=1)
    with mock.patch("documents.services.start_pipeline") as start:
        first = api.post(f"/api/documents/{doc_id}/submit/")
        second = api.post(f"/api/documents/{doc_id}/submit/")
    assert first.status_code == 202 and first.json()["status"] == "processing"
    assert second.status_code == 409
    assert start.call_count == 1


def test_cannot_submit_without_pages(api, make_document):
    doc_id = make_document("invoice")
    resp = api.post(f"/api/documents/{doc_id}/submit/")
    assert resp.status_code == 400
    assert Document.objects.get(pk=doc_id).status == "draft"


def test_cannot_upload_above_max_pages(api, make_document):
    doc_id = make_document("id_document", pages=2)  # max_pages = 2
    resp = api.post(f"/api/documents/{doc_id}/pages/", {"image": upload()}, format="multipart")
    assert resp.status_code == 400
    assert "at most 2 pages" in str(resp.json())

    bulk_doc = make_document("id_document")
    resp = api.post(f"/api/documents/{bulk_doc}/pages/bulk/", {"images[]": [upload() for _ in range(3)]},
                    format="multipart")
    assert resp.status_code == 400
    assert Page.objects.filter(document_id=bulk_doc).count() == 0


def test_cannot_submit_above_max_pages(api, make_document, document_types):
    doc_id = make_document("invoice", pages=3)
    document_types["invoice"].max_pages = 2
    document_types["invoice"].save()
    assert api.post(f"/api/documents/{doc_id}/submit/").status_code == 400


def test_invalid_file_rejected_by_content(api, make_document):
    doc_id = make_document("general")
    fake = SimpleUploadedFile("scan.jpg", b"definitely not an image", content_type="image/jpeg")
    resp = api.post(f"/api/documents/{doc_id}/pages/", {"image": fake}, format="multipart")
    assert resp.status_code == 400
    assert "image" in resp.json()

    gif = upload("anim.jpg", fmt="GIF", content_type="image/jpeg")
    resp = api.post(f"/api/documents/{doc_id}/pages/", {"image": gif}, format="multipart")
    assert resp.status_code == 400
    assert "Unsupported image format GIF" in str(resp.json())

    bulk = api.post(f"/api/documents/{doc_id}/pages/bulk/", {"images[]": [upload(), fake]}, format="multipart")
    assert bulk.status_code == 400
    assert Page.objects.filter(document_id=doc_id).count() == 0


def test_heic_and_webp_accepted(api, make_document):
    doc_id = make_document("general")
    heic = upload("photo.heic", fmt="HEIF", content_type="image/heic")
    webp = upload("photo.png", fmt="WEBP", content_type="image/png")  # wrong extension on purpose
    for f in (heic, webp):
        assert api.post(f"/api/documents/{doc_id}/pages/", {"image": f}, format="multipart").status_code == 201
    names = list(Page.objects.filter(document_id=doc_id).order_by("order").values_list("image", flat=True))
    assert names[0].endswith("1.heic") and names[1].endswith("2.webp")

    api.post(f"/api/documents/{doc_id}/submit/")
    assert Document.objects.get(pk=doc_id).status == "ready"
    assert all(p.image.name.endswith(".jpg") for p in Page.objects.filter(document_id=doc_id))


@override_settings(MAX_PAGE_UPLOAD_BYTES=1000)
def test_oversized_file_rejected(api, make_document):
    doc_id = make_document("general")
    resp = api.post(f"/api/documents/{doc_id}/pages/", {"image": upload(size=(800, 800))}, format="multipart")
    assert resp.status_code == 400
    assert "MB or smaller" in str(resp.json())


def test_pages_locked_while_processing(api, make_document):
    doc_id = make_document("general", pages=2)
    page_ids = list(Page.objects.filter(document_id=doc_id).values_list("id", flat=True))
    Document.objects.filter(pk=doc_id).update(status=Document.Status.PROCESSING)

    assert api.post(f"/api/documents/{doc_id}/pages/", {"image": upload()}, format="multipart").status_code == 409
    assert api.patch(f"/api/documents/{doc_id}/pages/reorder/", {"page_ids": page_ids[::-1]},
                     format="json").status_code == 409
    assert api.patch(f"/api/pages/{page_ids[0]}/", {"rotation": 90}, format="json").status_code == 409
    assert api.delete(f"/api/pages/{page_ids[0]}/").status_code == 409


def test_reorder_requires_exact_page_set(api, make_document):
    doc_id = make_document("general", pages=2)
    ids = list(Page.objects.filter(document_id=doc_id).values_list("id", flat=True))
    for bad in ([ids[0]], [ids[0], ids[0]], [ids[0], ids[1], 999]):
        assert api.patch(f"/api/documents/{doc_id}/pages/reorder/", {"page_ids": bad}, format="json").status_code == 400


def test_upload_throttle(api, make_document, settings):
    from rest_framework.settings import api_settings
    from rest_framework.throttling import SimpleRateThrottle

    doc_id = make_document("general")
    with mock.patch.object(SimpleRateThrottle, "THROTTLE_RATES", {**api_settings.DEFAULT_THROTTLE_RATES, "uploads": "2/min"}):
        codes = [api.post(f"/api/documents/{doc_id}/pages/", {"image": upload()}, format="multipart").status_code
                 for _ in range(3)]
    assert codes == [201, 201, 429]
