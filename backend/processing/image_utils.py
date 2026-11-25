"""Image validation and normalisation for scanned pages."""
from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

register_heif_opener()

# Pillow format names we accept. MPO is what many phone cameras write for JPEGs.
ALLOWED_FORMATS = {"JPEG", "MPO", "PNG", "WEBP", "HEIF", "AVIF"}
FORMAT_EXTENSIONS = {"JPEG": "jpg", "MPO": "jpg", "PNG": "png", "WEBP": "webp", "HEIF": "heic", "AVIF": "heic"}

MAX_LONG_SIDE = 3000
MAX_OUTPUT_BYTES = 4 * 1024 * 1024 - 64 * 1024  # Azure F0 limit is 4 MB; keep a small margin
_EXIF_ORIENTATION = 0x0112
_MAX_PIXELS = 100_000_000


class InvalidImage(ValueError):
    pass


@dataclass(frozen=True)
class ImageInfo:
    format: str
    width: int
    height: int
    extension: str


def inspect_image(fileobj) -> ImageInfo:
    """Identify an upload by its content (not its extension). Rewinds the file afterwards."""
    try:
        fileobj.seek(0)
        with Image.open(fileobj) as img:
            fmt = (img.format or "").upper()
            width, height = img.size
            img.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        raise InvalidImage("The file is not a readable image.") from exc
    finally:
        fileobj.seek(0)
    if fmt not in ALLOWED_FORMATS:
        raise InvalidImage(f"Unsupported image format {fmt or 'unknown'}. Use JPEG, PNG, HEIC or WebP.")
    if width * height > _MAX_PIXELS:
        raise InvalidImage("The image resolution is too large.")
    return ImageInfo(format=fmt, width=width, height=height, extension=FORMAT_EXTENSIONS[fmt])


@dataclass(frozen=True)
class PreparedImage:
    data: bytes
    width: int
    height: int
    changed: bool

    @property
    def size(self) -> int:
        return len(self.data)


def prepare_image(data: bytes, rotation: int = 0) -> PreparedImage:
    """EXIF-orient, rotate (clockwise), convert to RGB JPEG, cap the long side and compress < 4 MB.

    Returns the original bytes untouched when the image is already a compliant JPEG,
    so running the step twice (retry / reprocess) does not degrade quality.
    """
    with Image.open(io.BytesIO(data)) as img:
        img.load()
        fmt = (img.format or "").upper()
        orientation = img.getexif().get(_EXIF_ORIENTATION, 1)
        needs_work = (
            fmt not in {"JPEG", "MPO"}
            or rotation % 360
            or orientation not in (0, 1)
            or max(img.size) > MAX_LONG_SIDE
            or len(data) > MAX_OUTPUT_BYTES
            or img.mode not in ("RGB", "L")
        )
        if not needs_work:
            return PreparedImage(data=data, width=img.width, height=img.height, changed=False)

        out = ImageOps.exif_transpose(img)
        if rotation % 360:
            out = out.rotate(-(rotation % 360), expand=True)  # PIL rotates counter-clockwise
        out = _to_rgb(out)
        if max(out.size) > MAX_LONG_SIDE:
            out.thumbnail((MAX_LONG_SIDE, MAX_LONG_SIDE), Image.Resampling.LANCZOS)
        encoded = compress_jpeg(out)
        return PreparedImage(data=encoded, width=out.width, height=out.height, changed=True)


def _to_rgb(img: Image.Image) -> Image.Image:
    if img.mode in ("RGB", "L"):
        return img
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    return img.convert("RGB")


def compress_jpeg(img: Image.Image, max_bytes: int = MAX_OUTPUT_BYTES) -> bytes:
    """Lower JPEG quality, then dimensions, until the image fits in `max_bytes`."""
    current = img
    while True:
        for quality in (90, 85, 80, 72, 65, 55):
            buf = io.BytesIO()
            current.save(buf, format="JPEG", quality=quality, optimize=True)
            if buf.tell() <= max_bytes:
                return buf.getvalue()
        if max(current.size) < 600:
            return buf.getvalue()
        current = current.resize((int(current.width * 0.8), int(current.height * 0.8)), Image.Resampling.LANCZOS)
