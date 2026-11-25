"""Type-specific field extraction with Azure prebuilt models (or mock data).

Output shapes stored in `Document.extracted_fields`:

* invoice / receipt / id_document / contract:
  ``{FieldName: {"value": ..., "content": "...", "confidence": 0.97}}``; ``Items`` has a list of
  ``{Description, Quantity, UnitPrice, Amount}`` as its value.
* form_table (prebuilt-layout):
  ``{"tables": [{row_count, column_count, cells: [{row, col, content}]}], "markdown": "..."}``
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Iterable

from azure.ai.documentintelligence.models import DocumentContentFormat
from django.conf import settings

from . import azure_client

LAYOUT_MODEL = "prebuilt-layout"

# Item sub-fields differ per model; normalise them to one shape.
ITEM_FIELD_MAP = {
    "prebuilt-invoice": {"Description": "Description", "Quantity": "Quantity", "UnitPrice": "UnitPrice", "Amount": "Amount"},
    "prebuilt-receipt": {"Description": "Description", "Quantity": "Quantity", "UnitPrice": "Price", "Amount": "TotalPrice"},
}


def extract_fields(doc_type, file_bytes: bytes) -> dict:
    """Run the document type's field model on one file (image or PDF)."""
    model_id = getattr(doc_type, "azure_field_model", None)
    if not model_id:
        return {}
    if settings.OCR_ENGINE == "mock":
        return mock_fields(model_id)
    kwargs = {}
    if model_id == LAYOUT_MODEL:
        kwargs["output_content_format"] = DocumentContentFormat.MARKDOWN
    result, _ = azure_client.analyze(model_id, file_bytes, **kwargs)
    return normalize_result(model_id, result)


def extract_fields_from_files(doc_type, files: Iterable[bytes]) -> dict:
    """Extract from several single-page files (F0 tier) and merge them into one result."""
    merged: dict = {}
    for file_bytes in files:
        merged = merge_results(merged, extract_fields(doc_type, file_bytes))
    return merged


def merge_results(base: dict, extra: dict) -> dict:
    """Earlier pages win for scalar fields; tables, items and markdown are concatenated."""
    if not base:
        return dict(extra)
    out = dict(base)
    for key, value in extra.items():
        if key == "tables":
            out["tables"] = (out.get("tables") or []) + value
        elif key == "markdown":
            out["markdown"] = "\n\n".join(filter(None, [out.get("markdown"), value]))
        elif key == "Items" and key in out:
            out["Items"] = {
                **out["Items"],
                "value": (out["Items"].get("value") or []) + (value.get("value") or []),
            }
        elif key not in out or _is_empty(out[key]):
            out[key] = value
    return out


def _is_empty(field: Any) -> bool:
    return isinstance(field, dict) and field.get("value") in (None, "", [], {})


# ------------------------------------------------------------------ normalisation

def normalize_result(model_id: str, result) -> dict:
    if model_id == LAYOUT_MODEL:
        return {"tables": normalize_tables(result), "markdown": getattr(result, "content", "") or ""}
    documents = getattr(result, "documents", None) or []
    if not documents:
        return {}
    fields = documents[0].fields or {}
    normalized = {name: normalize_field(f) for name, f in fields.items()}
    if "Items" in fields and model_id in ITEM_FIELD_MAP:
        normalized["Items"] = normalize_items(fields["Items"], ITEM_FIELD_MAP[model_id])
    return normalized


def normalize_tables(result) -> list[dict]:
    return [
        {
            "row_count": table.row_count,
            "column_count": table.column_count,
            "cells": [
                {"row": c.row_index, "col": c.column_index, "content": c.content, "kind": getattr(c, "kind", None)}
                for c in (table.cells or [])
            ],
        }
        for table in (getattr(result, "tables", None) or [])
    ]


def normalize_field(field) -> dict:
    return {
        "value": field_value(field),
        "content": getattr(field, "content", None),
        "confidence": getattr(field, "confidence", None),
    }


def normalize_items(items_field, mapping: dict[str, str]) -> dict:
    rows = []
    for item in getattr(items_field, "value_array", None) or []:
        obj = getattr(item, "value_object", None) or {}
        row = {}
        for target, source in mapping.items():
            sub = obj.get(source)
            value = field_value(sub) if sub is not None else None
            if isinstance(value, dict) and "amount" in value:  # currency -> plain number
                value = value["amount"]
            row[target] = value
        rows.append(row)
    return {
        "value": rows,
        "content": getattr(items_field, "content", None),
        "confidence": getattr(items_field, "confidence", None),
    }


