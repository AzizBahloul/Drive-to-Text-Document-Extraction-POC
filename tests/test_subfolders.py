"""Subfolders: documents in nested folders are listed, within depth and folder limits."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.drive.client import DriveClient, FileKind, FolderListing, Subfolder
from app.drive.errors import DriveError
from app.main import create_app
from tests.conftest import drive_file

ROOT = "1RootFolderIdAbCdEfGhIjKlMn"


class TreeDrive(DriveClient):
    """A folder tree in memory: {folder id: (files, subfolders)}. Ids starting 'locked' fail."""

    mode = "fake"

    def __init__(self, tree: dict[str, tuple[list, list[Subfolder]]]) -> None:
        self.tree = tree
        self.reads: list[str] = []

    async def list_folder(self, folder_id: str, resource_key: str | None = None) -> FolderListing:
        self.reads.append(folder_id)
        if folder_id.startswith("locked"):
            raise DriveError("not_found", "This folder does not exist or is not shared publicly.")
        files, subfolders = self.tree[folder_id]
        return FolderListing(folder_id, "resumes", tuple(files), "fake", 0, tuple(subfolders))


def resumes_tree() -> dict:
    """The shape that went unnoticed: nothing at the top, the PDF one level down."""
    return {
        ROOT: ([], [Subfolder("english", "English"), Subfolder("french", "French")]),
        "english": (
            [drive_file("cv.pdf", FileKind.PDF, file_id="cv-en")],
            [Subfolder("archive", "archive")],
        ),
        "french": ([drive_file("cv.pdf", FileKind.PDF, file_id="cv-fr")], []),
        "archive": ([drive_file("old.png", FileKind.IMAGE, file_id="old")], []),
    }


async def test_documents_in_subfolders_are_listed_with_their_path() -> None:
    listing = await TreeDrive(resumes_tree()).list_tree(ROOT)
    assert [(f.folder, f.name) for f in listing.files] == [
        ("English", "cv.pdf"),
        ("English/archive", "old.png"),
        ("French", "cv.pdf"),
    ]
    assert listing.folders_scanned == 4 and listing.folders_skipped == 0
    assert listing.files[0].to_dict()["folder"] == "English"


async def test_top_level_files_come_first() -> None:
    tree = resumes_tree()
    tree[ROOT] = ([drive_file("zz-top.pdf", FileKind.PDF)], tree[ROOT][1])
    listing = await TreeDrive(tree).list_tree(ROOT)
    assert (listing.files[0].folder, listing.files[0].name) == ("", "zz-top.pdf")


async def test_depth_limit_leaves_deeper_folders_out() -> None:
    drive = TreeDrive(resumes_tree())
    listing = await drive.list_tree(ROOT, depth=1)
    assert [f.id for f in listing.files] == ["cv-en", "cv-fr"]
    assert listing.folders_skipped == 1 and "archive" not in drive.reads


async def test_depth_zero_is_the_old_top_level_only_behaviour() -> None:
    listing = await TreeDrive(resumes_tree()).list_tree(ROOT, depth=0)
    assert listing.files == () and listing.folders_scanned == 1 and listing.folders_skipped == 2


async def test_folder_limit_caps_the_number_of_reads() -> None:
    drive = TreeDrive(resumes_tree())
    listing = await drive.list_tree(ROOT, max_folders=2)
    assert len(drive.reads) == 2
    assert listing.folders_scanned == 2 and listing.folders_skipped == 2  # French, archive


async def test_a_folder_reachable_twice_is_read_once() -> None:
    tree = resumes_tree()
    # Drive can show the same folder in two places; a shortcut back to the root is a loop.
    tree["french"] = ([], [Subfolder("english", "English again"), Subfolder(ROOT, "loop")])
    drive = TreeDrive(tree)
    listing = await drive.list_tree(ROOT, depth=5)
    assert sorted(drive.reads) == sorted([ROOT, "english", "french", "archive"])
    assert [f.id for f in listing.files] == ["cv-en", "old"]


async def test_an_unreadable_subfolder_does_not_fail_the_listing() -> None:
    tree = resumes_tree()
    tree[ROOT] = ([], [Subfolder("english", "English"), Subfolder("locked-1", "Private")])
    listing = await TreeDrive(tree).list_tree(ROOT)
    assert [f.id for f in listing.files] == ["cv-en", "old"]
    assert listing.folders_skipped == 1


async def test_the_shared_folder_itself_must_be_readable() -> None:
    with pytest.raises(DriveError):
        await TreeDrive({}).list_tree("locked-root")


async def test_public_page_mode_walks_real_nested_pages() -> None:
    def entry(entry_id: str, title: str, href: str) -> str:
        return (
            f'<div class="flip-entry" id="entry-{entry_id}"><a href="{href}">'
            f'<div class="flip-entry-title">{title}</div></a></div>'
        )

    pages = {
        ROOT: entry(
            "1englishfolderid0000",
            "English",
            "https://drive.google.com/drive/folders/1englishfolderid0000?resourcekey=0-key_1",
        ),
        "1englishfolderid0000": entry(
            "1cvfileid0000000000", "cv.pdf", "https://drive.google.com/file/d/1cvfileid/view"
        ),
    }
    seen_keys: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        folder = request.url.params["id"]
        seen_keys[folder] = request.url.params.get("resourcekey")
        body = (
            f'<html><title>x - Google Drive</title><div class="flip-entries">{pages[folder]}</div>'
        )
        return httpx.Response(200, text=body)

    client = DriveClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    listing = await client.list_tree(ROOT)
    assert [(f.folder, f.name) for f in listing.files] == [("English", "cv.pdf")]
    assert seen_keys["1englishfolderid0000"] == "0-key_1"


def test_folder_endpoint_reports_paths_and_folder_counts() -> None:
    settings = Settings(google_api_key=None, allowed_hosts="testserver", _env_file=None)
    app = create_app(settings=settings, drive=TreeDrive(resumes_tree()))
    with TestClient(app) as client:
        body = client.post(
            "/api/folders", json={"url": f"https://drive.google.com/drive/folders/{ROOT}"}
        ).json()
    assert [(f["folder"], f["name"]) for f in body["files"]] == [
        ("English", "cv.pdf"),
        ("English/archive", "old.png"),
        ("French", "cv.pdf"),
    ]
    assert body["folders_scanned"] == 4 and body["folders_skipped"] == 0
