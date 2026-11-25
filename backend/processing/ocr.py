"""Per-page OCR with the `prebuilt-read` model (or a mock engine for development and tests)."""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

from azure.ai.documentintelligence.models import AnalyzeOutputOption, DocumentAnalysisFeature
from django.conf import settings

from . import azure_client
from .pdf_utils import image_to_pdf

logger = logging.getLogger(__name__)

READ_MODEL = "prebuilt-read"

MOCK_TEXT = (
    "MOCK OCR TEXT\n"
    "Invoice INV-1001 dated 15/01/2025\n"
    "Total: ₹1,250.50 (USD $15.00)\n"
    "Contact: billing@example.com, +91 98765 43210\n"
    "GSTIN: 27AAPFU0939F1ZV PAN: AAPFU0939F"
)


@dataclass
class OcrResult:
    text: str
    lines: list[dict] = field(default_factory=list)
    words: list[dict] = field(default_factory=list)
    confidence: float | None = None
    language: str = ""
    pdf_bytes: bytes | None = None
    page_info: dict = field(default_factory=dict)

    @property
    def ocr_data(self) -> dict:
        return {**self.page_info, "language": self.language, "lines": self.lines, "words": self.words}


def read_page(image_bytes: bytes) -> OcrResult:
    """OCR a single page image and return text, geometry, confidence, language and a searchable PDF."""
    engine = settings.OCR_ENGINE
    if engine == "mock":
        return _mock_read_page(image_bytes)
    if engine == "azure":
        return _azure_read_page(image_bytes)
    raise ValueError(f"Unknown OCR_ENGINE {engine!r} (use 'azure' or 'mock').")


def _azure_read_page(image_bytes: bytes) -> OcrResult:
    kwargs = {"output": [AnalyzeOutputOption.PDF]}
    if settings.DOCINTEL_DETECT_LANGUAGE:
        # v4 only returns `languages` when the add-on feature is requested.
        kwargs["features"] = [DocumentAnalysisFeature.LANGUAGES]
    result, operation_id = azure_client.analyze(READ_MODEL, image_bytes, **kwargs)
    pdf_bytes = None
    try:
        pdf_bytes = azure_client.get_result_pdf(READ_MODEL, operation_id)
    except azure_client.TransientAzureError:
        raise
    except azure_client.AzureProcessingError:
        # The text is still usable; the pipeline falls back to an image-only page PDF.
        logger.warning("Searchable PDF unavailable for operation %s; using image PDF", operation_id)
    return parse_read_result(result, pdf_bytes)


def parse_read_result(result, pdf_bytes: bytes | None = None) -> OcrResult:
    pages = list(getattr(result, "pages", None) or [])
    lines, words, page_info = [], [], {}
    for page in pages:
        page_info = page_info or {
            "width": page.width,
            "height": page.height,
            "unit": page.unit,
            "angle": page.angle,
        }
        lines += [{"content": ln.content, "polygon": list(ln.polygon or [])} for ln in (page.lines or [])]
        words += [
            {"content": w.content, "polygon": list(w.polygon or []), "confidence": w.confidence}
            for w in (page.words or [])
        ]
    confidences = [w["confidence"] for w in words if w["confidence"] is not None]
    return OcrResult(
        text=(getattr(result, "content", "") or "").strip(),
        lines=lines,
        words=words,
        confidence=round(sum(confidences) / len(confidences), 4) if confidences else None,
        language=detect_language(result),
        pdf_bytes=pdf_bytes,
        page_info=page_info,
    )


def detect_language(result) -> str:
    """Pick the locale that covers the most text, weighted by confidence."""
    scores: Counter[str] = Counter()
    for lang in getattr(result, "languages", None) or []:
        spans = getattr(lang, "spans", None) or []
        length = sum(getattr(s, "length", 0) or 0 for s in spans) or 1
        scores[lang.locale] += length * (lang.confidence or 0)
    return scores.most_common(1)[0][0] if scores else ""


def _mock_read_page(image_bytes: bytes) -> OcrResult:
    words = [
        {"content": token, "polygon": [0, 0, 1, 0, 1, 1, 0, 1], "confidence": 0.95}
        for token in MOCK_TEXT.split()
    ]
    lines = [{"content": line, "polygon": [0, i, 1, i, 1, i + 1, 0, i + 1]} for i, line in enumerate(MOCK_TEXT.splitlines())]
    return OcrResult(
        text=MOCK_TEXT,
        lines=lines,
        words=words,
        confidence=0.95,
        language="en",
        pdf_bytes=image_to_pdf(image_bytes),
        page_info={"width": 8.5, "height": 11, "unit": "inch", "angle": 0},
    )
