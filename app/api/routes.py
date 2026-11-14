"""API routes: resolve a folder, start a batch, follow it, export it."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.api.schemas import (
    BatchCreated,
    BatchRequest,
    CapabilitiesResponse,
    DetailResponse,
    ErrorResponse,
    FolderRequest,
    FolderResponse,
)
from app.batch.manager import BatchManager, BatchNotFoundError, TooManyBatchesError
from app.config import Settings, get_settings
from app.drive.client import DriveClient, FolderListing
from app.drive.errors import DriveError
from app.drive.links import parse_folder_link
from app.extraction import ocr as ocr_engine
from app.extraction.preview import reencode_thumbnail

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

# Folder listings are cached between "list" and "run"; keep only the most recent ones so
# repeated lookups cannot grow memory without bound.
MAX_CACHED_LISTINGS = 100

# Documented refusals, so the API reference at /api/docs matches what the routes return.
FOLDER_ERRORS: dict[int | str, dict] = {
    400: {"model": ErrorResponse, "description": "Not a Google Drive folder link."},
    403: {"model": ErrorResponse, "description": "The folder is not shared publicly."},
    404: {"model": ErrorResponse, "description": "The folder does not exist."},
    500: {"model": ErrorResponse, "description": "The server's Google API key is invalid."},
    502: {"model": ErrorResponse, "description": "Google Drive could not be reached."},
}
BATCH_ERRORS: dict[int | str, dict] = {
    400: {
        "model": DetailResponse,
        "description": "Folder not listed yet, unknown files, or unknown OCR language.",
    },
    429: {"model": DetailResponse, "description": "Too many batches are already running."},
    503: {"model": DetailResponse, "description": "OCR is unavailable on the server."},
}
NO_IMAGE: dict[int | str, dict] = {
    200: {"content": {"image/jpeg": {}}, "description": "A JPEG image."},
    404: {"model": DetailResponse, "description": "No image for this file."},
}
UNKNOWN_BATCH: dict[int | str, dict] = {
    404: {"model": DetailResponse, "description": "No batch with this id (or it expired)."}
}


class ThumbnailCache:
    """The most recent Drive thumbnails, re-encoded; misses are fetched four at a time."""

    def __init__(self, size: int = 300) -> None:
        self.size = size
        self.items: OrderedDict[str, bytes | None] = OrderedDict()
        self.fetching = asyncio.Semaphore(4)

    def get(self, file_id: str) -> tuple[bool, bytes | None]:
        if file_id not in self.items:
            return False, None
        self.items.move_to_end(file_id)
        return True, self.items[file_id]

    def put(self, file_id: str, image: bytes | None) -> None:
        self.items[file_id] = image
        self.items.move_to_end(file_id)
        while len(self.items) > self.size:
            self.items.popitem(last=False)


def image_response(data: bytes) -> Response:
    headers = {"Cache-Control": "private, max-age=3600"}
    return Response(data, media_type="image/jpeg", headers=headers)


def get_drive(request: Request) -> DriveClient:
    return request.app.state.drive


def get_manager(request: Request) -> BatchManager:
    return request.app.state.batches


def get_listings(request: Request) -> dict[str, tuple[FolderListing, str | None]]:
    return request.app.state.listings


Drive = Annotated[DriveClient, Depends(get_drive)]
Manager = Annotated[BatchManager, Depends(get_manager)]
Listings = Annotated[dict[str, tuple[FolderListing, str | None]], Depends(get_listings)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def drive_error_response(exc: DriveError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status, content={"error": exc.message, "kind": exc.kind}
    )


@router.get("/capabilities", response_model=CapabilitiesResponse)
async def capabilities(drive: Drive, settings: AppSettings) -> CapabilitiesResponse:
    languages = list(ocr_engine.installed_languages())
    return CapabilitiesResponse(
        drive_mode=drive.mode,
        ocr_available=bool(languages),
        ocr_languages=languages,
        ocr_default_language=settings.ocr_default_language,
        max_file_size_mb=settings.max_file_size_mb,
        max_parallel_files=settings.max_parallel_files,
    )


@router.post("/folders", response_model=FolderResponse, responses=FOLDER_ERRORS)
async def resolve_folder(
    body: FolderRequest,
    drive: Drive,
    listings: Listings,
    settings: AppSettings,
) -> FolderResponse:
    link = parse_folder_link(body.url)
    listing = await drive.list_tree(
        link.folder_id,
        link.resource_key,
        depth=settings.subfolder_depth,
        max_folders=settings.max_folders,
    )
    listings.pop(link.folder_id, None)
    listings[link.folder_id] = (listing, link.resource_key)
    while len(listings) > MAX_CACHED_LISTINGS:
        listings.pop(next(iter(listings)))
    return FolderResponse(
        folder_id=listing.folder_id,
        folder_url=link.url,
        folder_name=listing.name,
        source=listing.source,
        files=[file.to_dict() for file in listing.files],
        skipped=listing.skipped,
        folders_scanned=listing.folders_scanned,
        folders_skipped=listing.folders_skipped,
    )


@router.get(
    "/folders/{folder_id}/files/{file_id}/thumbnail",
    response_class=Response,
    responses=NO_IMAGE,
)
async def file_thumbnail(
    folder_id: str, file_id: str, request: Request, drive: Drive, listings: Listings
) -> Response:
    """Drive's thumbnail of a listed file, fetched by the server and re-encoded as JPEG."""
    # Only files of a folder this server listed: the route is not a proxy for any Drive id.
    listing, resource_key = listings.get(folder_id, (None, None))
    if listing is None or not any(file.id == file_id for file in listing.files):
        raise HTTPException(404, "List the folder first; this file is not in it.")
    cache: ThumbnailCache = request.app.state.thumbnails
    found, image = cache.get(file_id)
    if not found:
        async with cache.fetching:
            try:
                image = reencode_thumbnail(await drive.thumbnail(file_id, resource_key))
                cache.put(file_id, image)
            except DriveError as exc:
                log.info("No thumbnail for %s: %s", file_id, exc.message)
                image = None
                if exc.kind == "not_found":  # a dropped connection is worth another try
                    cache.put(file_id, None)
    if image is None:
        raise HTTPException(404, "Drive has no thumbnail for this file.")
    return image_response(image)


