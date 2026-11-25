"""PDF helpers: merge per-page searchable PDFs, or build image-only PDFs as a fallback."""
from __future__ import annotations

import io
from typing import Iterable

import pikepdf
from PIL import Image
from pillow_heif import register_heif_opener

register_heif_opener()

FALLBACK_DPI = 200


def merge_pdfs(pdfs: Iterable[bytes]) -> bytes:
    """Concatenate PDFs in the given order."""
    merged = pikepdf.Pdf.new()
    sources = []  # keep sources open until the merged file is saved
    for data in pdfs:
        src = pikepdf.Pdf.open(io.BytesIO(data))
        sources.append(src)
        merged.pages.extend(src.pages)
    if not len(merged.pages):
        raise ValueError("Nothing to merge: no pages.")
    out = io.BytesIO()
    merged.save(out, compress_streams=True)
    for src in sources:
        src.close()
    return out.getvalue()


def image_to_pdf(image_bytes: bytes) -> bytes:
    """Single image -> one-page (non-searchable) PDF."""
    return images_to_pdf([image_bytes])


def images_to_pdf(images: Iterable[bytes]) -> bytes:
    frames = []
    for data in images:
        with Image.open(io.BytesIO(data)) as img:
            frames.append(img.convert("RGB"))
    if not frames:
        raise ValueError("No images to convert.")
    out = io.BytesIO()
    frames[0].save(out, format="PDF", save_all=True, append_images=frames[1:], resolution=FALLBACK_DPI)
    return out.getvalue()


def page_count(pdf_bytes: bytes) -> int:
    with pikepdf.Pdf.open(io.BytesIO(pdf_bytes)) as pdf:
        return len(pdf.pages)
