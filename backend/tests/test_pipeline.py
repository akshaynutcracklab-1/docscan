"""Pipeline failures, retries and the Azure code path (with a fake client, never the network)."""
from unittest import mock

import pytest
from azure.core.exceptions import ServiceRequestError
from django.test import override_settings

from documents.models import Document, Page
from processing import azure_client
from processing.azure_client import AzureProcessingError
from tests.azure_fakes import FakeClient, http_error, layout_result

pytestmark = pytest.mark.django_db


@pytest.fixture
def fake_azure():
    """Switch to OCR_ENGINE=azure and route every SDK call to a FakeClient."""
    def _install(**kwargs):
        client = FakeClient(**kwargs)
        patcher = mock.patch.object(azure_client, "get_client", return_value=client)
        patcher.start()
        installed.append(patcher)
        return client

    installed = []
    with override_settings(OCR_ENGINE="azure"):
        yield _install
    for p in installed:
        p.stop()


def test_failed_azure_call_marks_document_failed_then_retry_succeeds(api, make_document):
    doc_id = make_document("invoice", pages=2)
    error = AzureProcessingError("Azure Document Intelligence analysis with prebuilt-read failed (HTTP 400): bad image")
    with mock.patch("processing.ocr.read_page", side_effect=error):
        assert api.post(f"/api/documents/{doc_id}/submit/").status_code == 202

    status = api.get(f"/api/documents/{doc_id}/status/").json()
    assert status["status"] == "failed"
    assert "HTTP 400" in status["error_message"]
    assert status["current_step"] == "failed:ocr_pages"
    assert Page.objects.get(document_id=doc_id, order=1).status == "failed"
    assert api.post(f"/api/documents/{doc_id}/submit/").status_code == 409  # failed docs use retry

    resp = api.post(f"/api/documents/{doc_id}/retry/")
    assert resp.status_code == 202
    detail = api.get(f"/api/documents/{doc_id}/").json()
    assert detail["status"] == "ready" and detail["error_message"] == ""
    assert detail["extracted_fields"]["InvoiceId"]["value"] == "INV-1001"
    assert api.post(f"/api/documents/{doc_id}/retry/").status_code == 409  # only when failed


def test_unexpected_error_has_generic_message(api, make_document):
    doc_id = make_document("general", pages=1)
    with mock.patch("processing.tasks.pdf_utils.merge_pdfs", side_effect=RuntimeError("secret internals")):
        api.post(f"/api/documents/{doc_id}/submit/")
    document = Document.objects.get(pk=doc_id)
    assert document.status == "failed"
    assert document.error_message == "Unexpected error while building pdf."
    assert "secret" not in document.error_message


def test_azure_path_end_to_end(api, make_document, fake_azure):
    client = fake_azure()
    doc_id = make_document("invoice", pages=3)
    api.post(f"/api/documents/{doc_id}/submit/")

    detail = api.get(f"/api/documents/{doc_id}/").json()
    assert detail["status"] == "ready", detail["error_message"]
    read_calls = [c for c in client.calls if c[0] == "prebuilt-read"]
    assert len(read_calls) == 3  # one request per page
    assert all(c[1]["output"] == ["pdf"] and c[1]["features"] == ["languages"] for c in read_calls)
    assert len(client.pdf_requests) == 3
    # F0: the field model only sees the first two pages, one image per request.
    assert [c[0] for c in client.calls].count("prebuilt-invoice") == 2

    fields = detail["extracted_fields"]
    assert fields["VendorName"] == {"value": "Contoso", "content": "Contoso", "confidence": 0.95}
    assert fields["InvoiceDate"]["value"] == "2025-01-15"
    assert fields["InvoiceTotal"]["value"] == {"amount": 110.0, "currency_symbol": "$", "currency_code": "USD"}
    assert fields["Items"]["value"] == [
        {"Description": "Widget", "Quantity": 2, "UnitPrice": 50.0, "Amount": 100.0},
        {"Description": "Widget", "Quantity": 2, "UnitPrice": 50.0, "Amount": 100.0},
    ]  # items from both pages are concatenated
    assert fields["CustomerAddress"]["value"]["city"] == "Pune"

    page = Page.objects.filter(document_id=doc_id).first()
    assert page.ocr_text == "Hello Azure world"
    assert page.ocr_confidence == pytest.approx(0.99)
    assert page.ocr_data["words"][0] == {"content": "Hello", "polygon": [0.1, 0.1, 1.0, 0.1, 1.0, 0.3, 0.1, 0.3],
                                        "confidence": 0.99}
    assert page.ocr_data["lines"][0]["content"] == "Hello Azure world"
    assert detail["language"] == "en"


