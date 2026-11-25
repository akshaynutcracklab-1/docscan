"""Write-side business logic for documents and pages. Views stay thin and call these."""
from __future__ import annotations

import logging
from typing import Iterable, Sequence

from django.contrib.postgres.search import SearchVector
from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .exceptions import Conflict, ServiceUnavailable
from .models import Document, DocumentType, Page

logger = logging.getLogger(__name__)

# Large enough that shifted orders never collide with real ones during a two-step renumber.
_ORDER_OFFSET = 1_000_000


def default_title(document_type: DocumentType) -> str:
    return f"{document_type.name} – {timezone.localdate():%d %b %Y}"


def create_document(*, owner, document_type: DocumentType, title: str = "") -> Document:
    return Document.objects.create(
        owner=owner,
        document_type=document_type,
        title=(title or "").strip() or default_title(document_type),
    )


def _lock(document: Document) -> Document:
    return Document.objects.select_for_update().select_related("document_type").get(pk=document.pk)


def _ensure_pages_editable(document: Document) -> None:
    if not document.is_page_editable:
        raise Conflict(f"Pages cannot be changed while the document is {document.status}.")


def _shift_orders(document: Document, from_order: int, delta: int) -> None:
    """Move every page with order >= from_order by `delta`, without tripping the unique constraint."""
    pages = Page.objects.filter(document=document, order__gte=from_order)
    pages.update(order=F("order") + _ORDER_OFFSET)
    Page.objects.filter(document=document, order__gte=from_order + _ORDER_OFFSET).update(
        order=F("order") - _ORDER_OFFSET + delta
    )


def _apply_order(document: Document, page_ids_in_order: Sequence[int]) -> None:
    Page.objects.filter(document=document).update(order=F("order") + _ORDER_OFFSET)
    for index, page_id in enumerate(page_ids_in_order, start=1):
        Page.objects.filter(pk=page_id, document=document).update(order=index)


def _after_pages_changed(document: Document) -> None:
    document.page_count = document.pages.count()
    fields = ["page_count", "updated_at"]
    if document.status == Document.Status.DRAFT and document.page_count:
        document.status = Document.Status.UPLOADING
        fields.append("status")
    document.save(update_fields=fields)


def add_pages(document: Document, images: Iterable, *, order: int | None = None, rotation: int = 0) -> list[Page]:
    """Add one or more already-validated images. `order` (1-based) inserts at that position."""
    images = list(images)
    with transaction.atomic():
        document = _lock(document)
        _ensure_pages_editable(document)
        current = document.pages.count()
        limit = document.document_type.max_pages
        if current + len(images) > limit:
            raise ValidationError(
                {"image": [f"{document.document_type.name} documents can have at most {limit} pages "
                           f"({current} already uploaded)."]}
            )

        if order is None or order > current:
            order = current + 1
        else:
            _shift_orders(document, order, len(images))

        created = []
        for offset, image in enumerate(images):
            page = Page(document=document, order=order + offset, rotation=rotation, file_size=image.size or 0)
            page.image.save(image.name or "page.jpg", image, save=False)
            page.save()
            created.append(page)
        _after_pages_changed(document)
    return created


def reorder_pages(document: Document, page_ids: Sequence[int]) -> None:
    with transaction.atomic():
        document = _lock(document)
        _ensure_pages_editable(document)
        existing = set(document.pages.values_list("id", flat=True))
        if len(page_ids) != len(set(page_ids)) or set(page_ids) != existing:
            raise ValidationError({"page_ids": ["Must list every page of this document exactly once."]})
        _apply_order(document, page_ids)
        document.save(update_fields=["updated_at"])


def update_page_rotation(page: Page, rotation: int) -> Page:
    with transaction.atomic():
        document = _lock(page.document)
        _ensure_pages_editable(document)
        page.rotation = rotation
        fields = ["rotation", "updated_at"]
        if page.status != Page.Status.PENDING:
            # The page must be prepared and OCR'd again on the next (re)process.
            page.status = Page.Status.PENDING
            fields.append("status")
        page.save(update_fields=fields)
    return page


def delete_page(page: Page) -> None:
    with transaction.atomic():
        document = _lock(page.document)
        _ensure_pages_editable(document)
        page.delete()
        remaining = list(document.pages.order_by("order").values_list("id", flat=True))
        _apply_order(document, remaining)
        _after_pages_changed(document)


# ------------------------------------------------------------------ processing lifecycle

def _validate_page_count(document: Document) -> None:
    count = document.pages.count()
    if count == 0:
        raise ValidationError({"detail": "Add at least one page before submitting."})
    if count > document.document_type.max_pages:
        raise ValidationError(
            {"detail": f"{document.document_type.name} documents can have at most "
                       f"{document.document_type.max_pages} pages."}
        )


def _transition_to_processing(document: Document, allowed: Iterable[str], action: str) -> Document:
    with transaction.atomic():
        document = _lock(document)
        if document.status not in allowed:
            raise Conflict(f"Cannot {action} a document that is {document.status}.")
        _validate_page_count(document)
        if action == "retry":
            document.pages.filter(status=Page.Status.FAILED).update(status=Page.Status.PENDING)
        document.status = Document.Status.PROCESSING
        document.progress = 0
        document.current_step = "queued"
        document.error_message = ""
        document.page_count = document.pages.count()
        document.save(update_fields=["status", "progress", "current_step", "error_message", "page_count", "updated_at"])
    # Started after the commit so the worker always sees status=processing.
    start_pipeline(document)
    document.refresh_from_db()
    return document


def submit_document(document: Document) -> Document:
    return _transition_to_processing(document, Document.SUBMITTABLE_STATUSES, "submit")


def retry_document(document: Document) -> Document:
    return _transition_to_processing(document, (Document.Status.FAILED,), "retry")


def reprocess_document(document: Document) -> Document:
    return _transition_to_processing(document, (Document.Status.READY,), "reprocess")


def start_pipeline(document: Document) -> None:
    from processing.tasks import run_pipeline

    try:
        run_pipeline(document.id)
    except Exception as exc:
        logger.exception("Could not start processing for document %s", document.id)
        Document.objects.filter(pk=document.pk).update(
            status=Document.Status.FAILED,
            error_message="Processing could not be started (task queue unavailable).",
            current_step="failed",
        )
        raise ServiceUnavailable() from exc


def update_document(document: Document, validated_data: dict) -> Document:
    if "document_type" in validated_data and validated_data["document_type"] != document.document_type:
        if document.status not in Document.SUBMITTABLE_STATUSES:
            raise Conflict("The document type can only be changed before the document is submitted.")
        if document.pages.count() > validated_data["document_type"].max_pages:
            raise ValidationError({"document_type": ["This document has more pages than that type allows."]})
    if "full_text" in validated_data and document.status == Document.Status.PROCESSING:
        raise Conflict("The text cannot be edited while the document is processing.")
    for field, value in validated_data.items():
        setattr(document, field, value)
    document.save()
    if {"title", "full_text"} & validated_data.keys():
        update_search_vector(document)
    return document


def update_search_vector(document: Document) -> None:
    if connection.vendor != "postgresql":
        return
    Document.objects.filter(pk=document.pk).update(
        search_vector=SearchVector("title", weight="A", config="simple")
        + SearchVector("full_text", weight="B", config="simple")
    )
