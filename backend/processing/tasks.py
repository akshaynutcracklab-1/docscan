"""Celery pipeline: prepare_pages -> ocr_pages -> build_pdf -> extract_fields -> analyze_document.

Every step:
* reloads the document and silently skips if it is no longer `processing` (failed earlier,
  deleted, ...), so the chain needs no special stop logic;
* updates `progress` / `current_step`;
* retries up to 3 times with exponential backoff on transient Azure errors (429 / 5xx / network);
* marks the document `failed` with a user-safe message on anything else.
"""
from __future__ import annotations

import logging
from statistics import mean

from celery import chain, shared_task
from django.conf import settings
from django.core.files.base import ContentFile

from documents.models import Document, Page
from documents.services import update_search_vector

from . import extractors, ocr, pdf_utils
from .analysis import analyze_text
from .azure_client import AzureProcessingError, TransientAzureError
from .image_utils import InvalidImage, prepare_image

logger = logging.getLogger(__name__)

PROGRESS = {
    "prepare_pages": (0, 10),
    "ocr_pages": (10, 70),
    "build_pdf": (70, 80),
    "extract_fields": (80, 90),
    "analyze": (90, 100),
}
STEP_LABELS = {
    "prepare_pages": "Preparing pages",
    "ocr_pages": "Recognising text",
    "build_pdf": "Building PDF",
    "extract_fields": "Extracting fields",
    "analyze": "Analysing text",
}
F0_MAX_PAGES_PER_REQUEST = 2


class PipelineError(Exception):
    """A failure with a message that is safe to show to the user."""


# ------------------------------------------------------------------ orchestration

def run_pipeline(document_id) -> None:
    document_id = str(document_id)
    chain(
        prepare_pages.si(document_id),
        ocr_pages.si(document_id),
        build_pdf.si(document_id),
        extract_fields.si(document_id),
        analyze_document.si(document_id),
    ).apply_async()


def _set_progress(document_id, progress: int, step: str) -> None:
    Document.objects.filter(pk=document_id, status=Document.Status.PROCESSING).update(
        progress=progress, current_step=step
    )


def _fail(document_id, step: str, exc: BaseException) -> None:
    if isinstance(exc, (PipelineError, AzureProcessingError, InvalidImage)):
        message = str(exc)
    else:
        message = f"Unexpected error while {STEP_LABELS[step].lower()}."
    logger.error("Document %s failed at %s: %s", document_id, step, exc, exc_info=exc)
    Document.objects.filter(pk=document_id).update(
        status=Document.Status.FAILED, error_message=message[:2000], current_step=f"failed:{step}"
    )


def _run_step(task, document_id: str, step: str, func) -> str:
    document = (
        Document.objects.select_related("document_type").filter(pk=document_id).first()
    )
    if document is None or document.status != Document.Status.PROCESSING:
        logger.info("Skipping %s for document %s (not processing)", step, document_id)
        return document_id

    _set_progress(document_id, PROGRESS[step][0], step)
    try:
        func(document)
    except TransientAzureError as exc:
        retries = task.request.retries
        if retries < task.max_retries:
            countdown = settings.PIPELINE_RETRY_BACKOFF * (2 ** retries)
            logger.warning("Transient Azure error in %s for %s (attempt %s): %s; retrying in %ss",
                           step, document_id, retries + 1, exc, countdown)
            raise task.retry(exc=exc, countdown=countdown)
        _fail(document_id, step, exc)
    except Exception as exc:  # noqa: BLE001 - every failure must end in status=failed
        _fail(document_id, step, exc)
    else:
        _set_progress(document_id, PROGRESS[step][1], step)
    return document_id


TASK_OPTIONS = {"bind": True, "max_retries": settings.PIPELINE_MAX_RETRIES, "acks_late": True}


@shared_task(name="processing.prepare_pages", **TASK_OPTIONS)
def prepare_pages(self, document_id: str) -> str:
    return _run_step(self, document_id, "prepare_pages", _prepare_pages)


@shared_task(name="processing.ocr_pages", **TASK_OPTIONS)
def ocr_pages(self, document_id: str) -> str:
    return _run_step(self, document_id, "ocr_pages", _ocr_pages)


@shared_task(name="processing.build_pdf", **TASK_OPTIONS)
def build_pdf(self, document_id: str) -> str:
    return _run_step(self, document_id, "build_pdf", _build_pdf)


@shared_task(name="processing.extract_fields", **TASK_OPTIONS)
def extract_fields(self, document_id: str) -> str:
    return _run_step(self, document_id, "extract_fields", _extract_fields)


@shared_task(name="processing.analyze_document", **TASK_OPTIONS)
def analyze_document(self, document_id: str) -> str:
    return _run_step(self, document_id, "analyze", _analyze)


# ------------------------------------------------------------------ helpers

