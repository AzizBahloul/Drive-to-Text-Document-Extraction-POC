"""Drive client against a mocked Google: listing, error mapping, downloads, page scraping."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.drive.client import DriveClient, FileKind, parse_public_folder_page
from app.drive.errors import DriveError
from tests.conftest import drive_file

FOLDER = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"


def api_client(handler) -> DriveClient:
    transport = httpx.MockTransport(handler)
    return DriveClient(httpx.AsyncClient(transport=transport), api_key="test-key")


def public_client(handler) -> DriveClient:
    transport = httpx.MockTransport(handler)
    return DriveClient(httpx.AsyncClient(transport=transport), api_key=None)


def google_error(status: int, reason: str) -> httpx.Response:
    if reason.isupper():  # new-style body: reason lives in error.details[]
        body = {"error": {"code": status, "message": reason, "details": [{"reason": reason}]}}
    else:
        body = {"error": {"code": status, "errors": [{"reason": reason}], "message": reason}}
    return httpx.Response(status, json=body)


async def test_api_key_listing_filters_paginates_and_sorts() -> None:
    pages = {
        None: {
            "nextPageToken": "p2",
            "files": [
                {"id": "f2", "name": "zeta.pdf", "mimeType": "application/pdf", "size": "10"},
                {"id": "f9", "name": "notes.txt", "mimeType": "text/plain", "size": "5"},
                {"id": "sub", "name": "sub", "mimeType": "application/vnd.google-apps.folder"},
            ],
        },
        "p2": {"files": [{"id": "f1", "name": "Alpha.PNG", "mimeType": "image/png", "size": "20"}]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Goog-Api-Key"] == "test-key"
        assert "key" not in request.url.params, "the API key must never be in the URL"
        if request.url.path.endswith(f"/files/{FOLDER}"):
            return httpx.Response(
                200,
                json={
                    "id": FOLDER,
                    "name": "Inbox",
                    "mimeType": "application/vnd.google-apps.folder",
                },
            )
        assert f"'{FOLDER}' in parents" in request.url.params["q"]
        return httpx.Response(200, json=pages[request.url.params.get("pageToken")])

    listing = await api_client(handler).list_folder(FOLDER)
    assert listing.name == "Inbox"
    assert listing.source == "drive-api"
    assert [f.name for f in listing.files] == ["Alpha.PNG", "zeta.pdf"]
    assert listing.files[0].kind is FileKind.IMAGE and listing.files[0].size == 20
    assert listing.skipped == 1  # notes.txt; the subfolder is reported, not ignored
    assert [(sub.id, sub.name) for sub in listing.subfolders] == [("sub", "sub")]


async def test_api_key_rejects_a_file_link_pretending_to_be_a_folder() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"id": FOLDER, "name": "x.pdf", "mimeType": "application/pdf"}
        )

    with pytest.raises(DriveError) as info:
        await api_client(handler).list_folder(FOLDER)
    assert info.value.kind == "invalid_link"


@pytest.mark.parametrize(
    ("status", "reason", "kind"),
    [
        (404, "notFound", "not_found"),
        (403, "forbidden", "forbidden"),
        (403, "accessNotConfigured", "config"),
        (400, "keyInvalid", "config"),
        (400, "API_KEY_INVALID", "config"),
        (500, "backendError", "upstream"),
    ],
)
async def test_api_errors_map_to_user_facing_kinds(status: int, reason: str, kind: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return google_error(status, reason)

    with pytest.raises(DriveError) as info:
        await api_client(handler).list_folder(FOLDER)
    assert info.value.kind == kind


async def test_network_failure_is_retried_then_reported_as_upstream(monkeypatch) -> None:
    monkeypatch.setattr("app.drive.client.asyncio.sleep", _no_sleep)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("boom")

    with pytest.raises(DriveError) as info:
        await api_client(handler).list_folder(FOLDER)
    assert info.value.kind == "upstream"
    assert attempts == 3


async def test_transient_reset_recovers_on_retry(monkeypatch) -> None:
    monkeypatch.setattr("app.drive.client.asyncio.sleep", _no_sleep)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadError("connection reset")
        return httpx.Response(200, text=PUBLIC_PAGE)

    listing = await public_client(handler).list_folder(FOLDER)
    assert attempts == 2 and len(listing.files) == 4


async def test_download_retries_after_a_dropped_connection(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("app.drive.client.asyncio.sleep", _no_sleep)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadError("")
        return httpx.Response(
            200, content=b"%PDF-1.4 ok", headers={"content-type": "application/pdf"}
        )

    client = public_client(handler)
    target = tmp_path / "a.pdf"
    written = await client.download(
        drive_file("a.pdf", FileKind.PDF, size=None), target, max_bytes=1000
    )
    assert attempts == 2 and written == 11 and target.read_bytes().endswith(b"ok")


async def _no_sleep(_: float) -> None:
    return None


async def test_download_streams_to_disk_and_enforces_size_limit(tmp_path: Path) -> None:
    payload = b"%PDF-1.4 " + b"x" * 5000

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["alt"] == "media"
        assert "key" not in request.url.params
        assert request.headers["X-Goog-Api-Key"] == "test-key"
        return httpx.Response(200, content=payload, headers={"content-type": "application/pdf"})

    client = api_client(handler)
    file = drive_file("a.pdf", FileKind.PDF, size=None)
    target = tmp_path / "a.pdf"
    assert await client.download(file, target, max_bytes=10_000) == len(payload)
    assert target.read_bytes() == payload

    with pytest.raises(DriveError) as info:
        await client.download(file, tmp_path / "b.pdf", max_bytes=100)
    assert info.value.kind == "too_large"


async def test_download_refuses_html_pages(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"<html>sign in</html>", headers={"content-type": "text/html"}
        )

    with pytest.raises(DriveError) as info:
        await public_client(handler).download(
            drive_file("a.pdf", FileKind.PDF, size=None), tmp_path / "a.pdf", max_bytes=1000
        )
    assert info.value.kind == "forbidden"


PUBLIC_PAGE = """
<html><head><title>Shared scans - Google Drive</title></head><body>
<div class="flip-entries">
<div class="flip-entry" id="entry-1aaaaaaaaaaaaaaaaaa">
  <a href="https://drive.google.com/file/d/1aaaaaaaaaaaaaaaaaa/view?usp=drive_web">
  <div class="flip-entry-title">Contrat &amp; annexe.pdf</div></a></div>