def field_value(field) -> Any:
    """Turn a DocumentField into plain JSON-serialisable data."""
    if field is None:
        return None
    raw_kind = getattr(field, "type", "") or ""
    kind = str(getattr(raw_kind, "value", raw_kind))  # SDK may give a str-Enum or a plain str
    if kind == "array":
        return [field_value(item) for item in (field.value_array or [])]
    if kind == "object":
        return {key: field_value(value) for key, value in (field.value_object or {}).items()}
    if kind == "currency" and field.value_currency is not None:
        cur = field.value_currency
        return {"amount": cur.amount, "currency_symbol": cur.currency_symbol, "currency_code": cur.currency_code}
    if kind == "address" and field.value_address is not None:
        addr = field.value_address
        return addr.as_dict() if hasattr(addr, "as_dict") else dict(addr)
    attribute = {
        "string": "value_string",
        "date": "value_date",
        "time": "value_time",
        "phoneNumber": "value_phone_number",
        "number": "value_number",
        "integer": "value_integer",
        "selectionMark": "value_selection_mark",
        "countryRegion": "value_country_region",
        "signature": "value_signature",
        "boolean": "value_boolean",
    }.get(kind)
    value = getattr(field, attribute, None) if attribute else None
    if value is None:
        return getattr(field, "content", None)
    if isinstance(value, (dt.date, dt.time, dt.datetime)):
        return value.isoformat()
    return value


# ------------------------------------------------------------------ mock engine

def _f(value, content=None, confidence=0.95):
    return {"value": value, "content": content if content is not None else str(value), "confidence": confidence}


def mock_fields(model_id: str) -> dict:
    if model_id == "prebuilt-invoice":
        return {
            "VendorName": _f("Mock Supplies Pvt Ltd"),
            "InvoiceId": _f("INV-1001"),
            "InvoiceDate": _f("2025-01-15", "15/01/2025"),
            "DueDate": _f("2025-02-14", "14/02/2025"),
            "SubTotal": _f({"amount": 1059.75, "currency_symbol": "₹", "currency_code": "INR"}, "₹1,059.75"),
            "TotalTax": _f({"amount": 190.75, "currency_symbol": "₹", "currency_code": "INR"}, "₹190.75"),
            "InvoiceTotal": _f({"amount": 1250.50, "currency_symbol": "₹", "currency_code": "INR"}, "₹1,250.50"),
            "Items": {
                "value": [{"Description": "A4 paper ream", "Quantity": 5, "UnitPrice": 211.95, "Amount": 1059.75}],
                "content": None,
                "confidence": 0.93,
            },
        }
    if model_id == "prebuilt-receipt":
        return {
            "MerchantName": _f("Mock Café"),
            "TransactionDate": _f("2025-01-15", "15/01/2025"),
            "Total": _f({"amount": 350.0, "currency_symbol": "₹", "currency_code": "INR"}, "₹350.00"),
            "Items": {
                "value": [{"Description": "Coffee", "Quantity": 2, "UnitPrice": 175.0, "Amount": 350.0}],
                "content": None,
                "confidence": 0.92,
            },
        }
    if model_id == "prebuilt-idDocument":
        return {
            "FirstName": _f("Asha"),
            "LastName": _f("Kumar"),
            "DocumentNumber": _f("X1234567"),
            "DateOfBirth": _f("1990-05-20", "20/05/1990"),
            "DateOfExpiration": _f("2030-05-19", "19/05/2030"),
        }
    if model_id == "prebuilt-contract":
        return {
            "Title": _f("Service Agreement"),
            "Parties": _f([{"Name": "Mock Corp"}, {"Name": "Example LLP"}], "Mock Corp and Example LLP"),
            "EffectiveDate": _f("2025-01-01", "1 January 2025"),
        }
    if model_id == LAYOUT_MODEL:
        return {
            "tables": [{
                "row_count": 2,
                "column_count": 2,
                "cells": [
                    {"row": 0, "col": 0, "content": "Item", "kind": "columnHeader"},
                    {"row": 0, "col": 1, "content": "Qty", "kind": "columnHeader"},
                    {"row": 1, "col": 0, "content": "Paper", "kind": "content"},
                    {"row": 1, "col": 1, "content": "5", "kind": "content"},
                ],
            }],
            "markdown": "| Item | Qty |\n| --- | --- |\n| Paper | 5 |",
        }
    return {}