def _read(field_file) -> bytes:
    with field_file.open("rb") as fh:
        return fh.read()


def _replace_file(instance, field_name: str, data: bytes, filename: str) -> None:
    """Save new content for a FileField and remove the previous file from storage."""
    field_file = getattr(instance, field_name)
    old_name = field_file.name
    field_file.save(filename, ContentFile(data), save=False)
    if old_name and old_name != field_file.name:
        field_file.storage.delete(old_name)


def _pages(document: Document, *, pending_only: bool = False):
    qs = document.pages.order_by("order")
    return qs.exclude(status=Page.Status.DONE) if pending_only else qs


# ------------------------------------------------------------------ steps

def _prepare_pages(document: Document) -> None:
    """EXIF orientation + rotation, HEIC -> JPEG, long side <= 3000 px, < 4 MB."""
    for page in _pages(document, pending_only=True):
        try:
            prepared = prepare_image(_read(page.image), page.rotation)
        except Exception as exc:
            Page.objects.filter(pk=page.pk).update(status=Page.Status.FAILED)
            raise PipelineError(f"Page {page.order} could not be read as an image.") from exc
        if prepared.changed:
            _replace_file(page, "image", prepared.data, f"{page.order}.jpg")
            page.rotation = 0  # baked into the stored image
        page.width, page.height, page.file_size = prepared.width, prepared.height, prepared.size
        page.save(update_fields=["image", "rotation", "width", "height", "file_size", "updated_at"])


def _ocr_pages(document: Document) -> None:
    """One `prebuilt-read` call per page (F0 analyses at most 2 pages per request)."""
    start, end = PROGRESS["ocr_pages"]
    pending = list(_pages(document, pending_only=True))
    threshold = settings.LOW_CONFIDENCE_THRESHOLD
    for index, page in enumerate(pending, start=1):
        image_bytes = _read(page.image)
        try:
            result = ocr.read_page(image_bytes)
        except TransientAzureError:
            raise  # retried by the task; the page stays pending
        except Exception:
            Page.objects.filter(pk=page.pk).update(status=Page.Status.FAILED)
            raise
        page.ocr_text = result.text
        page.ocr_data = result.ocr_data
        page.ocr_confidence = result.confidence
        page.is_low_quality = result.confidence is not None and result.confidence < threshold
        _replace_file(page, "page_pdf", result.pdf_bytes or pdf_utils.image_to_pdf(image_bytes), f"{page.order}.pdf")
        page.status = Page.Status.DONE
        page.save()
        _set_progress(document.pk, start + round((end - start) * index / len(pending)), "ocr_pages")


def _build_pdf(document: Document) -> None:
    pages = list(_pages(document))
    pdfs = [_read(p.page_pdf) if p.page_pdf else pdf_utils.image_to_pdf(_read(p.image)) for p in pages]
    _replace_file(document, "pdf_file", pdf_utils.merge_pdfs(pdfs), "document.pdf")

    document.full_text = "\n\n".join(f"--- Page {p.order} ---\n\n{p.ocr_text}" for p in pages)
    languages = [p.ocr_data.get("language") for p in pages if p.ocr_data.get("language")]
    document.language = max(set(languages), key=languages.count) if languages else ""
    confidences = [p.ocr_confidence for p in pages if p.ocr_confidence is not None]
    document.average_confidence = round(mean(confidences), 4) if confidences else None
    document.page_count = len(pages)
    document.save(update_fields=["pdf_file", "full_text", "language", "average_confidence", "page_count", "updated_at"])


def _extract_fields(document: Document) -> None:
    doc_type = document.document_type
    if not doc_type.azure_field_model:
        document.extracted_fields = {}
    elif settings.DOCINTEL_TIER == "S0":
        # Paid tier: one request with the merged PDF covers every page.
        document.extracted_fields = extractors.extract_fields(doc_type, _read(document.pdf_file))
    else:
        pages = list(_pages(document))
        if doc_type.azure_field_model != extractors.LAYOUT_MODEL:
            # Field models only need the first page(s): respect max_pages and the F0 2-page limit.
            pages = pages[: min(doc_type.max_pages, F0_MAX_PAGES_PER_REQUEST)]
        document.extracted_fields = extractors.extract_fields_from_files(doc_type, (_read(p.image) for p in pages))
    document.save(update_fields=["extracted_fields", "updated_at"])


def _analyze(document: Document) -> None:
    low = [
        {"page": p.order, "confidence": p.ocr_confidence}
        for p in _pages(document)
        if p.is_low_quality
    ]
    document.analysis = analyze_text(document.full_text, low_confidence_pages=low)
    document.save(update_fields=["analysis", "updated_at"])
    update_search_vector(document)
    Document.objects.filter(pk=document.pk, status=Document.Status.PROCESSING).update(
        status=Document.Status.READY, progress=100, current_step="done", error_message=""
    )
