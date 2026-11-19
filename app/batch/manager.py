"""Create batches, run them in the background, and publish per-file progress."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
import tempfile
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytesseract

from app.batch.models import Batch, FileJob, FileStatus
from app.config import Settings
from app.drive.client import DriveClient, DriveError, DriveFile, FileKind
from app.extraction import ocr as ocr_engine
from app.extraction.models import ExtractionResult, Method
from app.extraction.pdf_parser import PdfOpenError
from app.extraction.preview import PagePreviewer, make_preview
from app.extraction.router import (
    ExtractionOptions,
    PageEvent,
    UnsupportedFileError,
    extract_file,
)

log = logging.getLogger(__name__)

Event = dict

# Live reading view. Text-layer pages can go by at hundreds a second, and scanned pages are
# queued for OCR several at a time, so at most one frame (a rendered page) is made per
# FRAME_INTERVAL_S. The page events themselves are all sent.
FRAME_INTERVAL_S = 0.2
PAGE_PREVIEW_WIDTH = 240
MAX_PAGE_PREVIEWS = 12  # ~15 KB each, trimmed to one when the file is finished
EXCERPT_CHARS = 280


class BatchNotFoundError(KeyError):
    pass


class TooManyBatchesError(RuntimeError):
    """Refuse a new batch while too many are still running."""


class BatchManager:
    def __init__(self, drive: DriveClient, settings: Settings) -> None:
        self._drive = drive
        self._settings = settings
        self._batches: dict[str, Batch] = {}
        self._downloads = asyncio.Semaphore(settings.max_parallel_downloads)
        self._extractions = asyncio.Semaphore(settings.max_parallel_files)
        # Bound the temporary files waiting for a free extraction worker.
        self._in_flight = asyncio.Semaphore(
            settings.max_parallel_files + settings.max_parallel_downloads
        )

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
        drive: DriveClient | None = None,
        owner: str | None = None,
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
            owner=owner,
            jobs={file.id: FileJob(file=file) for file in files},
        )
        self._batches[batch.id] = batch
        batch.task = asyncio.create_task(
            self._run(batch, resource_key, drive or self._drive), name=f"batch-{batch.id}"
        )
        return batch

    def _prune(self) -> None:
        """Forget the oldest finished batches so memory stays bounded."""
        finished = [(b.finished_at, b.id) for b in self._batches.values() if b.finished_at]
        excess = len(finished) - self._settings.batch_retention + 1
        for _, batch_id in sorted(finished)[: max(excess, 0)]:
            del self._batches[batch_id]

    def get(self, batch_id: str) -> Batch:
        try:
            return self._batches[batch_id]
        except KeyError as exc:
            raise BatchNotFoundError(batch_id) from exc

    def discard_private_batches(self, owner: str) -> None:
        """Stop a disconnected browser's active work and drop its retained results."""
        for batch_id, batch in list(self._batches.items()):
            if batch.owner == owner:
                if batch.finished_at is None:
                    self.cancel(batch_id)
                del self._batches[batch_id]

    def cancel(self, batch_id: str) -> Batch:
        """Cancel an in-flight batch and mark unfinished jobs as cancelled."""
        batch = self.get(batch_id)
        if batch.finished_at is not None:
            return batch
        if batch.task and not batch.task.done():
            batch.task.cancel()
        now = datetime.now(UTC)
        for job in batch.jobs.values():
            if job.status in {FileStatus.PENDING, FileStatus.PROCESSING}:
                job.status = FileStatus.FAILED
                job.error = "Extraction was cancelled."
                job.finished_at = now
        batch.finished_at = now
        self._publish(batch, {"type": "batch", **batch.to_dict(include_text=False)})
        log.info("Batch %s cancelled by user", batch.id)
        return batch

    async def _run(self, batch: Batch, resource_key: str | None, drive: DriveClient) -> None:
        try:
            await asyncio.gather(
                *(self._process(batch, job, resource_key, drive) for job in batch.jobs.values()),
                return_exceptions=True,
            )
        except asyncio.CancelledError:
            log.info("Batch %s task cancelled", batch.id)
        finally:
            if batch.finished_at is None:
                batch.finished_at = datetime.now(UTC)
            self._publish(batch, {"type": "batch", **batch.to_dict(include_text=False)})
            log.info("Batch %s finished: %s", batch.id, batch.counts())

    async def _process(
        self, batch: Batch, job: FileJob, resource_key: str | None, drive: DriveClient
    ) -> None:
        async with self._in_flight:
            job.status = FileStatus.PROCESSING
            job.started_at = datetime.now(UTC)
            self._publish(batch, {"type": "file", "file": job.to_dict(include_text=False)})
            try:
                job.result = await self._download_and_extract(batch, job, resource_key, drive)
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
                while len(job.page_previews) > 1:  # keep only the last page shown
                    job.page_previews.popitem(last=False)
                self._publish(batch, {"type": "file", "file": job.to_dict(include_text=True)})

    async def _download_and_extract(
        self, batch: Batch, job: FileJob, resource_key: str | None, drive: DriveClient
    ) -> ExtractionResult:
        raw_suffix = Path(job.file.name).suffix.lower()
        suffix = (
            raw_suffix
            if re.match(r"^\.[a-z0-9]{1,10}$", raw_suffix)
            else (".pdf" if job.file.kind.value == "pdf" else ".img")
        )
        with tempfile.TemporaryDirectory(prefix="drive-to-text-") as workdir:
            target = Path(workdir) / f"{job.file.id}{suffix}"
            async with self._downloads:
                job.downloaded_bytes = await drive.download(
                    job.file,
                    target,
                    max_bytes=self._settings.max_file_size_bytes,
                    resource_key=resource_key,
                )
            async with self._extractions:
                work = asyncio.create_task(self._extract(batch, job, target))
                try:
                    return await asyncio.shield(work)
                except asyncio.CancelledError:
                    # The native worker still owns its file and its CPU slot until it exits.
                    await asyncio.gather(work, return_exceptions=True)
                    raise

    async def _extract(self, batch: Batch, job: FileJob, target: Path) -> ExtractionResult:
        # Show the real first page while it is being read; the page swaps it in live.
        job.preview = await asyncio.to_thread(make_preview, target, job.file.kind)
        self._publish(batch, {"type": "file", "file": job.to_dict(include_text=False)})
        options = ExtractionOptions(
            ocr_language=batch.ocr_language,
            render_dpi=self._settings.ocr_render_dpi,
            min_chars_per_page=self._settings.pdf_text_min_chars_per_page,
            ocr_timeout_s=self._settings.ocr_timeout_seconds,
            ocr_workers=self._settings.effective_ocr_page_workers,
            max_pages=self._settings.max_pdf_pages,
            time_limit_s=self._settings.file_time_limit_seconds,
        )
        loop = asyncio.get_running_loop()

        def extract() -> ExtractionResult:
            # Opening and closing a PDF preview also takes the PDFium lock.
            previewer: PagePreviewer | contextlib.nullcontext[None]
            if job.file.kind is FileKind.PDF:
                previewer = PagePreviewer(target, PAGE_PREVIEW_WIDTH)
            else:
                previewer = contextlib.nullcontext(None)
            with previewer as pages:
                reporter = PageReporter(self, batch, job, pages, loop)
                return extract_file(target, job.file.kind, options, reporter)

        return await asyncio.to_thread(extract)

    def _page_reached(self, batch: Batch, job: FileJob, page: dict, image: bytes | None) -> None:
        """On the event loop: remember the page and its frame, and tell the subscribers."""
        if job.status is not FileStatus.PROCESSING:
            return  # an OCR callback that lost the race with the file finishing
        number = page["page"]
        if image is not None:
            job.page_previews[number] = image
            job.page_previews.move_to_end(number)
            while len(job.page_previews) > MAX_PAGE_PREVIEWS:
                job.page_previews.popitem(last=False)
        page["has_image"] = number in job.page_previews or (number == 1 and bool(job.preview))
        job.progress = {key: page[key] for key in ("page", "total", "method", "state")}
        self._publish(batch, {"type": "page", "file_id": job.file.id, **page})

    # ----- events ----------------------------------------------------------

    def _publish(self, batch: Batch, event: Event) -> None:
        batch.seq += 1
        event_copy = dict(event)
        event_copy["id"] = batch.seq
        batch.event_history.append(event_copy)

        for queue in list(batch.subscribers):
            try:
                queue.put_nowait(event_copy)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(event_copy)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    async def events(self, batch_id: str, last_event_id: int | None = None) -> AsyncIterator[Event]:
        """Yield a snapshot, then live updates until the batch is finished."""
        batch = self.get(batch_id)
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=100)
        batch.subscribers.append(queue)
        try:
            if last_event_id is not None and batch.event_history:
                replayed = False
                for past_event in batch.event_history:
                    if past_event.get("id", 0) > last_event_id:
                        yield past_event
                        replayed = True
                if replayed and batch.finished_at:
                    return
                if not replayed:
                    yield {"type": "snapshot", **batch.to_dict(include_text=True)}
            else:
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


