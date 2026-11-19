"""Private Drive files and results must stay scoped to the connecting browser."""

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import build_text_pdf

TOKEN = "test-access-token"
ROOT = "test-private-root"
FOLDER = "application/vnd.google-apps.folder"
FILE = {"id": "private-file", "name": "private.pdf", "mimeType": "application/pdf"}


@pytest.fixture
def private_drive():
    calls = []

    def google(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.host == "www.googleapis.com"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        if request.url.params.get("alt") == "media":
            return httpx.Response(200, content=build_text_pdf(["Confidential test document."]))
        if request.url.path.endswith(f"/{ROOT}"):
            return httpx.Response(200, json={"id": ROOT, "name": "Private", "mimeType": FOLDER})
        if request.url.path.endswith("/private-file"):
            return httpx.Response(200, json=FILE)
        assert request.url.path.endswith("/files")
        if "'child-folder'" in request.url.params["q"]:
            return httpx.Response(200, json={"files": [FILE]})
        return httpx.Response(
            200, json={"files": [{"id": "child-folder", "name": "Child", "mimeType": FOLDER}]}
        )

    settings = Settings(
        google_api_key=None,
        google_oauth_client_id="test-client",
        google_picker_key="browser-key",
        google_project_number="100",
        allowed_hosts="testserver",
        _env_file=None,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        transport = httpx.AsyncClient(transport=httpx.MockTransport(google))
        app.state.http = transport
        try:
            yield client, calls
        finally:
            assert client.portal is not None
            client.portal.call(transport.aclose)


@pytest.mark.parametrize("mode", ["folder", "files"])
def test_private_batch_requires_same_browser(private_drive, mode):
    client, calls = private_drive
    response = client.post(
        "/api/drive/session",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={
            "mode": mode,
            "expires_in": 600,
            "files": [{"id": FILE["id"]}] if mode == "files" else [],
        },
    )
    assert response.status_code == 204
    assert "HttpOnly" in response.headers["Set-Cookie"]
    assert "SameSite=strict" in response.headers["Set-Cookie"]
    if mode == "folder":
        listing = client.post("/api/folders", json={"url": ROOT}).json()
        assert listing["files"][0]["folder"] == "Child"
        assert len(calls) == 3  # root metadata and two child listings
    else:
        listing = client.get("/api/drive/selection").json()
        assert client.post("/api/folders", json={"url": ROOT}).status_code == 403
    assert "access-token" not in str(listing)
    created = client.post(
        "/api/batches", json={"folder_id": listing["folder_id"], "file_ids": [FILE["id"]]}
    )
    assert created.status_code == 202
    batch = f"/api/batches/{created.json()['batch_id']}"
    with client.stream("GET", batch + "/events") as response:
        assert any('"status": "finished"' in line for line in response.iter_lines())
    response = client.get(batch + "/export")
    assert "Confidential test document." in response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert client.delete("/api/drive/session").status_code == 204
    for suffix in ("", "/events", "/export", "/files/private-file/preview"):
        assert client.get(batch + suffix).status_code == 404


def test_private_session_rejects_cross_origin_and_missing_token(private_drive):
    client, _ = private_drive
    body = {"mode": "folder", "expires_in": 600}
    assert client.post("/api/drive/session", json=body).status_code == 401
    assert (
        client.post(
            "/api/drive/session",
            json=body,
            headers={"Origin": "http://testserver:2222", "Authorization": f"Bearer {TOKEN}"},
        ).status_code
        == 403
    )
