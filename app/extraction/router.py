"""Route a downloaded file to the right pipeline and collect the result.

PDF   -> text layer per page; pages without one are rendered and OCR'd
image -> OCR

Every file is processed within explicit limits (pixels, pages, seconds), because the
files come from a folder the user does not control and may be hostile or simply huge.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.drive.client import FileKind
from app.extraction import ocr as ocr_engine
from app.extraction import pdf_parser
from app.extraction.models import ExtractionResult, Method, PageResult

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExtractionOptions:
    ocr_language: str | None  # None when Tesseract is not installed
    render_dpi: int = 300
    min_chars_per_page: int = 25
    ocr_timeout_s: float = 0  # per image or per scanned page; 0 means no limit
    max_pages: int = 0  # 0 means every page
    time_limit_s: float = 0  # whole file; 0 means no limit
    ocr_workers: int = 1  # scanned pages of one PDF OCR'd in parallel


class UnsupportedFileError(ValueError):
    """The file is neither a PDF nor a supported image."""


@dataclass(frozen=True)
class PageEvent:
    """One page reached: ``text`` is None while OCR is still reading it."""

    number: int
    total: int
    method: Method
    text: str | None


PageCallback = Callable[[PageEvent], None]


def _report(on_page: PageCallback | None, event: PageEvent) -> None:
    """Progress is for display only; a failing callback must never stop the extraction."""
    if on_page is None:
        return
    try:
        on_page(event)
    except Exception:
        log.exception("Page progress callback failed")


def extract_file(
    path: Path,
    kind: FileKind,
    options: ExtractionOptions,
    on_page: PageCallback | None = None,
) -> ExtractionResult:
    """Extract one file. ``on_page`` is called as pages are read, from worker threads."""
    if kind is FileKind.PDF:
        return extract_pdf(path, options, on_page)
    if kind is FileKind.IMAGE:
        return extract_image(path, options, on_page)
    raise UnsupportedFileError(f"Unsupported file kind: {kind}")


def extract_image(
    path: Path, options: ExtractionOptions, on_page: PageCallback | None = None
) -> ExtractionResult:
    if options.ocr_language is None:
        raise ocr_engine.OcrUnavailableError(
            "Tesseract is not installed on the server, so images cannot be read."
        )
    try:
        with Image.open(path) as image:
            width, height = image.size
            if width * height > ocr_engine.MAX_PIXELS:
                raise UnsupportedFileError(
                    f"The image is {width} x {height} pixels; the limit is "
                    f"{ocr_engine.MAX_PIXELS // 1_000_000} megapixels."
                )
            image.load()
            _report(on_page, PageEvent(1, 1, Method.OCR, None))
            text = ocr_engine.ocr_image(image, options.ocr_language, timeout=options.ocr_timeout_s)
    except UnidentifiedImageError as exc:
        raise UnsupportedFileError("The file is not a readable PNG or JPEG image.") from exc
    except Image.DecompressionBombError as exc:
        raise UnsupportedFileError("The image is too large to process safely.") from exc
    _report(on_page, PageEvent(1, 1, Method.OCR, text))
    page = PageResult(number=1, method=Method.OCR, text=text)
    result = ExtractionResult.from_pages([page], options.ocr_language)
    if not text:
        result.warnings.append("OCR found no text in this image.")
    return result


def extract_pdf(
    path: Path, options: ExtractionOptions, on_page: PageCallback | None = None
) -> ExtractionResult:
    """Read the text layer page by page; OCR the pages that have none, in parallel.

    PDFium renders on this thread (it is not thread-safe). Tesseract runs as a separate
    process per page, so a small thread pool that only waits on those processes gives
    real parallelism. Results are put back in page order.
    """
    total = pdf_parser.page_count(path)
    shown_total = min(total, options.max_pages) if options.max_pages else total
    deadline = time.monotonic() + options.time_limit_s if options.time_limit_s else None
    workers = max(1, options.ocr_workers)

    def run_ocr(number: int, image: Image.Image, native: str) -> tuple[PageResult, bool]:
        language = options.ocr_language
        assert language is not None
        timed_out = False
        try:
            text = ocr_engine.ocr_image(image, language, timeout=options.ocr_timeout_s)
        except ocr_engine.OcrTimeoutError:
            text = ""
            timed_out = True
        method = Method.PDF_PARSER if native and not text.strip() else Method.OCR
        page = PageResult(number, method, native if method is Method.PDF_PARSER else text)
        _report(on_page, PageEvent(number, shown_total, page.method, page.text))
        return page, timed_out

    slots: list[PageResult | Future[tuple[PageResult, bool]]] = []
    in_flight: deque[Future[tuple[PageResult, bool]]] = deque()
    stopped_early = ""
    no_ocr: list[int] = []
    sources = pdf_parser.iter_page_sources(
        path,
        min_chars=options.min_chars_per_page,
        dpi=options.render_dpi,
        max_pixels=ocr_engine.MAX_PIXELS,
        render=options.ocr_language is not None,
    )
    with (
        ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ocr") as pool,
        closing(sources),
    ):
        for number, text, image in sources:
            if image is None:
                if options.ocr_language is None and not pdf_parser.has_text_layer(
                    text, options.min_chars_per_page
                ):
                    no_ocr.append(number)
                slots.append(PageResult(number, Method.PDF_PARSER, text))
                _report(on_page, PageEvent(number, shown_total, Method.PDF_PARSER, text))
            else:
                # Rendered pages wait in memory until OCR'd: never run far ahead of Tesseract.
                while len(in_flight) >= workers * 2:
                    in_flight.popleft().result()
                _report(on_page, PageEvent(number, shown_total, Method.OCR, None))
                future = pool.submit(run_ocr, number, image, text)
                in_flight.append(future)
                slots.append(future)
            if number >= total:
                break
            if options.max_pages and len(slots) >= options.max_pages:
                stopped_early = (
                    f"Only the first {len(slots)} of {total} pages were processed "
                    f"(MAX_PDF_PAGES={options.max_pages})."
                )
                break
            if deadline is not None and time.monotonic() > deadline:
                stopped_early = (
                    f"Stopped after {len(slots)} of {total} pages: the file exceeded the "
                    f"{options.time_limit_s:g} s time limit."
                )
                break

    pages: list[PageResult] = []
    timed_out: list[int] = []
    for slot in slots:
        if isinstance(slot, PageResult):
            pages.append(slot)
        else:
            page, was_timed_out = slot.result()
            pages.append(page)
            if was_timed_out:
                timed_out.append(page.number)

    result = ExtractionResult.from_pages(pages, options.ocr_language)
    if not pages:
        result.warnings.append("The PDF has no pages.")
    elif not result.text:
        result.warnings.append("No text was found on any page.")
    ocr_pages = sum(1 for page in pages if page.method is Method.OCR)
    if ocr_pages and ocr_pages < len(pages):
        result.warnings.append(f"{ocr_pages} of {len(pages)} page(s) were read with OCR.")
    elif ocr_pages and len(pages) > 0:
        result.warnings.append(
            "No text layer met the minimum length. The whole document was read with OCR."
        )
    if timed_out:
        listed = ", ".join(str(n) for n in timed_out[:10])
        more = "…" if len(timed_out) > 10 else ""
        result.warnings.append(
            f"OCR timed out on {len(timed_out)} page(s) ({listed}{more}). "
            "Available PDF text was kept."
        )
    if no_ocr:
        result.warnings.append(
            f"{len(no_ocr)} page(s) fell below the text threshold and OCR is not available. "
            "Available PDF text was kept."
        )
    if stopped_early:
        result.warnings.append(stopped_early)
    return result
