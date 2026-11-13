import pytest

from app.drive.errors import DriveError
from app.drive.links import parse_folder_link

FOLDER_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"


@pytest.mark.parametrize(
    "url",
    [
        f"https://drive.google.com/drive/folders/{FOLDER_ID}",
        f"https://drive.google.com/drive/folders/{FOLDER_ID}?usp=sharing",
        f"https://drive.google.com/drive/folders/{FOLDER_ID}?usp=drive_link",
        f"https://drive.google.com/drive/u/0/folders/{FOLDER_ID}",
        f"https://drive.google.com/drive/u/2/folders/{FOLDER_ID}/",
        f"https://drive.google.com/drive/mobile/folders/{FOLDER_ID}",
        f"https://drive.google.com/open?id={FOLDER_ID}",
        f"https://drive.google.com/embeddedfolderview?id={FOLDER_ID}#list",
        f"drive.google.com/drive/folders/{FOLDER_ID}",
        f"  https://drive.google.com/drive/folders/{FOLDER_ID}  ",
        FOLDER_ID,
    ],
)
def test_accepts_every_known_folder_link_shape(url: str) -> None:
    assert parse_folder_link(url).folder_id == FOLDER_ID


def test_keeps_resource_key_when_present() -> None:
    link = parse_folder_link(
        f"https://drive.google.com/drive/folders/{FOLDER_ID}?resourcekey=0-abc123&usp=sharing"
    )
    assert link.folder_id == FOLDER_ID
    assert link.resource_key == "0-abc123"
    assert link.url.endswith(FOLDER_ID)


@pytest.mark.parametrize(
    ("url", "fragment"),
    [
        ("", "Paste"),
        ("https://example.com/drive/folders/abc", "not a Google Drive link"),
        ("https://drive.google.com/file/d/1abcdefghijklmnop/view", "single file"),
        ("https://drive.google.com/drive/my-drive", "Could not find a folder id"),
        ("https://docs.google.com/document/d/1abcdefghijklmnop/edit", "Could not find a folder id"),
    ],
)
def test_rejects_invalid_links_with_a_clear_message(url: str, fragment: str) -> None:
    with pytest.raises(DriveError) as info:
        parse_folder_link(url)
    assert info.value.kind == "invalid_link"
    assert fragment in info.value.message
    assert info.value.http_status == 400
