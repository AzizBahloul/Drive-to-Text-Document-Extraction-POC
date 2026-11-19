"""Small JPEG previews of a document's first page, shown while the file is being read.

Previews are a courtesy for the page, never a reason for a file to fail: every error is
logged and ``None`` comes back, and the extraction goes on without one.
"""

from __future__ import annotations

import io
import logging
import math
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType

import pypdfium2 as pdfium
from PIL import Image, ImageOps, UnidentifiedImageError

from app.drive.client import FileKind
from app.extraction import pdf_parser

log = logging.getLogger(__name__)

PREVIEW_WIDTH = 360
# A preview never needs more than a tall page at PREVIEW_WIDTH; this also caps what a
# hostile page size or image header can make us allocate.
MAX_PREVIEW_PIXELS = PREVIEW_WIDTH * PREVIEW_WIDTH * 3
# Images are opened only to be shrunk; refuse headers that announce absurd sizes.
MAX_SOURCE_PIXELS = 60_000_000


def make_preview(path: Path, kind: FileKind) -> bytes | None:
    """JPEG bytes of the first page (PDF) or of the image, at most PREVIEW_WIDTH wide."""
    try:
        image = _first_pdf_page(path) if kind is FileKind.PDF else _open_image(path.read_bytes())
        return encode_jpeg(image)
    except Exception as exc:  # a preview must never break the extraction
        log.info("No preview for %s: %s", path.name, exc)
        return None


def reencode_thumbnail(data: bytes) -> bytes | None:
    """Decode bytes from Drive as an image and re-encode them as a clean, small JPEG."""
    try:
        return encode_jpeg(_open_image(data))
    except Exception as exc:
        log.info("Unusable thumbnail from Drive: %s", exc)
        return None


def encode_jpeg(image: Image.Image) -> bytes:
    if image.mode != "RGB":
        background = Image.new("RGB", image.size, "white")
        rgba = image.convert("RGBA")
        background.paste(rgba, mask=rgba.getchannel("A"))
        image = background
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=78, optimize=True)  # no EXIF or other metadata
    return buffer.getvalue()


class PagePreviewer:
    """Renders any page of one PDF as a small JPEG, for the live view while it is read.

    The document stays open for the life of the extraction (its own handle, separate from
    the one being read). Every PDFium call holds ``PDFIUM_LOCK``. Use as a context manager.
    """

    def __init__(self, path: Path, width: int = PREVIEW_WIDTH) -> None:
        self._path = path
        self._width = width
        self._document: pdfium.PdfDocument | None = None
        self._context: AbstractContextManager[pdfium.PdfDocument] | None = None

    def __enter__(self) -> PagePreviewer:
        try:
            self._context = pdf_parser.open_pdf(self._path)
            self._document = self._context.__enter__()
        except Exception as exc:
            log.info("No page previews for %s: %s", self._path.name, exc)
            self._context = None
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._context is not None:
            self._context.__exit__(exc_type, exc, traceback)
        self._context = self._document = None

    def render(self, number: int) -> bytes | None:
        """JPEG of page ``number`` (1-based), or None when it cannot be rendered."""
        if self._document is None:
            return None
        try:
            with pdf_parser.PDFIUM_LOCK:
                image = _render_pdf_page(self._document, number - 1, self._width)
            return encode_jpeg(image)
        except Exception as exc:
            log.info("No preview of page %d of %s: %s", number, self._path.name, exc)
            return None


def _first_pdf_page(path: Path) -> Image.Image:
    with pdf_parser.open_pdf(path) as document, pdf_parser.PDFIUM_LOCK:
        return _render_pdf_page(document, 0, PREVIEW_WIDTH)


def _render_pdf_page(document, index: int, width: int) -> Image.Image:
    """Render one page at ``width`` pixels wide, capped at MAX_PREVIEW_PIXELS. Hold the lock."""
    if not 0 <= index < len(document):
        raise ValueError(f"the PDF has no page {index + 1}")
    page = document[index]
    try:
        page_width, page_height = page.get_size()
        scale = width / max(page_width, 1.0)
        area = max(page_width, 1.0) * max(page_height, 1.0)
        if area * scale * scale > MAX_PREVIEW_PIXELS:
            scale = math.sqrt(MAX_PREVIEW_PIXELS / area)
        bitmap = page.render(scale=scale)
        try:
            return bitmap.to_pil().convert("RGB")  # convert copies out of PDFium's buffer
        finally:
            bitmap.close()
    finally:
        page.close()


def _open_image(data: bytes) -> Image.Image:
    try:
        source = Image.open(io.BytesIO(data))
    except UnidentifiedImageError as exc:
        raise ValueError("not an image") from exc
    with source:
        width, height = source.size
        if width * height > MAX_SOURCE_PIXELS:
            raise ValueError(f"{width} x {height} is too large to preview")
        source.draft("RGB", (PREVIEW_WIDTH, PREVIEW_WIDTH * 3))  # JPEG: decode at a reduced size
        image = ImageOps.exif_transpose(source)
    image.thumbnail((PREVIEW_WIDTH, PREVIEW_WIDTH * 3))
    return image
