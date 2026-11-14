"""Real page images: Drive thumbnails at listing time, rendered first pages while reading."""

from __future__ import annotations

import io
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.drive.client import DriveClient, FileKind
from app.drive.errors import DriveError
from app.extraction import ocr as ocr_engine
from app.extraction import router
from app.extraction.preview import (
    MAX_PREVIEW_PIXELS,
    PREVIEW_WIDTH,
    make_preview,
    reencode_thumbnail,
)
from app.main import create_app
from tests.conftest import build_text_image, build_text_pdf
from tests.test_api import FOLDER
from tests.test_api import FakeDrive as ApiFakeDrive


def _png(size: tuple[int, int] = (300, 400)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, "PNG")
    return buffer.getvalue()


def _jpeg_size(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "JPEG"
        return image.size


# ----- previews rendered from the downloaded file ---------------------------------------


def test_pdf_preview_is_the_first_page_at_preview_width(tmp_path: Path) -> None:
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(build_text_pdf(["First page of the document."]))
    width, height = _jpeg_size(make_preview(pdf, FileKind.PDF))
    assert width == PREVIEW_WIDTH
    assert height > width  # a portrait letter page


def test_a_huge_declared_page_is_previewed_within_the_pixel_budget(tmp_path: Path) -> None:
    pdf = tmp_path / "huge.pdf"
    pdf.write_bytes(build_text_pdf(["Tiny file, enormous page."], media_box=(14400, 14400)))
    width, height = _jpeg_size(make_preview(pdf, FileKind.PDF))
    assert width * height <= MAX_PREVIEW_PIXELS


def test_image_preview_is_shrunk(tmp_path: Path) -> None:
    png = tmp_path / "scan.png"
    build_text_image("Scanned words").save(png)
    width, _ = _jpeg_size(make_preview(png, FileKind.IMAGE))
    assert width == PREVIEW_WIDTH


def test_a_broken_file_has_no_preview_instead_of_an_error(tmp_path: Path) -> None:
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 this is not really a pdf")
    assert make_preview(broken, FileKind.PDF) is None
    assert make_preview(broken, FileKind.IMAGE) is None


def test_thumbnails_from_drive_are_reencoded_or_refused() -> None:
    assert _jpeg_size(reencode_thumbnail(_png((320, 414)))) == (320, 414)
    assert reencode_thumbnail(b"<html>not an image</html>") is None


# ----- the Drive thumbnail fetch ---------------------------------------------------------


def _thumb_client(handler) -> DriveClient:
    return DriveClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), api_key=None)


async def test_drive_thumbnail_returns_image_bytes_and_passes_the_resource_key() -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, content=_png(), headers={"content-type": "image/png"})

    data = await _thumb_client(handler).thumbnail("file-1", "rk-9")
    assert data == _png()
    assert seen[0].params["id"] == "file-1" and seen[0].params["resourcekey"] == "rk-9"


@pytest.mark.parametrize(
    ("status", "content_type", "body"),
    [
        (200, "text/html; charset=utf-8", b"<html>sign in</html>"),
        (404, "image/png", b""),
        (200, "image/png", b"x" * (2 * 1024 * 1024 + 1)),
    ],
)
async def test_drive_thumbnail_refuses_pages_errors_and_oversized_bodies(
    status: int, content_type: str, body: bytes
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers={"content-type": content_type})

    with pytest.raises(DriveError):
        await _thumb_client(handler).thumbnail("file-1")


# ----- the HTTP endpoints ---------------------------------------------------------------


class ThumbDrive(ApiFakeDrive):
    def __init__(self) -> None:
        super().__init__()
        self.thumbnail_calls: list[str] = []
        self.offline = False

    async def thumbnail(self, file_id: str, resource_key: str | None = None) -> bytes:
        self.thumbnail_calls.append(file_id)
        if file_id == "id-scan_png":
            raise DriveError("not_found", "Drive has no thumbnail for this file.")
        if self.offline:
            raise DriveError("upstream", "Could not reach Google Drive: connection dropped")
        return _png()


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(router.ocr_engine, "ocr_image", lambda image, language, **_: "OCR TEXT")
    monkeypatch.setattr(ocr_engine, "installed_languages", lambda: ("eng",))
    drive = ThumbDrive()
    settings = Settings(google_api_key=None, allowed_hosts="testserver", _env_file=None)
    with TestClient(create_app(settings=settings, drive=drive)) as client:
        yield client, drive


def _list(client: TestClient) -> None:
    url = f"https://drive.google.com/drive/folders/{FOLDER}"
    assert client.post("/api/folders", json={"url": url}).status_code == 200


def test_thumbnail_is_served_only_for_files_of_a_listed_folder(setup) -> None:
    client, drive = setup
    path = f"/api/folders/{FOLDER}/files/id-report_pdf/thumbnail"
    assert client.get(path).status_code == 404  # folder not listed yet
    assert drive.thumbnail_calls == []
    _list(client)
    assert client.get(f"/api/folders/{FOLDER}/files/some-other-id/thumbnail").status_code == 404
    assert drive.thumbnail_calls == []  # never asked Drive about a file outside the listing

    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    _jpeg_size(response.content)
    assert client.get(path).status_code == 200
    assert drive.thumbnail_calls == ["id-report_pdf"]  # the second request came from the cache


def test_missing_thumbnail_is_a_404_and_is_not_retried(setup) -> None:
    client, drive = setup
    _list(client)
    path = f"/api/folders/{FOLDER}/files/id-scan_png/thumbnail"
    assert client.get(path).status_code == 404
    assert client.get(path).status_code == 404
    assert drive.thumbnail_calls == ["id-scan_png"]


def test_batch_publishes_and_serves_the_rendered_first_page(setup) -> None:
    client, _ = setup
    _list(client)
    created = client.post(
        "/api/batches", json={"folder_id": FOLDER, "file_ids": ["id-report_pdf", "id-scan_png"]}
    )
    batch_id = created.json()["batch_id"]
    with client.stream("GET", f"/api/batches/{batch_id}/events") as stream:
        events = [json.loads(line[5:]) for line in stream.iter_lines() if line.startswith("data:")]

    report = [
        e["file"] for e in events if e["type"] == "file" and e["file"]["id"] == "id-report_pdf"
    ]
    # The page is announced while the file is still being read, then the file finishes.
    # (The first "processing" event may predate the subscription and come in the snapshot.)
    states = [(f["status"], f["has_preview"]) for f in report]
    assert states[-2:] == [("processing", True), ("done", True)]
    assert report[-2]["downloaded_bytes"] == 100

    preview = client.get(f"/api/batches/{batch_id}/files/id-report_pdf/preview")
    assert preview.status_code == 200 and preview.headers["content-type"] == "image/jpeg"
    assert _jpeg_size(preview.content)[0] == PREVIEW_WIDTH
    # The image's download failed in the fake, so it never got a preview.
    assert client.get(f"/api/batches/{batch_id}/files/id-scan_png/preview").status_code == 404
    assert client.get(f"/api/batches/{batch_id}/files/nope/preview").status_code == 404
    assert client.get("/api/batches/nope/files/id-report_pdf/preview").status_code == 404


def test_a_dropped_connection_is_not_remembered_as_no_thumbnail(setup) -> None:
    client, drive = setup
    _list(client)
    path = f"/api/folders/{FOLDER}/files/id-report_pdf/thumbnail"
    drive.offline = True
    assert client.get(path).status_code == 404
    drive.offline = False
    assert client.get(path).status_code == 200
    assert drive.thumbnail_calls == ["id-report_pdf", "id-report_pdf"]
