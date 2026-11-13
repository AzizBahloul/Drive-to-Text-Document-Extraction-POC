"""Create batches, run them in the background, and publish per-file progress."""

from __future__ import annotations

import asyncio
import logging
import secrets
import tempfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytesseract

from app.batch.models import Batch, FileJob, FileStatus
from app.config import Settings
from app.drive.client import DriveClient, DriveError, DriveFile
from app.extraction import ocr as ocr_engine
from app.extraction.models import ExtractionResult
from app.extraction.pdf_parser import PdfOpenError
from app.extraction.router import ExtractionOptions, UnsupportedFileError, extract_file

log = logging.getLogger(__name__)

Event = dict


class BatchNotFoundError(KeyError):
    pass


class TooManyBatchesError(RuntimeError):
    """Refuse a new batch while too many are still running."""


class BatchManager:
    def __init__(self, drive: DriveClient, settings: Settings) -> None:
        self._drive = drive
        self._settings = settings
        self._batches: dict[str, Batch] = {}
        self._semaphore = asyncio.Semaphore(settings.max_parallel_files)

    # ----- lifecycle -------------------------------------------------------

    def create(
        self,
        *,
        folder_id: str,
        folder_url: str,
        folder_name: str | None,
        files: list[DriveFile],
        ocr_language: str | None,
        resource_key: str | None = None,
    ) -> Batch:
        running = sum(1 for batch in self._batches.values() if batch.finished_at is None)
        if running >= self._settings.max_running_batches:
            raise TooManyBatchesError(
                f"{running} batches are already running. Wait for one to finish and try again."
            )
        self._prune()
        batch = Batch(
            id=secrets.token_urlsafe(9),
            folder_id=folder_id,
            folder_url=folder_url,
            folder_name=folder_name,
            ocr_language=ocr_language,
            jobs={file.id: FileJob(file=file) for file in files},
        )
        self._batches[batch.id] = batch
        batch.task = asyncio.create_task(self._run(batch, resource_key), name=f"batch-{batch.id}")
        return batch

    def _prune(self) -> None:
        """Forget the oldest finished batches so memory stays bounded."""
        finished = [batch for batch in self._batches.values() if batch.finished_at is not None]
        excess = len(finished) - self._settings.batch_retention + 1
        for batch in sorted(finished, key=lambda b: b.finished_at)[: max(excess, 0)]:
            del self._batches[batch.id]

    def get(self, batch_id: str) -> Batch:
        try:
            return self._batches[batch_id]
        except KeyError as exc:
            raise BatchNotFoundError(batch_id) from exc

    async def _run(self, batch: Batch, resource_key: str | None) -> None:
        try:
            await asyncio.gather(
                *(self._process(batch, job, resource_key) for job in batch.jobs.values()),
                return_exceptions=True,
            )
        finally:
            batch.finished_at = datetime.now(UTC)
            self._publish(batch, {"type": "batch", **batch.to_dict(include_text=False)})
            log.info("Batch %s finished: %s", batch.id, batch.counts())

    async def _process(self, batch: Batch, job: FileJob, resource_key: str | None) -> None:
        async with self._semaphore:
            job.status = FileStatus.PROCESSING
            job.started_at = datetime.now(UTC)
            self._publish(batch, {"type": "file", "file": job.to_dict(include_text=False)})
            try:
                job.result = await self._download_and_extract(batch, job, resource_key)
                job.status = FileStatus.DONE
            except DriveError as exc:
                job.status = FileStatus.FAILED
                job.error = exc.message
            except Exception as exc:  # one broken file must never stop the batch
                log.exception("Extraction failed for %s", job.file.name)
                job.status = FileStatus.FAILED
                job.error = _describe(exc)
            finally:
                job.finished_at = datetime.now(UTC)
                self._publish(batch, {"type": "file", "file": job.to_dict(include_text=True)})

    async def _download_and_extract(
        self, batch: Batch, job: FileJob, resource_key: str | None
    ) -> ExtractionResult:
        suffix = Path(job.file.name).suffix or (".pdf" if job.file.kind.value == "pdf" else ".img")
        with tempfile.TemporaryDirectory(prefix="drive-to-text-") as workdir:
            target = Path(workdir) / f"{job.file.id}{suffix}"
            job.downloaded_bytes = await self._drive.download(
                job.file,
                target,
                max_bytes=self._settings.max_file_size_bytes,
                resource_key=resource_key,
            )
            options = ExtractionOptions(
                ocr_language=batch.ocr_language,
                render_dpi=self._settings.ocr_render_dpi,
                min_chars_per_page=self._settings.pdf_text_min_chars_per_page,
                ocr_timeout_s=self._settings.ocr_timeout_seconds,
                ocr_workers=self._settings.effective_ocr_page_workers,
                max_pages=self._settings.max_pdf_pages,
                time_limit_s=self._settings.file_time_limit_seconds,
            )
            # Extraction is CPU-bound (PDF rendering, Tesseract); keep the event loop free.
            return await asyncio.to_thread(extract_file, target, job.file.kind, options)

    # ----- events ----------------------------------------------------------

    def _publish(self, batch: Batch, event: Event) -> None:
        for queue in list(batch.subscribers):
            queue.put_nowait(event)

    async def events(self, batch_id: str) -> AsyncIterator[Event]:
        """Yield a snapshot, then live updates until the batch is finished."""
        batch = self.get(batch_id)
        queue: asyncio.Queue[Event] = asyncio.Queue()
        batch.subscribers.append(queue)
        try:
            yield {"type": "snapshot", **batch.to_dict(include_text=True)}
            if batch.finished_at:
                return
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield {"type": "ping"}
                    continue
                yield event
                if event.get("type") == "batch":
                    return
        finally:
            batch.subscribers.remove(queue)


_USER_FACING = (
    UnsupportedFileError,
    PdfOpenError,
    ocr_engine.OcrUnavailableError,
    ocr_engine.OcrTimeoutError,
)


def _describe(exc: Exception) -> str:
    """A message safe to show in the page: known failures verbatim, the rest generic.

    Unexpected exceptions can carry temp paths or library internals; those stay in the
    server log (``log.exception`` above) instead of the API response.
    """
    if isinstance(exc, _USER_FACING):
        return str(exc).strip() or type(exc).__name__
    if isinstance(exc, pytesseract.TesseractError):
        return "Tesseract could not read this file."
    return "Unexpected error while extracting this file. The server log has the details."
