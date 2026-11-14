"""End-to-end through the HTTP layer with a fake Drive backend."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.drive.client import DriveClient, DriveFile, FileKind, FolderListing
from app.drive.errors import DriveError
from app.extraction import ocr as ocr_engine
from app.extraction import router
from app.main import create_app
from tests.conftest import build_text_pdf, drive_file

FOLDER = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"


class FakeDrive(DriveClient):
    mode = "fake"

    def __init__(self) -> None:
        self.files = [
            drive_file("report.pdf", FileKind.PDF),
            drive_file("scan.png", FileKind.IMAGE),
        ]

    async def list_folder(self, folder_id: str, resource_key: str | None = None) -> FolderListing:
        if folder_id.startswith("private"):
            raise DriveError("not_found", "This folder does not exist or is not shared publicly.")
        return FolderListing(folder_id, "Demo folder", tuple(self.files), "fake", skipped=1)

    async def download(
        self, file: DriveFile, destination: Path, *, max_bytes: int, resource_key=None
    ):
        if file.kind is FileKind.IMAGE:
            raise DriveError("upstream", "Download failed: simulated outage")
        destination.write_bytes(build_text_pdf(["Hello from the API test, with enough text."]))
        return 100


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(router.ocr_engine, "ocr_image", lambda image, language, **_: "OCR TEXT")
    monkeypatch.setattr(ocr_engine, "installed_languages", lambda: ("ara", "eng", "fra"))
    settings = Settings(google_api_key=None, allowed_hosts="testserver", _env_file=None)
    app = create_app(settings=settings, drive=FakeDrive())
    with TestClient(app) as test_client:
        yield test_client


def test_index_serves_the_single_page(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "<title>" in response.text


def test_capabilities_reports_ocr_languages(client: TestClient) -> None:
    data = client.get("/api/capabilities").json()
    assert data["ocr_languages"] == ["ara", "eng", "fra"]
    assert data["drive_mode"] == "fake"


def test_invalid_link_returns_400_with_message(client: TestClient) -> None:
    response = client.post("/api/folders", json={"url": "https://example.com/x"})
    assert response.status_code == 400
    assert response.json() == {
        "error": "That is not a Google Drive link. It should start with drive.google.com.",
        "kind": "invalid_link",
    }


def test_private_folder_returns_404(client: TestClient) -> None:
    response = client.post("/api/folders", json={"url": "private-folder-id-123"})
    assert response.status_code == 404
    assert response.json()["kind"] == "not_found"


def test_listing_then_batch_then_export(client: TestClient) -> None:
    listing = client.post(
        "/api/folders", json={"url": f"https://drive.google.com/drive/folders/{FOLDER}"}
    )
    assert listing.status_code == 200
    body = listing.json()
    assert body["folder_name"] == "Demo folder"
    assert [f["kind"] for f in body["files"]] == ["pdf", "image"]
    assert body["skipped"] == 1

    created = client.post(
        "/api/batches",
        json={
            "folder_id": FOLDER,
            "file_ids": [f["id"] for f in body["files"]],
            "ocr_language": "fra+eng",
        },
    )
    assert created.status_code == 202
    batch_id = created.json()["batch_id"]

    with client.stream("GET", f"/api/batches/{batch_id}/events") as stream:
        assert stream.headers["content-type"].startswith("text/event-stream")
        events = [json.loads(line[5:]) for line in stream.iter_lines() if line.startswith("data:")]
    assert events[0]["type"] == "snapshot"
    assert events[-1]["type"] == "batch" and events[-1]["status"] == "finished"

    export = client.get(f"/api/batches/{batch_id}/export")
    assert export.status_code == 200
    assert export.headers["content-disposition"].endswith('.json"')
    data = export.json()
    assert data["ocr_language"] == "fra+eng"
    assert data["summary"] == {"pending": 0, "processing": 0, "done": 1, "failed": 1, "total": 2}
    by_name = {f["name"]: f for f in data["files"]}
    assert by_name["report.pdf"]["method"] == "pdf-parser"
    assert "Hello from the API test" in by_name["report.pdf"]["text"]
    assert by_name["scan.png"]["status"] == "failed"
    assert "simulated outage" in by_name["scan.png"]["error"]


def test_batch_rejects_unknown_files_and_languages(client: TestClient) -> None:
    client.post("/api/folders", json={"url": FOLDER})
    unknown = client.post("/api/batches", json={"folder_id": FOLDER, "file_ids": ["nope"]})
    assert unknown.status_code == 400
    bad_lang = client.post(
        "/api/batches",
        json={"folder_id": FOLDER, "file_ids": ["id-report_pdf"], "ocr_language": "deu"},
    )
    assert bad_lang.status_code == 503
    assert "deu" in bad_lang.json()["detail"]
    not_listed = client.post(
        "/api/batches", json={"folder_id": "never-listed-1234", "file_ids": ["x"]}
    )
    assert not_listed.status_code == 400


def test_unknown_batch_is_404(client: TestClient) -> None:
    assert client.get("/api/batches/nope").status_code == 404
    assert client.get("/api/batches/nope/export").status_code == 404
    assert client.get("/api/batches/nope/events").status_code == 404
    assert isinstance(httpx.Response, type)


def test_native_pdfs_still_run_when_tesseract_is_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ocr_engine, "installed_languages", lambda: ())
    client.post("/api/folders", json={"url": FOLDER})
    response = client.post(
        "/api/batches", json={"folder_id": FOLDER, "file_ids": ["id-report_pdf"]}
    )
    assert response.status_code == 202, response.text
    batch_id = response.json()["batch_id"]
    with client.stream("GET", f"/api/batches/{batch_id}/events") as stream:
        for line in stream.iter_lines():
            if line.startswith("event: batch"):
                break
    export = client.get(f"/api/batches/{batch_id}/export").json()
    assert export["ocr_language"] is None
    assert export["files"][0]["status"] == "done"
    assert export["files"][0]["method"] == "pdf-parser"
