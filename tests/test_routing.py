"""Routing decisions: which pipeline handles which file, without running Tesseract."""

from pathlib import Path

import pytest

from app.drive.client import FileKind, classify
from app.extraction import router
from app.extraction.models import Method
from app.extraction.router import ExtractionOptions, UnsupportedFileError, extract_file

OPTIONS = ExtractionOptions(ocr_language="eng", render_dpi=100, min_chars_per_page=25)


@pytest.mark.parametrize(
    ("name", "mime", "expected"),
    [
        ("report.pdf", "application/pdf", FileKind.PDF),
        ("scan.PNG", "image/png", FileKind.IMAGE),
        ("photo.jpg", "image/jpeg", FileKind.IMAGE),
        ("photo.jpeg", "image/jpeg", FileKind.IMAGE),
        ("weird-mime.pdf", "application/octet-stream", FileKind.PDF),
        ("no-mime.JPG", None, FileKind.IMAGE),
        ("sheet.xlsx", "application/vnd.ms-excel", None),
        ("notes.txt", "text/plain", None),
        ("drawing.gif", "image/gif", None),
        ("subfolder", "application/vnd.google-apps.folder", None),
    ],
)
def test_classify_keeps_only_pdfs_and_images(name, mime, expected) -> None:
    assert classify(name, mime) is expected


@pytest.fixture
def fake_ocr(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace Tesseract with a stub that records every call."""
    calls: list[str] = []

    def stub(image, language, *, psm=3, timeout=0):
        calls.append(language)
        return "OCR TEXT"

    monkeypatch.setattr(router.ocr_engine, "ocr_image", stub)
    return calls


def test_native_pdf_goes_through_the_parser_only(text_pdf: Path, fake_ocr: list[str]) -> None:
    result = extract_file(text_pdf, FileKind.PDF, OPTIONS)
    assert result.method is Method.PDF_PARSER
    assert "Quarterly report 2026" in result.text
    assert result.ocr_language is None
    assert fake_ocr == []


def test_scanned_pdf_is_detected_and_sent_to_ocr(scanned_pdf: Path, fake_ocr: list[str]) -> None:
    result = extract_file(scanned_pdf, FileKind.PDF, OPTIONS)
    assert result.method is Method.OCR
    assert result.text == "OCR TEXT"
    assert result.ocr_language == "eng"
    assert fake_ocr == ["eng"]
    assert any("No text layer" in warning for warning in result.warnings)


def test_image_goes_straight_to_ocr(text_png: Path, fake_ocr: list[str]) -> None:
    result = extract_file(text_png, FileKind.IMAGE, OPTIONS)
    assert result.method is Method.OCR
    assert result.pages[0].number == 1
    assert fake_ocr == ["eng"]


def test_broken_pdf_raises_a_readable_error(broken_pdf: Path, fake_ocr: list[str]) -> None:
    with pytest.raises(Exception) as info:
        extract_file(broken_pdf, FileKind.PDF, OPTIONS)
    assert "PDF" in str(info.value)


def test_non_image_bytes_with_image_kind_are_rejected(tmp_path: Path, fake_ocr: list[str]) -> None:
    bogus = tmp_path / "x.png"
    bogus.write_bytes(b"not an image")
    with pytest.raises(UnsupportedFileError):
        extract_file(bogus, FileKind.IMAGE, OPTIONS)


def test_scanned_pages_are_ocred_in_parallel_and_kept_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    import time

    from tests.conftest import build_text_image

    path = tmp_path / "six-pages.pdf"
    # Each page gets its own width, so the stub can tell which page it was given.
    images = [build_text_image(f"PAGE {n}", size=(400 + 10 * n, 300)) for n in range(6)]
    images[0].save(path, "PDF", resolution=72, save_all=True, append_images=images[1:])

    lock = threading.Lock()
    active = peak = calls = 0

    def slow_stub(image, language, **_):
        nonlocal active, peak, calls
        with lock:
            calls += 1
            order = calls
            active += 1
            peak = max(peak, active)
        time.sleep(0.05 if order % 2 else 0.01)  # finish out of order on purpose
        with lock:
            active -= 1
        return f"width {image.width}"

    monkeypatch.setattr(router.ocr_engine, "ocr_image", slow_stub)
    options = ExtractionOptions(ocr_language="eng", render_dpi=72, ocr_workers=3)
    result = extract_file(path, FileKind.PDF, options)

    assert [page.number for page in result.pages] == [1, 2, 3, 4, 5, 6]
    assert [page.text for page in result.pages] == [f"width {400 + 10 * n}" for n in range(6)]
    assert 1 < peak <= 3


def test_without_ocr_an_image_fails_with_a_clear_reason(text_png: Path) -> None:
    from app.extraction import ocr

    options = ExtractionOptions(ocr_language=None)
    with pytest.raises(ocr.OcrUnavailableError, match="Tesseract is not installed"):
        extract_file(text_png, FileKind.IMAGE, options)


def test_without_ocr_scanned_pages_are_left_empty_with_a_warning(scanned_pdf: Path) -> None:
    result = extract_file(scanned_pdf, FileKind.PDF, ExtractionOptions(ocr_language=None))
    assert result.pages[0].text == ""
    assert any("OCR is not available" in warning for warning in result.warnings)
