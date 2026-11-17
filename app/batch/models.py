"""In-memory state for a batch and the files it processes."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from app.drive.client import DriveFile
from app.extraction.models import ExtractionResult


class FileStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class BatchStatus(StrEnum):
    RUNNING = "running"
    FINISHED = "finished"


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class FileJob:
    file: DriveFile
    status: FileStatus = FileStatus.PENDING
    result: ExtractionResult | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    downloaded_bytes: int | None = None
    preview: bytes | None = field(default=None, repr=False)  # JPEG of page 1
    # Live reading: the page reached last, and JPEGs of the most recent pages shown.
    progress: dict | None = None
    page_previews: OrderedDict[int, bytes] = field(default_factory=OrderedDict, repr=False)

    @property
    def duration_ms(self) -> int | None:
        if self.started_at and self.finished_at:
            return int((self.finished_at - self.started_at).total_seconds() * 1000)
        return None

    def to_dict(self, *, include_text: bool = True) -> dict:
        payload = {
            **self.file.to_dict(),
            "status": self.status.value,
            "method": self.result.method.value if self.result else None,
            "ocr_language": self.result.ocr_language if self.result else None,
            "pages": (
                [page.to_dict(include_text=include_text) for page in self.result.pages]
                if self.result
                else []
            ),
            "char_count": self.result.char_count if self.result else 0,
            "warnings": list(self.result.warnings) if self.result else [],
            "error": self.error,
            "duration_ms": self.duration_ms,
            "downloaded_bytes": self.downloaded_bytes,
            "has_preview": self.preview is not None,
            "progress": dict(self.progress) if self.progress else None,
        }
        if include_text:
            payload["text"] = self.result.text if self.result else ""
        return payload


@dataclass
class Batch:
    id: str
    folder_id: str
    folder_url: str
    folder_name: str | None
    ocr_language: str | None  # None when Tesseract is not installed
    jobs: dict[str, FileJob]
    created_at: datetime = field(default_factory=_now)
    finished_at: datetime | None = None
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    task: asyncio.Task | None = None
    event_history: list[dict] = field(default_factory=list)
    seq: int = 0

    @property
    def status(self) -> BatchStatus:
        return BatchStatus.FINISHED if self.finished_at else BatchStatus.RUNNING

    def counts(self) -> dict[str, int]:
        counts = {status.value: 0 for status in FileStatus}
        for job in self.jobs.values():
            counts[job.status.value] += 1
        counts["total"] = len(self.jobs)
        return counts

    def to_dict(self, *, include_text: bool = True) -> dict:
        return {
            "batch_id": self.id,
            "status": self.status.value,
            "folder": {"id": self.folder_id, "url": self.folder_url, "name": self.folder_name},
            "ocr_language": self.ocr_language,
            "created_at": self.created_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "summary": self.counts(),
            "files": [job.to_dict(include_text=include_text) for job in self.jobs.values()],
        }
