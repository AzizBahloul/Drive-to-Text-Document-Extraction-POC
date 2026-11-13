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
    render_dpi: int = 200
    min_chars_per_page: int = 25
    ocr_timeout_s: float = 0  # per image or per scanned page; 0 means no limit
    max_pages: int = 0  # 0 means every page
    time_limit_s: float = 0  # whole file; 0 means no limit
    ocr_workers: int = 1  # scanned pages of one PDF OCR'd in parallel


class UnsupportedFileError(ValueError):
    """The file is neither a PDF nor a supported image."""


def extract_file(path: Path, kind: FileKind, options: ExtractionOptions) -> ExtractionResult:
    if kind is FileKind.PDF:
        return extract_pdf(path, options)
    if kind is FileKind.IMAGE:
        return extract_image(path, options)
    raise UnsupportedFileError(f"Unsupported file kind: {kind}")


def extract_image(path: Path, options: ExtractionOptions) -> ExtractionResult:
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
            text = ocr_engine.ocr_image(image, options.ocr_language, timeout=options.ocr_timeout_s)
    except UnidentifiedImageError as exc:
        raise UnsupportedFileError("The file is not a readable PNG or JPEG image.") from exc
    except Image.DecompressionBombError as exc:
        raise UnsupportedFileError("The image is too large to process safely.") from exc
    page = PageResult(number=1, method=Method.OCR, text=text)
    result = ExtractionResult.from_pages([page], options.ocr_language)
    if not text:
        result.warnings.append("OCR found no text in this image.")
    return result


def extract_pdf(path: Path, options: ExtractionOptions) -> ExtractionResult:
    """Read the text layer page by page; OCR the pages that have none, in parallel.

    PDFium renders on this thread (it is not thread-safe). Tesseract runs as a separate
    process per page, so a small thread pool that only waits on those processes gives
    real parallelism. Results are put back in page order.
    """
    total = pdf_parser.page_count(path)
    deadline = time.monotonic() + options.time_limit_s if options.time_limit_s else None
    workers = max(1, options.ocr_workers)

    def run_ocr(image: Image.Image) -> tuple[str, bool]:
        try:
            text = ocr_engine.ocr_image(image, options.ocr_language, timeout=options.ocr_timeout_s)
            return text, False
        except ocr_engine.OcrTimeoutError:
            return "", True

    slots: list[tuple[int, str | Future[tuple[str, bool]]]] = []
    in_flight: deque[Future[tuple[str, bool]]] = deque()
    stopped_early = ""
    no_ocr: list[int] = []
    sources = pdf_parser.iter_page_sources(
        path,
        min_chars=options.min_chars_per_page,
        dpi=options.render_dpi,
        max_pixels=ocr_engine.MAX_PIXELS,
    )
    with (
        ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ocr") as pool,
        closing(sources),
    ):
        for number, text, image in sources:
            if image is None:
                slots.append((number, text))
            elif options.ocr_language is None:
                slots.append((number, ""))
                no_ocr.append(number)
            else:
                # Rendered pages wait in memory until OCR'd: never run far ahead of Tesseract.
                while len(in_flight) >= workers * 2:
                    in_flight.popleft().result()
                future = pool.submit(run_ocr, image)
                in_flight.append(future)
                slots.append((number, future))
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
    for number, value in slots:
        if isinstance(value, str):
            pages.append(PageResult(number=number, method=Method.PDF_PARSER, text=value))
            continue
        text, was_timed_out = value.result()
        if was_timed_out:
            timed_out.append(number)
        pages.append(PageResult(number=number, method=Method.OCR, text=text))

    result = ExtractionResult.from_pages(pages, options.ocr_language)
    if not pages:
        result.warnings.append("The PDF has no pages.")
    elif not result.text:
        result.warnings.append("No text was found on any page.")
    ocr_pages = sum(1 for page in pages if page.method is Method.OCR)
    if ocr_pages and ocr_pages < len(pages):
        result.warnings.append(
            f"{ocr_pages} of {len(pages)} page(s) had no text layer and were sent to OCR."
        )
    elif ocr_pages and len(pages) > 0:
        result.warnings.append("No text layer found; the whole document was processed with OCR.")
    if timed_out:
        listed = ", ".join(str(n) for n in timed_out[:10])
        more = "…" if len(timed_out) > 10 else ""
        result.warnings.append(
            f"OCR timed out on {len(timed_out)} page(s) ({listed}{more}); they are left empty."
        )
    if no_ocr:
        result.warnings.append(
            f"{len(no_ocr)} page(s) have no text layer and OCR is not available on the "
            "server; they are left empty."
        )
    if stopped_early:
        result.warnings.append(stopped_early)
    return result
