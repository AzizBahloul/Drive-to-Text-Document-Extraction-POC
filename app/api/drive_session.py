"""Short-lived, browser-bound Drive access. Credentials and private listings stay in memory."""

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.api.schemas import DriveId
from app.drive.client import DriveClient, FolderListing

router = APIRouter(prefix="/api/drive")
COOKIE = "drive_session"


class PickedFile(BaseModel):
    id: DriveId
    resource_key: DriveId | None = None


class ConnectRequest(BaseModel):
    mode: Literal["folder", "files"]
    expires_in: int = Field(gt=0, le=86400)
    files: list[PickedFile] = Field(default_factory=list, max_length=200)


@dataclass
class DriveSession:
    drive: DriveClient
    expires: float
    mode: str
    listings: dict[str, tuple[FolderListing, str | None]] = field(default_factory=dict)
    picked: FolderListing | None = None


def close_session(state, key: str | None) -> None:
    if key and state.drive_sessions.pop(key, None):
        state.batches.discard_private_batches(key)


def session(request: Request) -> DriveSession | None:
    sessions = request.app.state.drive_sessions
    key = request.cookies.get(COOKIE)
    current = sessions.get(key)
    if current and current.expires <= time.monotonic():
        close_session(request.app.state, key)
        return None
    return current


@router.get("/config")
async def config(request: Request) -> dict:
    settings = request.app.state.settings
    return {
        "client_id": settings.google_oauth_client_id,
        "picker_key": settings.google_picker_key,
        "project_number": settings.google_project_number,
    }


@router.post("/session", status_code=204)
async def connect(body: ConnectRequest, request: Request) -> Response:
    settings = request.app.state.settings
    if not settings.google_oauth_client_id:
        raise HTTPException(503, "Configure a Google OAuth client ID to connect private Drive.")
    if body.mode == "files" and not (settings.google_picker_key and settings.google_project_number):
        raise HTTPException(503, "Configure Google Picker first.")
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token or len(token) > 4096 or not token.isprintable():
        raise HTTPException(401, "Connect to Google Drive again.")
    drive = DriveClient(request.app.state.http, access_token=token)
    ttl = min(body.expires_in, 3600)
    current = DriveSession(drive, time.monotonic() + ttl, body.mode)
    if body.mode == "files":
        if not body.files:
            raise HTTPException(400, "Choose at least one PDF or image.")
        limit = asyncio.Semaphore(4)

        async def read(item: PickedFile):
            async with limit:
                return await drive.file_metadata(item.id, item.resource_key)

        unique = {item.id: item for item in body.files}
        files = await asyncio.gather(*(read(item) for item in unique.values()))
        listing = FolderListing(
            "selection-" + secrets.token_urlsafe(12),
            "Selected Drive files",
            tuple(files),
            "drive-picker",
        )
        current.picked = listing
        current.listings[listing.folder_id] = (listing, None)
    sessions = request.app.state.drive_sessions
    for key in list(sessions):
        if sessions[key].expires <= time.monotonic():
            close_session(request.app.state, key)
    close_session(request.app.state, request.cookies.get(COOKIE))
    if len(sessions) >= 100:
        raise HTTPException(429, "Too many active Drive connections.")
    key = secrets.token_urlsafe(32)
    sessions[key] = current
    asyncio.get_running_loop().call_later(ttl, close_session, request.app.state, key)
    response = Response(status_code=204)
    response.set_cookie(
        COOKIE,
        key,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        max_age=ttl,
        path="/api",
    )
    return response


@router.delete("/session", status_code=204)
async def disconnect(request: Request) -> Response:
    close_session(request.app.state, request.cookies.get(COOKIE))
    response = Response(status_code=204)
    response.delete_cookie(COOKIE, path="/api")
    return response
