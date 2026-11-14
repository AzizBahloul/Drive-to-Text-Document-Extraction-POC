"""Read the text layer of a PDF with pypdfium2 and detect pages that have none.

PDFium is not thread-safe, not even across different documents, and a batch processes
several files in worker threads. Every call into it therefore goes through
``PDFIUM_LOCK``. The lock is held for one page at a time (load, read text, render,
close) and released before the caller does anything slow with the result, such as OCR.
Without it, two PDFs in one batch could crash the whole server (segfault, heap corruption).
"""

from __future__ import annotations

import logging
import math
import re
import threading
import unicodedata
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

log = logging.getLogger(__name__)

PDFIUM_LOCK = threading.Lock()


class PdfOpenError(RuntimeError):
    """The file could not be opened as a PDF (corrupt, encrypted, or not a PDF)."""


@contextmanager
def open_pdf(path: Path) -> Iterator[pdfium.PdfDocument]:
    """Open a document; the caller must hold ``PDFIUM_LOCK`` while using it."""
    with PDFIUM_LOCK:
        try:
            document = pdfium.PdfDocument(str(path))
        except pdfium.PdfiumError as exc:
            raise PdfOpenError(f"Not a readable PDF: {exc}") from exc
    try:
        yield document
    finally:
        with PDFIUM_LOCK:
            document.close()


def page_text(page: pdfium.PdfPage) -> str:
    textpage = page.get_textpage()
    try:
        return normalise(textpage.get_text_range())
    finally:
        textpage.close()


# Control characters other than tab and newline. PDFs whose symbol fonts lack a Unicode
# map (checkbox and bullet glyphs, typically) come out as C1 codes such as U+0085; they
# carry no recoverable meaning and confuse terminals and line-based stream readers.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def normalise(text: str) -> str:
    """Unify line endings, drop control characters, and turn non-breaking spaces into spaces."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    text = _CONTROL_CHARS.sub("", text)
    return "\n".join(line.rstrip() for line in text.split("\n"))


def has_text_layer(text: str, min_chars: int) -> bool:
    """True once ``min_chars`` letters or digits are seen; stops counting there.

    Most native pages reach the threshold in the first line, so this avoids walking
    every character of every page (it was ~13% of the time on a 500-page PDF).
    """
    if min_chars <= 0:
        return True
    seen = 0
    for char in text:
        if unicodedata.category(char)[0] in "LN":
            seen += 1
            if seen >= min_chars:
                return True
    return False


def render_scale(width_pt: float, height_pt: float, dpi: int, max_pixels: int) -> float:
    """Scale for rendering at ``dpi``, reduced so the bitmap stays within ``max_pixels``.

    PDF pages can declare any size (14400 x 14400 pt is legal), and at 200 dpi that is a
    40000 x 40000 bitmap, several gigabytes from a file of a few hundred bytes.
    """
    scale = dpi / 72
    area = max(width_pt, 1.0) * max(height_pt, 1.0)
    if area * scale * scale > max_pixels:
        scale = math.sqrt(max_pixels / area)
    return scale


def render_page(page: pdfium.PdfPage, dpi: int, max_pixels: int) -> Image.Image:
    width, height = page.get_size()
    # Grayscale straight from PDFium: OCR needs nothing more, and it is a third of the memory.
    # PDFium takes a float scale; pypdfium2 only infers int from its default of 1.
    scale = render_scale(width, height, dpi, max_pixels)
    bitmap = page.render(scale=scale, grayscale=True)  # pyright: ignore[reportArgumentType]
    try:
        # to_pil() shares the bitmap's buffer for grayscale; copy so the image owns its pixels
        # before it leaves the lock and travels to an OCR thread.
        return bitmap.to_pil().copy()
    finally:
        bitmap.close()


def page_count(path: Path) -> int:
    with open_pdf(path) as document, PDFIUM_LOCK:
        return len(document)


def iter_page_sources(
    path: Path,
    *,
    min_chars: int,
    dpi: int,
    max_pixels: int = 60_000_000,
    render: bool = True,
) -> Generator[tuple[int, str, Image.Image | None], None, None]:
    """Yield ``(page_number, text, image)`` for every page.

    ``image`` is the rendered page when it has no usable text layer (and ``render`` is
    true), otherwise ``None``. Rendering happens here, on the caller's thread, because
    PDFium is not thread-safe; what happens to the image afterwards is up to the caller.
    """
    with open_pdf(path) as document:
        with PDFIUM_LOCK:
            count = len(document)
        for index in range(count):
            image: Image.Image | None = None
            with PDFIUM_LOCK:
                page = document[index]
                try:
                    text = page_text(page).strip()
                    if not has_text_layer(text, min_chars):
                        text = ""
                        if render:
                            image = render_page(page, dpi, max_pixels)
                finally:
                    page.close()
            yield index + 1, text, image  # outside the lock: the caller may take its time
