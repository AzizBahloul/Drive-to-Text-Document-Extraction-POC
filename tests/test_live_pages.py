"""Live reading: page-by-page progress from the extraction, published as `page` events."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.batch import manager as manager_module
from app.batch.manager import BatchManager
from app.batch.models import Batch, FileJob, FileStatus
from app.config import Settings
from app.drive.client import FileKind
from app.extraction import ocr as ocr_engine
from app.extraction import router
from app.extraction.models import Method
from app.extraction.preview import PagePreviewer
from app.extraction.router import ExtractionOptions, extract_file
from app.main import create_app
from tests.conftest import build_text_pdf, drive_file
from tests.test_api import FOLDER
from tests.test_api import FakeDrive as ApiFakeDrive

PAGE_TEXTS = [f"Page {word} of the annual report, with plenty of text." for word in "ABC"]


def _join_pdfs(sources: list[bytes]) -> bytes:
    """One PDF made of the first page of each source."""
    joined = pdfium.PdfDocument.new()
    for data in sources:
        source = pdfium.PdfDocument(data)
        joined.import_pages(source, [0])
        source.close()
    buffer = io.BytesIO()
    joined.save(buffer)
    joined.close()
    return buffer.getvalue()


def _three_page_pdf() -> bytes:
    return _join_pdfs([build_text_pdf([text]) for text in PAGE_TEXTS])


def _jpeg_width(data: bytes) -> int:
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "JPEG"
        return image.size[0]


@pytest.fixture
def fake_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router.ocr_engine, "ocr_image", lambda image, language, **_: "OCR TEXT")


# ----- extraction ----------------------------------------------------------


def test_text_layer_pages_are_reported_in_order(tmp_path: Path) -> None:
    path = tmp_path / "three.pdf"
    path.write_bytes(_three_page_pdf())
    events = []
    result = extract_file(path, FileKind.PDF, ExtractionOptions(ocr_language="eng"), events.append)
    assert [(e.number, e.total, e.method) for e in events] == [
        (1, 3, Method.PDF_PARSER),
        (2, 3, Method.PDF_PARSER),
        (3, 3, Method.PDF_PARSER),
    ]
    assert [e.text for e in events] == [page.text for page in result.pages]
    assert "Page B" in events[1].text


def test_scanned_pages_are_reported_while_ocr_reads_them_and_when_it_is_done(
    scanned_pdf: Path, fake_ocr: None
) -> None:
    events = []
    extract_file(scanned_pdf, FileKind.PDF, ExtractionOptions(ocr_language="eng"), events.append)
    assert [(e.number, e.method, e.text) for e in events] == [
        (1, Method.OCR, None),
        (1, Method.OCR, "OCR TEXT"),
    ]


def test_images_are_reported_as_one_page(text_png: Path, fake_ocr: None) -> None:
    events = []
    extract_file(text_png, FileKind.IMAGE, ExtractionOptions(ocr_language="eng"), events.append)
    assert [(e.number, e.total, e.text) for e in events] == [(1, 1, None), (1, 1, "OCR TEXT")]


def test_the_page_total_reflects_the_page_limit(tmp_path: Path) -> None:
    path = tmp_path / "three.pdf"
    path.write_bytes(_three_page_pdf())
    events = []
    options = ExtractionOptions(ocr_language="eng", max_pages=2)
    extract_file(path, FileKind.PDF, options, events.append)
    assert [(e.number, e.total) for e in events] == [(1, 2), (2, 2)]


def test_a_failing_progress_callback_never_stops_the_extraction(text_pdf: Path) -> None:
    def broken(_event) -> None:
        raise RuntimeError("display went away")

    result = extract_file(text_pdf, FileKind.PDF, ExtractionOptions(ocr_language="eng"), broken)
    assert "Quarterly report" in result.text


def test_page_previewer_renders_any_page_and_refuses_quietly(tmp_path: Path) -> None:
    path = tmp_path / "three.pdf"
    path.write_bytes(_three_page_pdf())
    with PagePreviewer(path, width=240) as pages:
        assert _jpeg_width(pages.render(3)) == 240
        assert pages.render(4) is None
        assert pages.render(0) is None
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 not a pdf")
    with PagePreviewer(broken) as pages:
        assert pages.render(1) is None


# ----- batch and API ---------------------------------------------------------


class LiveDrive(ApiFakeDrive):
    async def download(self, file, destination: Path, *, max_bytes: int, resource_key=None):
        destination.write_bytes(_three_page_pdf())
        return 300


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, fake_ocr: None):
    monkeypatch.setattr(ocr_engine, "installed_languages", lambda: ("eng",))
    settings = Settings(google_api_key=None, allowed_hosts="testserver", _env_file=None)
    with TestClient(create_app(settings=settings, drive=LiveDrive())) as test_client:
        url = f"https://drive.google.com/drive/folders/{FOLDER}"
        assert test_client.post("/api/folders", json={"url": url}).status_code == 200
        yield test_client


def _run(client: TestClient) -> tuple[str, list[dict]]:
    created = client.post("/api/batches", json={"folder_id": FOLDER, "file_ids": ["id-report_pdf"]})
    batch_id = created.json()["batch_id"]
    with client.stream("GET", f"/api/batches/{batch_id}/events") as stream:
        events = [json.loads(line[5:]) for line in stream.iter_lines() if line.startswith("data:")]
    return batch_id, events


def test_batch_streams_every_page_with_its_image_and_text(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manager_module, "FRAME_INTERVAL_S", 0)
    batch_id, events = _run(client)
    pages = [e for e in events if e["type"] == "page"]
    assert [(p["page"], p["total"], p["state"], p["has_image"]) for p in pages] == [
        (1, 3, "read", True),
        (2, 3, "read", True),
        (3, 3, "read", True),
    ]
    assert all(p["file_id"] == "id-report_pdf" and p["method"] == "pdf-parser" for p in pages)
    assert pages[1]["excerpt"].startswith("Page B of the annual report")
    assert pages[1]["chars"] == len(pages[1]["excerpt"])

    done = [e for e in events if e["type"] == "file" and e["file"]["status"] == "done"]
    assert events.index(pages[-1]) < events.index(done[0])  # pages come before the result
    assert done[0]["file"]["progress"] == {
        "page": 3,
        "total": 3,
        "method": "pdf-parser",
        "state": "read",
    }

    base = f"/api/batches/{batch_id}/files/id-report_pdf/pages"
    last = client.get(f"{base}/3/preview")
    assert last.status_code == 200 and last.headers["content-type"] == "image/jpeg"
    assert _jpeg_width(last.content) == manager_module.PAGE_PREVIEW_WIDTH
    # Once the file is finished only the last page is kept; page 1 is the first-page preview.
    assert client.get(f"{base}/2/preview").status_code == 404
    assert client.get(f"{base}/1/preview").status_code == 200
    assert client.get(f"/api/batches/{batch_id}/files/nope/pages/1/preview").status_code == 404
    assert client.get("/api/batches/nope/files/id-report_pdf/pages/1/preview").status_code == 404


def test_fast_pages_are_throttled_but_the_first_and_last_are_always_shown(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manager_module, "FRAME_INTERVAL_S", 3600)
    _, events = _run(client)
    assert [e["page"] for e in events if e["type"] == "page"] == [1, 3]


def test_a_page_reported_after_the_file_finished_is_dropped() -> None:
    async def scenario() -> None:
        settings = Settings(google_api_key=None, _env_file=None)
        manager = BatchManager(drive=None, settings=settings)  # type: ignore[arg-type]
        job = FileJob(file=drive_file("report.pdf", FileKind.PDF), status=FileStatus.DONE)
        batch = Batch(
            id="b1",
            folder_id=FOLDER,
            folder_url="",
            folder_name=None,
            ocr_language="eng",
            jobs={job.file.id: job},
        )
        queue: asyncio.Queue = asyncio.Queue()
        batch.subscribers.append(queue)
        page = {"page": 2, "total": 2, "method": "ocr", "state": "read", "excerpt": "", "chars": 0}
        manager._page_reached(batch, job, page, b"jpeg")
        assert queue.empty()
        assert job.page_previews == {} and job.progress is None

    asyncio.run(scenario())