@router.post("/batches", response_model=BatchCreated, status_code=202, responses=BATCH_ERRORS)
async def create_batch(
    body: BatchRequest,
    manager: Manager,
    listings: Listings,
    settings: AppSettings,
) -> BatchCreated:
    try:
        listing, resource_key = listings[body.folder_id]
    except KeyError:
        raise HTTPException(400, "List the folder before starting a batch.") from None
    by_id = {file.id: file for file in listing.files}
    unknown = [file_id for file_id in body.file_ids if file_id not in by_id]
    if unknown:
        raise HTTPException(400, f"{len(unknown)} selected file(s) are not in this folder.")
    language: str | None = None
    if ocr_engine.installed_languages():
        try:
            language = ocr_engine.validate_language(
                body.ocr_language or settings.ocr_default_language
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except ocr_engine.OcrUnavailableError as exc:
            raise HTTPException(503, str(exc)) from exc
    # Without Tesseract the batch still runs: native PDFs are read from their text layer,
    # images fail one by one with the reason, and scanned pages stay empty with a warning.
    files = [by_id[file_id] for file_id in dict.fromkeys(body.file_ids)]
    try:
        batch = manager.create(
            folder_id=listing.folder_id,
            folder_url=f"https://drive.google.com/drive/folders/{listing.folder_id}",
            folder_name=listing.name,
            files=files,
            ocr_language=language,
            resource_key=resource_key,
        )
    except TooManyBatchesError as exc:
        raise HTTPException(429, str(exc)) from exc
    return BatchCreated(batch_id=batch.id)


@router.get("/batches/{batch_id}", responses=UNKNOWN_BATCH)
async def get_batch(batch_id: str, manager: Manager) -> dict:
    try:
        return manager.get(batch_id).to_dict()
    except BatchNotFoundError:
        raise HTTPException(404, "Unknown batch.") from None


@router.get("/batches/{batch_id}/events", responses=UNKNOWN_BATCH)
async def batch_events(batch_id: str, manager: Manager):
    try:
        manager.get(batch_id)
    except BatchNotFoundError:
        raise HTTPException(404, "Unknown batch.") from None

    async def stream() -> AsyncIterator[str]:
        async for event in manager.events(batch_id):
            yield f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/batches/{batch_id}/files/{file_id}/preview",
    response_class=Response,
    responses={**NO_IMAGE, **UNKNOWN_BATCH},
)
async def file_preview(batch_id: str, file_id: str, manager: Manager) -> Response:
    """The first page of a file as the server rendered it after downloading it."""
    try:
        job = manager.get(batch_id).jobs.get(file_id)
    except BatchNotFoundError:
        raise HTTPException(404, "Unknown batch.") from None
    if job is None or job.preview is None:
        raise HTTPException(404, "No preview for this file yet.")
    return image_response(job.preview)


@router.get(
    "/batches/{batch_id}/files/{file_id}/pages/{page}/preview",
    response_class=Response,
    responses={**NO_IMAGE, **UNKNOWN_BATCH},
)
async def page_preview(batch_id: str, file_id: str, page: int, manager: Manager) -> Response:
    """A recent page as the server rendered it while reading the file (live view)."""
    try:
        job = manager.get(batch_id).jobs.get(file_id)
    except BatchNotFoundError:
        raise HTTPException(404, "Unknown batch.") from None
    image = job.page_previews.get(page) if job else None
    if image is None and job is not None and page == 1:
        image = job.preview
    if image is None:
        raise HTTPException(404, "That page is no longer available.")
    return image_response(image)


@router.get("/batches/{batch_id}/export", responses=UNKNOWN_BATCH)
async def export_batch(batch_id: str, manager: Manager) -> JSONResponse:
    try:
        batch = manager.get(batch_id)
    except BatchNotFoundError:
        raise HTTPException(404, "Unknown batch.") from None
    filename = f"drive-to-text-{batch.id}.json"
    return JSONResponse(
        content=batch.to_dict(include_text=True),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
