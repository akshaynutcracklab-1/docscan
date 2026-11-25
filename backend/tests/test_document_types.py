import pytest

from documents.models import DocumentType

pytestmark = pytest.mark.django_db


def test_lists_only_active_types_in_sort_order(api, document_types):
    DocumentType.objects.filter(code="receipt").update(is_active=False)
    DocumentType.objects.filter(code="contract").update(sort_order=0)

    resp = api.get("/api/document-types/")
    assert resp.status_code == 200
    codes = [t["code"] for t in resp.json()]
    assert codes == ["contract", "general", "invoice", "id_document", "form_table"]

    invoice = next(t for t in resp.json() if t["code"] == "invoice")
    assert set(invoice) >= {"code", "name", "description", "icon", "max_pages", "display_fields"}
    assert invoice["display_fields"][:2] == ["VendorName", "InvoiceId"]
    assert invoice["has_field_extraction"] is True


def test_fixture_values(document_types):
    assert document_types["general"].azure_field_model is None
    assert document_types["invoice"].azure_field_model == "prebuilt-invoice"
    assert document_types["form_table"].azure_field_model == "prebuilt-layout"
    assert document_types["id_document"].max_pages == 2
    assert document_types["contract"].max_pages == 50


def test_inactive_type_cannot_be_used(api, document_types):
    DocumentType.objects.filter(code="receipt").update(is_active=False)
    resp = api.post("/api/documents/", {"document_type": "receipt"}, format="json")
    assert resp.status_code == 400
