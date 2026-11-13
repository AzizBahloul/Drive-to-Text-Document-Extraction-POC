from pathlib import Path

from app.extraction import pdf_parser


def test_reads_text_layer_from_native_pdf(text_pdf: Path) -> None:
    pages = list(pdf_parser.iter_page_sources(text_pdf, min_chars=25, dpi=72))
    assert len(pages) == 1
    number, text, image = pages[0]
    assert number == 1
    assert image is None
    assert "Revenue grew by twelve percent." in text


def test_scanned_pdf_has_no_text_layer(scanned_pdf: Path) -> None:
    pages = list(pdf_parser.iter_page_sources(scanned_pdf, min_chars=25, dpi=72, render=False))
    assert pages == [(1, "", None)]


def test_scanned_page_is_rendered_in_grayscale_for_ocr(scanned_pdf: Path) -> None:
    ((number, text, image),) = pdf_parser.iter_page_sources(scanned_pdf, min_chars=25, dpi=72)
    assert (number, text) == (1, "")
    assert image is not None and image.mode == "L" and image.width > 100


def test_normalise_unifies_line_endings_and_nbsp() -> None:
    assert pdf_parser.normalise("a\r\nb\rc\xa0d  \n") == "a\nb\nc d\n"


def test_text_layer_counts_letters_and_digits_only() -> None:
    assert pdf_parser.has_text_layer("... --- \n\t" * 20, min_chars=1) is False
    assert pdf_parser.has_text_layer("abc 123 é", min_chars=7) is True
    assert pdf_parser.has_text_layer("abc 123 é", min_chars=8) is False
    assert pdf_parser.has_text_layer("short", min_chars=25) is False
    assert pdf_parser.has_text_layer("x" * 25, min_chars=25) is True


def test_corrupt_file_raises_pdf_open_error(broken_pdf: Path) -> None:
    try:
        list(pdf_parser.iter_page_sources(broken_pdf, min_chars=25, dpi=72))
    except pdf_parser.PdfOpenError:
        return
    raise AssertionError("expected PdfOpenError")


def test_several_pdfs_can_be_read_from_parallel_threads(tmp_path: Path) -> None:
    """A batch runs files in worker threads; PDFium must never be entered twice at once.

    Without ``PDFIUM_LOCK`` this crashes the interpreter (segfault / heap corruption).
    """
    from concurrent.futures import ThreadPoolExecutor

    import pypdfium2 as pdfium

    from tests.conftest import build_text_pdf

    single = tmp_path / "one.pdf"
    single.write_bytes(build_text_pdf(["Parallel page with plenty of readable text in it."]))
    source = pdfium.PdfDocument(str(single))
    paths = []
    for n in range(4):
        doc = pdfium.PdfDocument.new()
        for _ in range(150):
            doc.import_pages(source)
        path = tmp_path / f"many-{n}.pdf"
        doc.save(str(path))
        doc.close()
        paths.append(path)
    source.close()

    def read(path: Path) -> int:
        return sum(
            1
            for _, text, image in pdf_parser.iter_page_sources(path, min_chars=25, dpi=72)
            if text and image is None
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        counts = list(pool.map(read, paths * 3))
    assert counts == [150] * 12


def test_normalise_drops_control_characters_from_symbol_fonts() -> None:
    raw = "contents:\r\n\x85 Memorandum\xa0of Understanding\x00\n\x95 Schedule\tDates"
    assert pdf_parser.normalise(raw) == (
        "contents:\n Memorandum of Understanding\n Schedule\tDates"
    )
