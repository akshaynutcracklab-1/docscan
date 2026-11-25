"""The only module that talks to the Azure Document Intelligence SDK directly.

Everything Azure goes through `analyze()` / `get_result_pdf()` so tests can patch one place.
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager

from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import HttpResponseError, ServiceRequestError, ServiceResponseError
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)

_client: DocumentIntelligenceClient | None = None
_client_lock = threading.Lock()


class AzureProcessingError(Exception):
    """Azure rejected the request (bad input, unsupported file, auth...). Not worth retrying."""


class TransientAzureError(AzureProcessingError):
    """Throttled (429), server error (5xx) or network failure. Safe to retry with backoff."""


def get_client() -> DocumentIntelligenceClient:
    """Create the client once per process and reuse its connection pool."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                if not settings.DOCINTEL_ENDPOINT or not settings.DOCINTEL_KEY:
                    raise ImproperlyConfigured(
                        "DOCINTEL_ENDPOINT and DOCINTEL_KEY must be set when OCR_ENGINE=azure."
                    )
                _client = DocumentIntelligenceClient(
                    endpoint=settings.DOCINTEL_ENDPOINT,
                    credential=AzureKeyCredential(settings.DOCINTEL_KEY),
                )
    return _client


def reset_client() -> None:
    global _client
    with _client_lock:
        _client = None


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (ServiceRequestError, ServiceResponseError)):
        return True
    if isinstance(exc, HttpResponseError):
        code = exc.status_code or 0
        return code == 429 or code >= 500
    return False


@contextmanager
def translate_errors(operation: str):
    """Convert SDK exceptions into our two error types with messages that are safe to show users."""
    try:
        yield
    except AzureProcessingError:
        raise
    except (HttpResponseError, ServiceRequestError, ServiceResponseError) as exc:
        status = getattr(exc, "status_code", None)
        reason = getattr(getattr(exc, "error", None), "message", None) or exc.__class__.__name__
        message = f"Azure Document Intelligence {operation} failed"
        message += f" (HTTP {status}): {reason}" if status else f": {reason}"
        if is_transient(exc):
            raise TransientAzureError(message) from exc
        raise AzureProcessingError(message) from exc


# ------------------------------------------------------------------ rate limiting

_PERIODS = {"s": 1, "m": 60, "h": 3600}


def parse_rate(rate: str) -> tuple[int, int]:
    """'20/m' -> (20, 60). Accepts s/m/h and long forms like 'min'."""
    count, _, period = rate.partition("/")
    return int(count), _PERIODS[(period or "m").strip().lower()[0]]


def acquire_rate_slot(rate: str | None = None) -> None:
    """Fixed-window limiter shared by every worker through the Django cache (Redis in production).

    Blocks until a slot in the current window is free.
    """
    limit, period = parse_rate(rate or settings.DOCINTEL_RATE_LIMIT)
    while True:
        now = time.time()
        window = int(now // period)
        key = f"docintel:rate:{window}"
        cache.add(key, 0, timeout=period * 2)
        try:
            count = cache.incr(key)
        except ValueError:  # key expired between add() and incr()
            cache.set(key, 1, timeout=period * 2)
            count = 1
        if count <= limit:
            return
        wait = (window + 1) * period - now + 0.05
        logger.info("Document Intelligence rate limit (%s) reached; waiting %.1fs", rate or settings.DOCINTEL_RATE_LIMIT, wait)
        time.sleep(wait)


# ------------------------------------------------------------------ calls

def analyze(model_id: str, body: bytes, **kwargs):
    """Run an analysis and wait for it. Returns (AnalyzeResult, operation_id)."""
    acquire_rate_slot()
    with translate_errors(f"analysis with {model_id}"):
        poller = get_client().begin_analyze_document(model_id, body=body, **kwargs)
        result = poller.result(timeout=settings.DOCINTEL_POLL_TIMEOUT)
        return result, poller.details["operation_id"]


def get_result_pdf(model_id: str, operation_id: str) -> bytes:
    """Download the searchable PDF produced by an analysis that requested output=[PDF]."""
    with translate_errors("PDF download"):
        return b"".join(get_client().get_analyze_result_pdf(model_id, result_id=operation_id))
