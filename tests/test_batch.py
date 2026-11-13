"""Batch manager: one failing file never stops the others; events tell the story."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.batch.manager import BatchManager
from app.batch.models import FileStatus
from app.config import Settings
from app.drive.client import DriveFile, FileKind
from app.drive.errors import DriveError
from app.extraction import router
from tests.conftest import build_text_pdf, drive_file


class FakeDrive:
    """Serves bytes from memory; raises for ids that start with 'missing'."""

    mode = "fake"

    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.blobs = blobs

    async def download(
        self, file: DriveFile, destination: Path, *, max_bytes: int, resource_key=None
    ):
        if file.id.startswith("missing"):
            raise DriveError("not_found", "Google Drive refused the download (HTTP 404).")
        destination.write_bytes(self.blobs[file.id])
        return len(self.blobs[file.id])


@pytest.fixture
def stub_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router.ocr_engine, "ocr_image", lambda image, language, **_: "OCR TEXT")


@pytest.fixture
def manager(stub_ocr) -> BatchManager:
    blobs = {
        "id-good_pdf": build_text_pdf(["A native PDF with enough characters to count."]),
        "id-bad_pdf": b"garbage that is not a pdf",
    }
    settings = Settings(max_parallel_files=2, google_api_key=None, _env_file=None)
    return BatchManager(FakeDrive(blobs), settings)


def make_batch(manager: BatchManager, files: list[DriveFile]):
    return manager.create(
        folder_id="folder-1234567890",
        folder_url="https://drive.google.com/drive/folders/folder-1234567890",
        folder_name="Inbox",
        files=files,
        ocr_language="eng",
    )


async def test_one_failure_does_not_stop_the_batch(manager: BatchManager) -> None:
    files = [
        drive_file("good.pdf", FileKind.PDF),
        drive_file("bad.pdf", FileKind.PDF),
        drive_file("missing.png", FileKind.IMAGE, file_id="missing-1"),
    ]
    batch = make_batch(manager, files)
    await batch.task

    statuses = {job.file.name: job.status for job in batch.jobs.values()}
    assert statuses == {
        "good.pdf": FileStatus.DONE,
        "bad.pdf": FileStatus.FAILED,
        "missing.png": FileStatus.FAILED,
    }
    assert batch.status.value == "finished"
    assert batch.counts() == {"pending": 0, "processing": 0, "done": 1, "failed": 2, "total": 3}
    assert "HTTP 404" in batch.jobs["missing-1"].error
    assert batch.jobs["id-bad_pdf"].error
    good = batch.jobs["id-good_pdf"]
    assert good.result and good.result.method.value == "pdf-parser"
    assert good.duration_ms is not None and good.downloaded_bytes


async def test_event_stream_replays_snapshot_then_live_updates(manager: BatchManager) -> None:
    batch = make_batch(manager, [drive_file("good.pdf", FileKind.PDF)])
    events = []
    async for event in manager.events(batch.id):
        events.append(event)
    assert events[0]["type"] == "snapshot"
    assert events[-1]["type"] == "batch"
    assert events[-1]["status"] == "finished"
    file_events = [event["file"]["status"] for event in events if event["type"] == "file"]
    assert file_events[-1] == "done"


async def test_late_subscriber_gets_only_the_final_snapshot(manager: BatchManager) -> None:
    batch = make_batch(manager, [drive_file("good.pdf", FileKind.PDF)])
    await batch.task
    events = [event async for event in manager.events(batch.id)]
    assert [event["type"] for event in events] == ["snapshot"]
    assert events[0]["files"][0]["text"].startswith("A native PDF")


async def test_export_contains_text_and_summary(manager: BatchManager) -> None:
    batch = make_batch(manager, [drive_file("good.pdf", FileKind.PDF)])
    await batch.task
    payload = batch.to_dict(include_text=True)
    assert payload["summary"]["done"] == 1
    assert payload["files"][0]["method"] == "pdf-parser"
    assert payload["files"][0]["pages"] == [
        {"page": 1, "method": "pdf-parser", "chars": payload["files"][0]["char_count"]}
    ]
    await asyncio.sleep(0)
