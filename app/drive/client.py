"""List and download public and authorized Google Drive documents.

Two strategies are implemented behind one interface:

* ``DriveApiStrategy`` uses Drive v3 with a public API key or a user access token. It is
  accurate, paginated and returns file sizes. This is the preferred path.
* ``PublicPageStrategy`` reads the public ``embeddedfolderview`` page that Google
  serves for any folder shared with "anyone with the link". It needs no
  credentials but is best-effort: Google does not document that page, it does
  not expose sizes, and it only lists the first few hundred entries.

The strategy is chosen once from the settings; the rest of the app only talks to
``DriveClient``.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import httpx

from app.drive.errors import DriveError

log = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/drive/v3"
PUBLIC_FOLDER_VIEW = "https://drive.google.com/embeddedfolderview"
PUBLIC_DOWNLOAD = "https://drive.usercontent.google.com/download"
# Drive renders a thumbnail of any file shared publicly (first page for PDFs).
PUBLIC_THUMBNAIL = "https://drive.google.com/thumbnail"
THUMBNAIL_MAX_BYTES = 2 * 1024 * 1024

LIST_RETRIES = 2  # extra attempts after a connection-level failure (listing and download)
PDF_MIME = "application/pdf"
IMAGE_MIMES = {"image/png", "image/jpeg"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
FOLDER_MIME = "application/vnd.google-apps.folder"
# Drive ids are URL-safe base64-ish; anything else is refused before it reaches a URL or path.
_FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
SUBFOLDER_CONCURRENCY = 4  # subfolders listed at once: quick, without hammering Google


class FileKind(StrEnum):
    PDF = "pdf"
    IMAGE = "image"


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    kind: FileKind
    size: int | None = None
    folder: str = ""  # path below the shared folder, "" for files at its top level

    resource_key: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "mime_type": self.mime_type,
            "kind": self.kind.value,
            "size": self.size,
            "folder": self.folder,
        }


@dataclass(frozen=True)
class Subfolder:
    id: str
    name: str
    resource_key: str | None = None


@dataclass(frozen=True)
class FolderListing:
    folder_id: str
    name: str | None
    files: tuple[DriveFile, ...]
    source: str
    skipped: int = 0  # entries that are neither documents nor folders
    subfolders: tuple[Subfolder, ...] = ()
    folders_scanned: int = 1
    folders_skipped: int = 0  # subfolders left out: too deep, over the limit, or unreadable


def classify(name: str, mime_type: str | None) -> FileKind | None:
    """Decide whether a Drive entry is a PDF, an image, or something we ignore."""
    mime = (mime_type or "").lower()
    if mime == PDF_MIME:
        return FileKind.PDF
    if mime in IMAGE_MIMES:
        return FileKind.IMAGE
    extension = os.path.splitext(name or "")[1].lower()
    if extension == ".pdf":
        return FileKind.PDF
    if extension in IMAGE_EXTENSIONS:
        return FileKind.IMAGE
    return None


def _guess_mime(name: str, kind: FileKind) -> str:
    if kind is FileKind.PDF:
        return PDF_MIME
    return "image/png" if name.lower().endswith(".png") else "image/jpeg"


async def _get_with_retry(
    http: httpx.AsyncClient, url: str, *, params: dict, headers: dict[str, str]
) -> httpx.Response:
    """GET with a couple of retries on connection-level failures (resets, timeouts)."""
    for attempt in range(LIST_RETRIES + 1):
        try:
            return await http.get(url, params=params, headers=headers, follow_redirects=not headers)
        except httpx.TransportError as exc:
            if attempt == LIST_RETRIES:
                raise DriveError("upstream", f"Could not reach Google Drive: {exc}") from exc
            log.warning("Drive request failed (%s); retrying", exc)
            await asyncio.sleep(0.5 * (attempt + 1))
    raise AssertionError("unreachable")


class ListingStrategy(Protocol):
    name: str

    async def list_folder(
        self, folder_id: str, resource_key: str | None, known_name: str | None = None
    ) -> FolderListing: ...

    def download_url(self, file_id: str) -> str: ...

    def download_headers(self, resource_key: str | None, file_id: str) -> dict[str, str]: ...


class DriveApiStrategy:
    name = "drive-api"

    def __init__(
        self, http: httpx.AsyncClient, api_key: str | None, access_token: str | None = None
    ) -> None:
        self._http = http
        self._credentials = (
            {"Authorization": f"Bearer {access_token}"}
            if access_token
            else {"X-Goog-Api-Key": api_key or ""}
        )
        if access_token:
            self.name = "drive-oauth"

    def _headers(self, resource_key: str | None, folder_id: str) -> dict[str, str]:
        # The key travels in a header, never in the URL, so it cannot end up in request
        # logs, proxy logs or error messages that quote the URL.
        headers = self._credentials.copy()
        if resource_key:
            headers["X-Goog-Drive-Resource-Keys"] = f"{folder_id}/{resource_key}"
        return headers

    async def list_folder(
        self, folder_id: str, resource_key: str | None, known_name: str | None = None
    ) -> FolderListing:
        headers = self._headers(resource_key, folder_id)
        folder_name = known_name or await self._folder_name(folder_id, headers)
        files: list[DriveFile] = []
        subfolders: list[Subfolder] = []
        skipped = 0
        page_token: str | None = None
        while True:
            params = {
                "q": f"'{folder_id}' in parents and trashed = false",
                "fields": "nextPageToken,files(id,name,mimeType,size,resourceKey)",
                "pageSize": "200",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page_token:
                params["pageToken"] = page_token
            data = await self._get_json(f"{API_BASE}/files", params, headers, folder_id)
            for entry in data.get("files", []):
                entry_id = str(entry.get("id", ""))
                if entry.get("mimeType") == FOLDER_MIME and _FILE_ID_RE.match(entry_id):
                    key = entry.get("resourceKey")
                    subfolders.append(
                        Subfolder(
                            entry_id,
                            entry.get("name") or entry_id,
                            key if isinstance(key, str) and _FILE_ID_RE.match(key) else None,
                        )
                    )
                    continue
                kind = classify(entry.get("name", ""), entry.get("mimeType"))
                if kind is None or not _FILE_ID_RE.match(entry_id):
                    skipped += 1
                    continue
                key = entry.get("resourceKey")
                size = entry.get("size")
                files.append(
                    DriveFile(
                        id=entry["id"],
                        name=entry.get("name", entry["id"]),
                        mime_type=entry.get("mimeType") or _guess_mime(entry["name"], kind),
                        kind=kind,
                        resource_key=key
                        if isinstance(key, str) and _FILE_ID_RE.fullmatch(key)
                        else None,
                        size=int(size) if size is not None else None,
                    )
                )
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        files.sort(key=lambda f: f.name.lower())
        return FolderListing(
            folder_id, folder_name, tuple(files), self.name, skipped, tuple(subfolders)
        )

    async def _folder_name(self, folder_id: str, headers: dict[str, str]) -> str | None:
        params = {"fields": "id,name,mimeType", "supportsAllDrives": "true"}
        data = await self._get_json(f"{API_BASE}/files/{folder_id}", params, headers, folder_id)
        if data.get("mimeType") != FOLDER_MIME:
            raise DriveError(
                "invalid_link",
                "That link points to a single file. Share the folder that contains it instead.",
            )
        return data.get("name")

    async def _get_json(
        self, url: str, params: dict, headers: dict[str, str], folder_id: str
    ) -> dict:
        response = await _get_with_retry(self._http, url, params=params, headers=headers)
        if response.status_code == 200:
            return response.json()
        reason = _api_error_reason(response)
        log.warning("Drive API %s for folder %s: %s", response.status_code, folder_id, reason)
        if self.name == "drive-oauth" and response.status_code in {401, 403, 404}:
            raise DriveError(
                "forbidden", "Drive denied access. Reconnect or choose a file you can read."
            )
        if response.status_code == 404:
            raise DriveError(
                "not_found",
                "This folder does not exist or is not shared publicly. "
                "Set its sharing to 'Anyone with the link' and try again.",
            )
        if response.status_code == 403:
            if reason in {"forbidden", "insufficientFilePermissions", "ACCESS_DENIED"}:
                raise DriveError(
                    "forbidden",
                    "Google refused access to this folder. Set its sharing to 'Anyone with "
                    "the link', and check that GOOGLE_API_KEY may call the Drive API.",
                )
            raise DriveError(
                "config",
                "Google rejected the API key. Check GOOGLE_API_KEY and that the Drive API "
                "is enabled for that key.",
            )
        if response.status_code == 400 and reason in {
            "keyInvalid",
            "API_KEY_INVALID",
            "badRequest",
        }:
            raise DriveError("config", "GOOGLE_API_KEY is not a valid Google API key.")
        raise DriveError("upstream", f"Google Drive answered with HTTP {response.status_code}.")

    def download_url(self, file_id: str) -> str:
        return f"{API_BASE}/files/{file_id}?alt=media&supportsAllDrives=true"

    def download_headers(self, resource_key: str | None, file_id: str) -> dict[str, str]:
        return self._headers(resource_key, file_id)


_ENTRY_RE = re.compile(
    r'<div class="flip-entry"[^>]*id="entry-(?P<id>[A-Za-z0-9_-]+)".*?'
    r'<div class="flip-entry-title">(?P<title>.*?)</div>',
    re.DOTALL,
)
_FOLDER_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL)
_ENTRY_URL_RE = re.compile(r"/drive/folders/")
_ENTRY_RESOURCE_KEY_RE = re.compile(r"[?&](?:amp;)?resourcekey=([A-Za-z0-9_-]{1,128})")
# The list icon encodes the MIME type: .../16/type/application/pdf
_ENTRY_MIME_RE = re.compile(r"/type/([a-z]+/[a-z0-9.+-]+)")


class PublicPageStrategy:
    name = "public-page"

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def list_folder(
        self, folder_id: str, resource_key: str | None, known_name: str | None = None
    ) -> FolderListing:
        params = {"id": folder_id}
        if resource_key:
            params["resourcekey"] = resource_key
        response = await _get_with_retry(self._http, PUBLIC_FOLDER_VIEW, params=params, headers={})
        if response.status_code in {403, 404}:
            raise DriveError(
                "not_found",
                "This folder does not exist or is not shared publicly. "
                "Set its sharing to 'Anyone with the link' and try again.",
            )
        if response.status_code != 200:
            raise DriveError("upstream", f"Google Drive answered with HTTP {response.status_code}.")
        return parse_public_folder_page(folder_id, response.text, self.name)

    def download_url(self, file_id: str) -> str:
        return f"{PUBLIC_DOWNLOAD}?id={file_id}&export=download&confirm=t"

    def download_headers(self, resource_key: str | None, file_id: str) -> dict[str, str]:
        return {}


def parse_public_folder_page(folder_id: str, page: str, source: str) -> FolderListing:
    """Extract file entries from Google's embedded folder view HTML."""
    if 'class="flip-entry"' not in page and "flip-entries" not in page:
        # Private folders redirect to a sign-in page; unknown ids render an error page.
        raise DriveError(
            "not_found",
            "This folder does not exist or is not shared publicly. "
            "Set its sharing to 'Anyone with the link' and try again.",
        )
    files: list[DriveFile] = []
    subfolders: list[Subfolder] = []
    skipped = 0
    for match in _ENTRY_RE.finditer(page):
        entry_id = match.group("id")
        name = html.unescape(re.sub(r"<[^>]+>", "", match.group("title"))).strip()
        block = match.group(0)
        if _ENTRY_URL_RE.search(block):
            key = _ENTRY_RESOURCE_KEY_RE.search(block)
            subfolders.append(Subfolder(entry_id, name or entry_id, key.group(1) if key else None))
            continue
        mime_match = _ENTRY_MIME_RE.search(block)
        mime = mime_match.group(1) if mime_match else None
        kind = classify(name, mime)
        if kind is None:
            skipped += 1
            continue
        files.append(
            DriveFile(
                id=entry_id,
                name=name,
                mime_type=mime or _guess_mime(name, kind),
                kind=kind,
                resource_key=key.group(1)
                if (key := _ENTRY_RESOURCE_KEY_RE.search(block))
                else None,
            )
        )
    title_match = _FOLDER_TITLE_RE.search(page)
    folder_name = html.unescape(title_match.group(1)).strip() if title_match else None
    if folder_name and folder_name.lower().endswith(" - google drive"):
        folder_name = folder_name[: -len(" - google drive")]
    files.sort(key=lambda f: f.name.lower())
    return FolderListing(
        folder_id, folder_name or None, tuple(files), source, skipped, tuple(subfolders)
    )