<div class="flip-entry" id="entry-1bbbbbbbbbbbbbbbbbb">
  <a href="https://drive.google.com/file/d/1bbbbbbbbbbbbbbbbbb/view">
  <div class="flip-entry-title">photo.JPG</div></a></div>
<div class="flip-entry" id="entry-1ccccccccccccccccc">
  <a href="https://drive.google.com/drive/folders/1ccccccccccccccccc">
  <div class="flip-entry-title">archive.pdf</div></a></div>
<div class="flip-entry" id="entry-1ddddddddddddddddd">
  <a href="https://drive.google.com/file/d/1ddddddddddddddddd/view">
  <div class="flip-entry-title">budget.xlsx</div></a></div>
<div class="flip-entry" id="entry-1eeeeeeeeeeeeeeeee">
  <a href="https://drive.google.com/file/d/1eeeeeeeeeeeeeeeee/view">
  <div class="flip-entry-list-icon"><img
    src="https://drive-thirdparty.googleusercontent.com/16/type/application/pdf" alt=""/></div>
  <div class="flip-entry-title">Sample PDF</div></a></div>
<div class="flip-entry" id="entry-1fffffffffffffffff">
  <a href="https://drive.google.com/file/d/1fffffffffffffffff/view">
  <div class="flip-entry-list-icon"><img
    src="https://drive-thirdparty.googleusercontent.com/16/type/image/jpeg" alt=""/></div>
  <div class="flip-entry-title">Sample image.png</div></a></div>
</div></body></html>
"""


def test_public_page_parser_extracts_files_and_skips_the_rest() -> None:
    listing = parse_public_folder_page(FOLDER, PUBLIC_PAGE, "public-page")
    assert listing.name == "Shared scans"
    assert [(f.id, f.name, f.kind, f.mime_type) for f in listing.files] == [
        ("1aaaaaaaaaaaaaaaaaa", "Contrat & annexe.pdf", FileKind.PDF, "application/pdf"),
        ("1bbbbbbbbbbbbbbbbbb", "photo.JPG", FileKind.IMAGE, "image/jpeg"),
        ("1fffffffffffffffff", "Sample image.png", FileKind.IMAGE, "image/jpeg"),
        ("1eeeeeeeeeeeeeeeee", "Sample PDF", FileKind.PDF, "application/pdf"),
    ]
    assert all(f.size is None for f in listing.files)
    assert listing.skipped == 1  # budget.xlsx; the folder entry is reported as a subfolder
    assert [(sub.id, sub.name) for sub in listing.subfolders] == [
        ("1ccccccccccccccccc", "archive.pdf")
    ]


def test_public_page_without_entries_is_treated_as_not_found() -> None:
    with pytest.raises(DriveError) as info:
        parse_public_folder_page(FOLDER, "<html><body>Sign in</body></html>", "public-page")
    assert info.value.kind == "not_found"


async def test_public_strategy_lists_through_http() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["id"] == FOLDER
        return httpx.Response(200, text=PUBLIC_PAGE)

    listing = await public_client(handler).list_folder(FOLDER)
    assert listing.source == "public-page" and len(listing.files) == 4
    assert json.loads(json.dumps(listing.files[0].to_dict()))["kind"] == "pdf"