class PageReporter:
    """Turns page events from the extraction threads into live ``page`` events.

    Called on the extraction thread (text-layer pages, OCR starting) and on OCR threads
    (OCR finished). Frames are rendered here, off the event loop, and only on the
    extraction thread, which is the only one that touches ``_last_frame``.
    """

    def __init__(
        self,
        manager: BatchManager,
        batch: Batch,
        job: FileJob,
        pages: PagePreviewer | None,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._manager = manager
        self._batch = batch
        self._job = job
        self._pages = pages
        self._loop = loop
        self._last_frame = float("-inf")  # the first page always gets a frame

    def __call__(self, event: PageEvent) -> None:
        reading = event.text is None
        image = None
        if reading or event.method is Method.PDF_PARSER:  # on the extraction thread
            now = time.monotonic()
            last_page = event.number >= event.total
            due = last_page or now - self._last_frame >= FRAME_INTERVAL_S
            if not due and not reading:
                return  # a text-layer page that went by between two frames
            if due:
                self._last_frame = now
                if event.number == 1 and self._job.preview is not None:
                    image = self._job.preview
                elif self._pages is not None:
                    image = self._pages.render(event.number)
        text = event.text or ""
        page = {
            "page": event.number,
            "total": event.total,
            "method": event.method.value,
            "state": "reading" if reading else "read",
            "excerpt": " ".join(text.split())[:EXCERPT_CHARS],
            "chars": len(text),
        }
        self._loop.call_soon_threadsafe(
            self._manager._page_reached, self._batch, self._job, page, image
        )


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
