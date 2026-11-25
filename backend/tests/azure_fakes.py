"""A fake DocumentIntelligenceClient returning real SDK model objects."""
from __future__ import annotations

from azure.ai.documentintelligence.models import AnalyzeResult
from azure.core.exceptions import HttpResponseError

from processing.pdf_utils import images_to_pdf
from tests.conftest import image_bytes

BOX = [0.1, 0.1, 1.0, 0.1, 1.0, 0.3, 0.1, 0.3]


def read_result(text="Hello Azure world", confidence=0.99, locale="en") -> AnalyzeResult:
    words, offset = [], 0
    for token in text.split():
        words.append({"content": token, "polygon": BOX, "confidence": confidence,
                      "span": {"offset": offset, "length": len(token)}})
        offset += len(token) + 1
    return AnalyzeResult({
        "apiVersion": "2024-11-30", "modelId": "prebuilt-read", "stringIndexType": "textElements",
        "content": text,
        "pages": [{"pageNumber": 1, "angle": 0.0, "width": 8.5, "height": 11.0, "unit": "inch", "spans": [],
                   "words": words, "lines": [{"content": text, "polygon": BOX, "spans": []}]}],
        "languages": [{"locale": locale, "confidence": 0.9, "spans": [{"offset": 0, "length": len(text)}]}],
    })


def invoice_result() -> AnalyzeResult:
    cur = lambda amount: {"amount": amount, "currencySymbol": "$", "currencyCode": "USD"}  # noqa: E731
    return AnalyzeResult({
        "apiVersion": "2024-11-30", "modelId": "prebuilt-invoice", "stringIndexType": "textElements",
        "content": "Contoso invoice",
        "documents": [{
            "docType": "invoice", "confidence": 0.9, "spans": [],
            "fields": {
                "VendorName": {"type": "string", "valueString": "Contoso", "content": "Contoso", "confidence": 0.95},
                "InvoiceDate": {"type": "date", "valueDate": "2025-01-15", "content": "15 Jan 2025", "confidence": 0.9},
                "InvoiceTotal": {"type": "currency", "valueCurrency": cur(110.0), "content": "$110.00", "confidence": 0.93},
                "Items": {"type": "array", "valueArray": [{
                    "type": "object", "content": "Widget 2 50 100",
                    "valueObject": {
                        "Description": {"type": "string", "valueString": "Widget", "content": "Widget"},
                        "Quantity": {"type": "number", "valueNumber": 2, "content": "2"},
                        "UnitPrice": {"type": "currency", "valueCurrency": cur(50.0), "content": "$50"},
                        "Amount": {"type": "currency", "valueCurrency": cur(100.0), "content": "$100"},
                    },
                }]},
                "CustomerAddress": {"type": "address", "content": "1 Main St",
                                    "valueAddress": {"streetAddress": "1 Main St", "city": "Pune"}},
            },
        }],
    })


def layout_result() -> AnalyzeResult:
    return AnalyzeResult({
        "apiVersion": "2024-11-30", "modelId": "prebuilt-layout", "stringIndexType": "textElements",
        "content": "| A | B |\n| --- | --- |\n| 1 | 2 |",
        "tables": [{"rowCount": 2, "columnCount": 2, "cells": [
            {"rowIndex": 0, "columnIndex": 0, "content": "A", "kind": "columnHeader"},
            {"rowIndex": 0, "columnIndex": 1, "content": "B", "kind": "columnHeader"},
            {"rowIndex": 1, "columnIndex": 0, "content": "1"},
            {"rowIndex": 1, "columnIndex": 1, "content": "2"},
        ]}],
    })


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code
        self.reason = "error"
        self.headers = {}

    def text(self):
        return ""


def http_error(status_code: int, message="boom") -> HttpResponseError:
    err = HttpResponseError(message=message, response=_Response(status_code))
    err.status_code = status_code
    return err


class FakePoller:
    def __init__(self, result, operation_id):
        self._result = result
        self.details = {"operation_id": operation_id}

    def result(self, timeout=None):
        return self._result


class FakeClient:
    """Records calls. `failures` is a list of exceptions raised by the next analyze calls."""

    def __init__(self, failures=None, results=None):
        self.failures = list(failures or [])
        self.results = results or {}
        self.calls = []
        self.pdf_requests = []

    def begin_analyze_document(self, model_id, body, **kwargs):
        self.calls.append((model_id, kwargs))
        if self.failures:
            raise self.failures.pop(0)
        result = self.results.get(model_id) or (read_result() if model_id == "prebuilt-read" else invoice_result())
        return FakePoller(result, f"op-{len(self.calls)}")

    def get_analyze_result_pdf(self, model_id, result_id):
        self.pdf_requests.append((model_id, result_id))
        return iter([images_to_pdf([image_bytes()])])