@override_settings(DOCINTEL_TIER="S0")
def test_s0_sends_merged_pdf_once(api, make_document, fake_azure):
    client = fake_azure()
    doc_id = make_document("invoice", pages=3)
    api.post(f"/api/documents/{doc_id}/submit/")
    assert Document.objects.get(pk=doc_id).status == "ready"
    assert [c[0] for c in client.calls].count("prebuilt-invoice") == 1


def test_layout_uses_markdown_output(api, make_document, fake_azure):
    client = fake_azure(results={"prebuilt-layout": layout_result()})
    doc_id = make_document("form_table", pages=1)
    api.post(f"/api/documents/{doc_id}/submit/")
    fields = Document.objects.get(pk=doc_id).extracted_fields
    assert fields["tables"][0]["row_count"] == 2
    assert fields["tables"][0]["cells"][2] == {"row": 1, "col": 0, "content": "1", "kind": None}
    assert fields["markdown"].startswith("| A | B |")
    layout_call = next(c for c in client.calls if c[0] == "prebuilt-layout")
    assert layout_call[1]["output_content_format"] == "markdown"


def test_low_confidence_pages_are_flagged(api, make_document, fake_azure):
    from tests.azure_fakes import read_result

    fake_azure(results={"prebuilt-read": read_result(confidence=0.5)})
    doc_id = make_document("general", pages=2)
    api.post(f"/api/documents/{doc_id}/submit/")
    document = Document.objects.get(pk=doc_id)
    assert document.analysis["low_confidence_pages"] == [{"page": 1, "confidence": 0.5}, {"page": 2, "confidence": 0.5}]
    assert all(p.is_low_quality for p in document.pages.all())


@pytest.mark.parametrize("transient", [http_error(429), http_error(503), ServiceRequestError("connection reset")])
def test_transient_errors_are_retried(api, make_document, fake_azure, transient):
    client = fake_azure(failures=[transient, transient])  # fails twice, third attempt works
    doc_id = make_document("general", pages=1)
    api.post(f"/api/documents/{doc_id}/submit/")
    assert Document.objects.get(pk=doc_id).status == "ready"
    assert len([c for c in client.calls if c[0] == "prebuilt-read"]) == 3


def test_transient_errors_give_up_after_three_retries(api, make_document, fake_azure):
    client = fake_azure(failures=[http_error(429)] * 10)
    doc_id = make_document("general", pages=1)
    api.post(f"/api/documents/{doc_id}/submit/")
    document = Document.objects.get(pk=doc_id)
    assert document.status == "failed"
    assert "HTTP 429" in document.error_message
    assert len(client.calls) == 4  # first try + 3 retries


def test_client_errors_are_not_retried(api, make_document, fake_azure):
    client = fake_azure(failures=[http_error(400, "InvalidImage")])
    doc_id = make_document("general", pages=1)
    api.post(f"/api/documents/{doc_id}/submit/")
    document = Document.objects.get(pk=doc_id)
    assert document.status == "failed" and "HTTP 400" in document.error_message
    assert len(client.calls) == 1


def test_queue_down_marks_failed_and_returns_503(api, make_document):
    doc_id = make_document("general", pages=1)
    with mock.patch("processing.tasks.run_pipeline", side_effect=ConnectionError("redis down")):
        resp = api.post(f"/api/documents/{doc_id}/submit/")
    assert resp.status_code == 503
    assert Document.objects.get(pk=doc_id).status == "failed"


def test_missing_azure_config_fails_cleanly(api, make_document):
    azure_client.reset_client()
    with override_settings(OCR_ENGINE="azure", DOCINTEL_KEY=""):
        doc_id = make_document("general", pages=1)
        api.post(f"/api/documents/{doc_id}/submit/")
    document = Document.objects.get(pk=doc_id)
    assert document.status == "failed"
    assert "test-key" not in document.error_message
    azure_client.reset_client()