def _api_error_reason(response: httpx.Response) -> str:
    """Best-effort reason string from a Google API error body (old and new shapes)."""
    try:
        payload = response.json()
    except ValueError:
        return ""
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    for detail in error.get("details") or []:
        if isinstance(detail, dict) and detail.get("reason"):
            return str(detail["reason"])
    errors = error.get("errors") or []
    if errors and isinstance(errors[0], dict):
        return str(errors[0].get("reason", ""))
    return str(error.get("status", ""))


_CONFIRM_HREF_RE = re.compile(r'href="([^"]*confirm=[^"]*)"', re.IGNORECASE)
_CONFIRM_INPUT_RE = re.compile(r'name="confirm"\s+value="([^"]+)"', re.IGNORECASE)
_FORM_ACTION_RE = re.compile(r'<form[^>]+action="([^"]+)"', re.IGNORECASE)


def _extract_confirm_download_url(original_url: str, html_page: str) -> str | None:
    """Parse Google Drive's 'virus scan warning' confirmation page for large public files."""
    href_match = _CONFIRM_HREF_RE.search(html_page)
    if href_match:
        raw_href = html.unescape(href_match.group(1))
        if raw_href.startswith("/"):
            return f"https://drive.google.com{raw_href}"
        return raw_href

    confirm_match = _CONFIRM_INPUT_RE.search(html_page)
    if confirm_match:
        token = confirm_match.group(1)
        action_match = _FORM_ACTION_RE.search(html_page)
        action = html.unescape(action_match.group(1)) if action_match else original_url
        sep = "&" if "?" in action else "?"
        return f"{action}{sep}confirm={token}"

    token_search = re.search(r"confirm=([A-Za-z0-9_-]+)", html_page)
    if token_search:
        sep = "&" if "?" in original_url else "?"
        return f"{original_url}{sep}confirm={token_search.group(1)}"

    return None


