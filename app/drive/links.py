"""Turn the many shapes of a Google Drive folder URL into a folder id."""

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from app.drive.errors import DriveError

_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{10,}$")
_FOLDER_PATH = re.compile(r"/(?:drive/(?:u/\d+/|mobile/)?folders|folderview)/([A-Za-z0-9_-]+)")
_FILE_PATH = re.compile(r"/file/d/")
_ALLOWED_HOSTS = {"drive.google.com", "docs.google.com"}
_RESOURCE_KEY = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


@dataclass(frozen=True)
class DriveFolderLink:
    folder_id: str
    resource_key: str | None = None

    @property
    def url(self) -> str:
        return f"https://drive.google.com/drive/folders/{self.folder_id}"


def parse_folder_link(raw: str) -> DriveFolderLink:
    """Accept a folder URL (or a bare folder id) and return the folder id.

    Supported shapes include ``/drive/folders/<id>``, ``/drive/u/0/folders/<id>``,
    ``/open?id=<id>``, ``/embeddedfolderview?id=<id>`` and any of these with
    ``?usp=sharing`` or ``?resourcekey=`` suffixes.
    """
    value = (raw or "").strip()
    if not value:
        raise DriveError("invalid_link", "Paste a Google Drive folder link to get started.")

    if _ID_PATTERN.match(value):
        return DriveFolderLink(folder_id=value)

    if "://" not in value:
        value = "https://" + value
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
    except ValueError as exc:
        raise DriveError("invalid_link", "That Google Drive URL is malformed.") from exc
    if host not in _ALLOWED_HOSTS:
        raise DriveError(
            "invalid_link",
            "That is not a Google Drive link. It should start with drive.google.com.",
        )

    if _FILE_PATH.search(parsed.path):
        raise DriveError(
            "invalid_link",
            "That link points to a single file. Share the folder that contains it instead.",
        )

    query = parse_qs(parsed.query)
    resource_key = next(iter(query.get("resourcekey", [])), None)
    if resource_key is not None and not _RESOURCE_KEY.match(resource_key):
        # It is sent upstream as a header value; refuse anything but the documented alphabet.
        raise DriveError("invalid_link", "The resourcekey in that link is not valid.")

    match = _FOLDER_PATH.search(parsed.path)
    if match:
        return DriveFolderLink(folder_id=match.group(1), resource_key=resource_key)

    candidate = (query.get("id") or [""])[0]
    if candidate and _ID_PATTERN.match(candidate):
        return DriveFolderLink(folder_id=candidate, resource_key=resource_key)

    raise DriveError(
        "invalid_link",
        "Could not find a folder id in that link. "
        "Expected something like drive.google.com/drive/folders/<id>.",
    )