class DriveClient:
    """Facade over the chosen listing/download strategy."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        api_key: str | None = None,
        *,
        access_token: str | None = None,
    ) -> None:
        self._http = http
        self.strategy: ListingStrategy
        if api_key or access_token:
            self.strategy = DriveApiStrategy(http, api_key, access_token)
        else:
            self.strategy = PublicPageStrategy(http)

    @property
    def mode(self) -> str:
        return self.strategy.name

    async def list_folder(
        self, folder_id: str, resource_key: str | None = None, known_name: str | None = None
    ) -> FolderListing:
        return await self.strategy.list_folder(folder_id, resource_key, known_name)

    async def file_metadata(self, file_id: str, resource_key: str | None) -> DriveFile:
        strategy = self.strategy
        if not isinstance(strategy, DriveApiStrategy):
            raise DriveError("config", "Google Drive API access is required.")
        headers = strategy.download_headers(resource_key, file_id)
        data = await strategy._get_json(
            f"{API_BASE}/files/{file_id}",
            {"fields": "id,name,mimeType,size,resourceKey,trashed", "supportsAllDrives": "true"},
            headers,
            file_id,
        )
        mime = data.get("mimeType", "")
        kind = classify(data.get("name", ""), mime)
        if (
            data.get("id") != file_id
            or not kind
            or data.get("trashed")
            or mime.startswith("application/vnd.google-apps.")
        ):
            raise DriveError("invalid_link", "Choose a PDF, PNG or JPEG file.")
        return DriveFile(
            file_id,
            data["name"],
            mime,
            kind,
            int(data["size"]) if "size" in data else None,
            resource_key=resource_key,
        )

    async def list_tree(
        self,
        folder_id: str,
        resource_key: str | None = None,
        *,
        depth: int = 3,
        max_folders: int = 50,
        on_found: Callable[[tuple[DriveFile, ...]], None] | None = None,
    ) -> FolderListing:
        """List a folder and its subfolders, level by level, ``depth`` levels down.

        At most ``max_folders`` folders are read in all (the shared one included), each
        folder once even if Drive shows it twice, a few at a time. A subfolder that cannot
        be read is counted in ``folders_skipped`` instead of failing the whole listing;
        only the shared folder itself must be readable. Every file records the path of
        the folder it was found in.

        ``on_found`` is called with the new documents as each folder comes back: once for
        the shared folder (even when it holds none), then for every subfolder that adds some.
        """
        root = await self.list_folder(folder_id, resource_key)
        files = list(root.files)
        if on_found:
            on_found(root.files)
        seen_files = {file.id for file in files}
        skipped = root.skipped
        scanned, left_out, attempted = 1, 0, 1
        visited = {folder_id}
        frontier = [(sub, sub.name, 1) for sub in root.subfolders]
        limit = asyncio.Semaphore(SUBFOLDER_CONCURRENCY)

        announced = set(seen_files)

        async def read(sub: Subfolder, path: str) -> FolderListing:
            async with limit:
                result = await self.list_folder(sub.id, sub.resource_key, sub.name)
            if on_found:  # as soon as this folder is back, not when its whole level is
                new = tuple(
                    replace(file, folder=path) for file in result.files if file.id not in announced
                )
                announced.update(file.id for file in new)
                if new:
                    on_found(new)
            return result

        while frontier:
            level: list[tuple[Subfolder, str, int]] = []
            for sub, path, level_depth in frontier:
                if sub.id in visited:
                    continue
                visited.add(sub.id)
                if level_depth > depth or attempted >= max_folders:
                    left_out += 1
                    continue
                attempted += 1
                level.append((sub, path, level_depth))
            frontier = []
            results = await asyncio.gather(
                *(read(sub, path) for sub, path, _ in level), return_exceptions=True
            )
            for (sub, path, level_depth), result in zip(level, results, strict=True):
                if isinstance(result, DriveError):
                    log.warning("Subfolder %s left out: %s", sub.id, result.message)
                    left_out += 1
                    continue
                if isinstance(result, BaseException):
                    raise result
                scanned += 1
                skipped += result.skipped
                for file in result.files:
                    if file.id not in seen_files:
                        seen_files.add(file.id)
                        files.append(replace(file, folder=path))
                frontier.extend(
                    (child, f"{path}/{child.name}", level_depth + 1) for child in result.subfolders
                )

        files.sort(key=lambda f: (f.folder != "", f.folder.lower(), f.name.lower()))
        return replace(
            root,
            files=tuple(files),
            skipped=skipped,
            subfolders=(),
            folders_scanned=scanned,
            folders_skipped=left_out,
        )

    async def thumbnail(self, file_id: str, resource_key: str | None = None) -> bytes:
        """Drive's own thumbnail of a public file, as raw bytes (at most 2 MB)."""
        params = {"id": file_id, "sz": "w360"}
        if resource_key:
            params["resourcekey"] = resource_key
        try:
            async with self._http.stream(
                "GET", PUBLIC_THUMBNAIL, params=params, timeout=15
            ) as response:
                content_type = response.headers.get("content-type", "")
                if response.status_code != 200 or not content_type.startswith("image/"):
                    raise DriveError("not_found", "Drive has no thumbnail for this file.")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data += chunk
                    if len(data) > THUMBNAIL_MAX_BYTES:
                        raise DriveError("upstream", "The thumbnail is unexpectedly large.")
                return bytes(data)
        except httpx.TransportError as exc:
            raise DriveError("upstream", f"Could not reach Google Drive: {_describe(exc)}") from exc

    async def download(
        self,
        file: DriveFile,
        destination: Path,
        *,
        max_bytes: int,
        resource_key: str | None = None,
    ) -> int:
        """Stream a file to ``destination``; returns the number of bytes written."""
        if file.size is not None and file.size > max_bytes:
            raise DriveError(
                "too_large",
                f"{file.name} is {file.size / 1_048_576:.1f} MB; the limit is "
                f"{max_bytes / 1_048_576:.0f} MB.",
            )
        url = self.strategy.download_url(file.id)
        headers = self.strategy.download_headers(file.resource_key or resource_key, file.id)
        if self.mode == "public-page" and file.resource_key:
            url += f"&resourcekey={file.resource_key}"
        for attempt in range(LIST_RETRIES + 1):
            try:
                return await self._stream_to(url, headers, destination, max_bytes, file)
            except httpx.TransportError as exc:
                if attempt == LIST_RETRIES:
                    raise DriveError("upstream", f"Download failed: {_describe(exc)}") from exc
                log.warning("Download of %s failed (%s); retrying", file.name, _describe(exc))
                await asyncio.sleep(0.5 * (attempt + 1))
        raise AssertionError("unreachable")

    async def _stream_to(
        self,
        url: str,
        headers: dict[str, str],
        destination: Path,
        max_bytes: int,
        file: DriveFile,
    ) -> int:
        allow_confirmation = self.mode == "public-page"
        for _ in range(6):
            async with self._http.stream("GET", url, headers=headers, follow_redirects=False) as r:
                if r.has_redirect_location:
                    url = _download_target(url, r.headers["location"])
                    headers = {}
                    continue
                if r.status_code != 200:
                    raise DriveError(
                        "not_found" if r.status_code in {403, 404} else "upstream",
                        f"Google Drive refused the download (HTTP {r.status_code}).",
                    )
                if r.headers.get("content-type", "").lower().startswith("text/html"):
                    if allow_confirmation:
                        allow_confirmation = False
                        body = bytearray()
                        async for chunk in r.aiter_bytes(chunk_size=8192):
                            body.extend(chunk)
                            if len(body) >= 131072:
                                break
                        target = _extract_confirm_download_url(url, body.decode(errors="replace"))
                        if target:
                            url = _download_target(url, target)
                            headers = {}
                            continue
                    raise DriveError(
                        "forbidden",
                        "Drive returned a web page. Check sharing or download the file manually.",
                    )
                written = 0
                with destination.open("wb") as handle:
                    async for chunk in _iter_limited(r.aiter_bytes(), max_bytes, file):
                        handle.write(chunk)
                        written += len(chunk)
                return written
        raise DriveError("upstream", "Google Drive returned too many download redirects.")


def _download_target(base: str, location: str) -> str:
    try:
        target = httpx.URL(base).join(location)
        if (
            target.scheme == "https"
            and not target.userinfo
            and target.port in {None, 443}
            and (
                target.host in {"drive.google.com", "drive.usercontent.google.com"}
                or target.host.endswith(".googleusercontent.com")
            )
        ):
            return str(target)
    except (ValueError, httpx.InvalidURL):
        pass
    raise DriveError("upstream", "Drive returned an unexpected download destination.")


def _describe(exc: Exception) -> str:
    message = str(exc).strip()
    return (
        f"{type(exc).__name__}: {message}"
        if message
        else f"{type(exc).__name__} (connection dropped)"
    )


async def _iter_limited(
    chunks: AsyncIterator[bytes], max_bytes: int, file: DriveFile
) -> AsyncIterator[bytes]:
    total = 0
    async for chunk in chunks:
        total += len(chunk)
        if total > max_bytes:
            raise DriveError(
                "too_large",
                f"{file.name} is larger than the {max_bytes / 1_048_576:.0f} MB limit.",
            )
        yield chunk
