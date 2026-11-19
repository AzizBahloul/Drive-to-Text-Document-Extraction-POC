"""Safety limits for hostile or oversized files, and HTTP-level hardening."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.api import routes
from app.batch import manager as manager_module
from app.batch.manager import BatchManager, TooManyBatchesError
from app.config import Settings
from app.drive.client import FileKind
from app.drive.errors import DriveError
from app.drive.links import parse_folder_link
from app.extraction import ocr, pdf_parser, router
from app.extraction.pdf_parser import PdfOpenError
from app.extraction.router import ExtractionOptions, UnsupportedFileError, extract_file
from app.main import create_app
from tests.conftest import build_text_image, build_text_pdf, drive_file
from tests.test_api import FakeDrive as ApiFakeDrive
from tests.test_batch import FakeDrive as BatchFakeDrive

OPTIONS = ExtractionOptions(ocr_language="eng", render_dpi=72, min_chars_per_page=25)


@pytest.fixture
def two_page_scan(tmp_path: Path) -> Path:
    path = tmp_path / "two-pages.pdf"
    first = build_text_image("PAGE ONE")
    first.save(path, "PDF", resolution=150, save_all=True, append_images=[first.copy()])
    return path


@pytest.fixture
def stub_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router.ocr_engine, "ocr_image", lambda image, language, **_: "OCR TEXT")


# ----- pixel budget ----------------------------------------------------------------


def test_render_scale_shrinks_pages_that_would_exceed_the_budget() -> None:
    scale = pdf_parser.render_scale(14400, 14400, dpi=200, max_pixels=60_000_000)
    assert (14400 * scale) ** 2 <= 60_000_000 * 1.0001
    assert pdf_parser.render_scale(612, 792, dpi=200, max_pixels=60_000_000) == pytest.approx(
        200 / 72
    )


def test_a_giant_blank_page_is_rendered_within_the_budget(tmp_path: Path) -> None:
    path = tmp_path / "giant.pdf"
    path.write_bytes(build_text_pdf([], media_box=(14400, 14400)))
    ((_, _, image),) = pdf_parser.iter_page_sources(
        path, min_chars=25, dpi=200, max_pixels=1_000_000
    )
    width, height = image.size
    assert width * height <= 1_010_000


def test_prepare_image_never_upscales_past_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr, "MAX_PIXELS", 200_000)
    prepared = ocr.prepare_image(Image.new("L", (100, 1000), "white"))
    assert prepared.width * prepared.height <= 200_000


def test_an_oversized_image_is_refused_before_decoding(
    text_png: Path, monkeypatch: pytest.MonkeyPatch, stub_ocr
) -> None:
    monkeypatch.setattr(ocr, "MAX_PIXELS", 1000)
    with pytest.raises(UnsupportedFileError, match="pixels"):
        extract_file(text_png, FileKind.IMAGE, OPTIONS)


# ----- OCR timeouts, page cap, time limit -------------------------------------------


def test_tesseract_timeout_becomes_ocr_timeout_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(*_, **__):
        raise RuntimeError("Tesseract process timeout")

    monkeypatch.setattr(ocr.pytesseract, "image_to_string", slow)
    with pytest.raises(ocr.OcrTimeoutError):
        ocr.ocr_image(Image.new("L", (50, 50)), "eng", timeout=5)


def test_a_timed_out_page_is_left_empty_and_reported(
    scanned_pdf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def timeout(*_, **__):
        raise ocr.OcrTimeoutError("OCR took longer than 5 s and was stopped.")

    monkeypatch.setattr(router.ocr_engine, "ocr_image", timeout)
    result = extract_file(scanned_pdf, FileKind.PDF, OPTIONS)
    assert result.pages[0].text == ""
    assert any("timed out on 1 page" in warning for warning in result.warnings)


def test_a_timed_out_image_fails_with_a_clear_message(
    text_png: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def timeout(*_, **__):
        raise ocr.OcrTimeoutError("OCR took longer than 5 s and was stopped.")

    monkeypatch.setattr(router.ocr_engine, "ocr_image", timeout)
    with pytest.raises(ocr.OcrTimeoutError):
        extract_file(text_png, FileKind.IMAGE, OPTIONS)


def test_page_cap_stops_early_with_a_warning(two_page_scan: Path, stub_ocr) -> None:
    options = ExtractionOptions(ocr_language="eng", render_dpi=72, max_pages=1)
    result = extract_file(two_page_scan, FileKind.PDF, options)
    assert len(result.pages) == 1
    assert any("first 1 of 2 pages" in warning for warning in result.warnings)


def test_page_cap_equal_to_page_count_adds_no_warning(scanned_pdf: Path, stub_ocr) -> None:
    options = ExtractionOptions(ocr_language="eng", render_dpi=72, max_pages=1)
    result = extract_file(scanned_pdf, FileKind.PDF, options)
    assert not any("pages were processed" in warning for warning in result.warnings)


def test_time_limit_keeps_the_pages_done_so_far(two_page_scan: Path, stub_ocr) -> None:
    options = ExtractionOptions(ocr_language="eng", render_dpi=72, time_limit_s=1e-9)
    result = extract_file(two_page_scan, FileKind.PDF, options)
    assert len(result.pages) == 1 and result.pages[0].text == "OCR TEXT"
    assert any("time limit" in warning for warning in result.warnings)


# ----- error messages shown to the user ---------------------------------------------


def test_known_failures_are_shown_verbatim_and_the_rest_generically() -> None:
    describe = manager_module._describe
    assert describe(PdfOpenError("Not a readable PDF: bad xref")) == "Not a readable PDF: bad xref"
    leaked = describe(OSError("/tmp/drive-to-text-abc/secret.pdf: permission denied"))
    assert "/tmp" not in leaked and "Unexpected error" in leaked


# ----- batch manager bounds ---------------------------------------------------------


def _manager(**overrides) -> BatchManager:
    blobs = {"id-a_pdf": build_text_pdf(["A native PDF with enough characters to count."])}
    settings = Settings(google_api_key=None, _env_file=None, **overrides)
    return BatchManager(BatchFakeDrive(blobs), settings)


def _create(manager: BatchManager):
    return manager.create(
        folder_id="folder-1234567890",
        folder_url="https://drive.google.com/drive/folders/folder-1234567890",
        folder_name="Inbox",
        files=[drive_file("a.pdf", FileKind.PDF)],
        ocr_language="eng",
    )


async def test_too_many_running_batches_are_refused() -> None:
    manager = _manager(max_running_batches=1)
    first = _create(manager)
    with pytest.raises(TooManyBatchesError):
        _create(manager)
    await first.task
    await _create(manager).task  # a slot is free again once the first one finished


async def test_old_finished_batches_are_forgotten() -> None:
    manager = _manager(batch_retention=2)
    ids = []
    for _ in range(4):
        batch = _create(manager)
        await batch.task
        ids.append(batch.id)
    kept = [batch_id for batch_id in ids if batch_id in manager._batches]
    assert kept == ids[-2:]


# ----- links ------------------------------------------------------------------------


def test_resource_key_with_header_breaking_characters_is_refused() -> None:
    url = "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMn?resourcekey=0-a%0d%0aX-Evil:%201"
    with pytest.raises(DriveError) as info:
        parse_folder_link(url)
    assert info.value.kind == "invalid_link"


# ----- HTTP hardening ---------------------------------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(ocr, "installed_languages", lambda: ("eng",))
    settings = Settings(google_api_key=None, allowed_hosts="testserver", _env_file=None)
    with TestClient(create_app(settings=settings, drive=ApiFakeDrive())) as test_client:
        yield test_client


def test_unknown_host_header_is_rejected(client: TestClient) -> None:
    response = client.get("/api/capabilities", headers={"host": "attacker.example"})
    assert response.status_code == 400


def test_security_headers_are_set_on_the_page_and_the_api(client: TestClient) -> None:
    for path in ("/", "/api/capabilities"):
        headers = client.get(path).headers
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
    docs_policy = client.get("/api/docs").headers["content-security-policy"]
    assert "script-src 'self' https://cdn.jsdelivr.net;" in docs_policy
    assert "unsafe-inline" not in docs_policy and "frame-ancestors 'none'" in docs_policy
    assert "connect-src 'self'" in docs_policy  # a tampered script could not phone home
    assert (
        "frame-ancestors 'none'" in client.get("/openapi.json").headers["content-security-policy"]
    )


def test_batch_ids_outside_the_drive_alphabet_are_rejected(client: TestClient) -> None:
    folder = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"
    client.post("/api/folders", json={"url": folder})
    response = client.post("/api/batches", json={"folder_id": folder, "file_ids": ["../etc"]})
    assert response.status_code == 422


def test_folder_listing_cache_is_bounded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes, "MAX_CACHED_LISTINGS", 2)
    for suffix in "abc":
        assert client.post("/api/folders", json={"url": f"folder-id-{suffix}-0123"}).is_success
    assert list(client.app.state.listings) == ["folder-id-b-0123", "folder-id-c-0123"]


def test_only_one_api_reference_page_is_served(client: TestClient) -> None:
    # ReDoc and the OAuth2 helper are FastAPI defaults this app neither uses nor allows
    # under its CSP; served anyway, they render blank pages.
    assert client.get("/api/docs").status_code == 200
    for path in ("/redoc", "/docs/oauth2-redirect"):
        assert client.get(path).status_code == 404


def test_api_reference_lists_every_status_the_api_returns(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    def codes(path: str, method: str) -> set[str]:
        return set(paths[path][method]["responses"])

    assert {"400", "403", "404", "500", "502"} <= codes("/api/folders", "post")
    assert {"400", "429", "503"} <= codes("/api/batches", "post")
    for path in ("/api/batches/{batch_id}", "/api/batches/{batch_id}/events"):
        assert "404" in codes(path, "get")
    assert "404" in codes("/api/batches/{batch_id}/export", "get")
    folder_403 = paths["/api/folders"]["post"]["responses"]["403"]
    assert folder_403["description"] != "Additional Response"
    assert "ErrorResponse" in str(folder_403["content"])


def test_api_reference_page_pins_its_cdn_assets_and_has_no_inline_script(
    client: TestClient,
) -> None:
    html = client.get("/api/docs").text
    assert '<html lang="en">' in html
    cdn_tags = [tag for tag in re.findall(r"<(?:script|link)\b[^>]*>", html) if "cdn." in tag]
    assert len(cdn_tags) == 2
    for tag in cdn_tags:
        assert "@5." in tag and 'integrity="sha384-' in tag and "crossorigin" in tag
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline script found"


# ----- not found --------------------------------------------------------------------

BROWSER = {"accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


def test_a_browser_gets_a_page_with_a_way_back_for_unknown_urls(client: TestClient) -> None:
    response = client.get("/this-page-does-not-exist", headers=BROWSER)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert '<html lang="en">' in response.text and 'href="/"' in response.text
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_api_clients_and_api_paths_keep_json_not_found(client: TestClient) -> None:
    assert client.get("/this-page-does-not-exist").json() == {"detail": "Not Found"}
    for path in ("/api/nope", "/api/batches/unknown", "/static/nope.js"):
        response = client.get(path, headers=BROWSER)
        assert response.status_code == 404
        assert response.headers["content-type"] == "application/json", path
    assert client.get("/api/batches/unknown").json() == {"detail": "Unknown batch."}


# ----- CSRF and rate limiting -------------------------------------------------------


def test_cross_origin_mutating_request_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/folders",
        json={"url": "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"},
        headers={"Origin": "https://malicious.evil.com"},
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "Cross-origin request forbidden."}


def test_same_origin_mutating_request_is_accepted(client: TestClient) -> None:
    response = client.post(
        "/api/folders",
        json={"url": "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"},
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 200


def test_virus_scan_warning_confirmation_url_extracted() -> None:
    from app.drive.client import _extract_confirm_download_url

    html = (
        '<a id="uc-download-link" '
        'href="/uc?export=download&amp;id=123&amp;confirm=t_xyz">Download</a>'
    )
    url = _extract_confirm_download_url(
        "https://drive.usercontent.google.com/download?id=123", html
    )
    assert url == "https://drive.google.com/uc?export=download&id=123&confirm=t_xyz"


def test_corrupted_page_in_pdf_is_skipped_gracefully(
    two_page_scan: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    texts = iter([RuntimeError("Corrupt page"), "Second page is readable"])

    def read_page(page):
        text = next(texts)
        if isinstance(text, Exception):
            raise text
        return text

    monkeypatch.setattr(pdf_parser, "page_text", read_page)
    sources = list(pdf_parser.iter_page_sources(two_page_scan, min_chars=5, dpi=72))
    assert sources == [(1, "", None), (2, "Second page is readable", None)]


def test_hsts_header_set_over_https(client: TestClient) -> None:
    # Starlette's TestClient uses ASGI transport; we can inject a custom scope
    # by wrapping the app to override the scheme in the HTTP scope.
    from starlette.types import ASGIApp, Receive, Scope, Send

    base_app: ASGIApp = client.app

    async def https_app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            scope = {**scope, "scheme": "https"}
        await base_app(scope, receive, send)

    from starlette.testclient import TestClient as _TC

    https_client = _TC(https_app)
    response = https_client.get("/")
    assert "strict-transport-security" in {k.lower() for k in response.headers}


async def test_cancel_finished_batch_is_a_noop() -> None:
    """cancel() on an already-finished batch returns it unchanged without error."""
    from app.batch.manager import BatchManager
    from app.config import Settings
    from app.drive.client import DriveFile
    from tests.conftest import build_text_pdf, drive_file

    class _FakeDrive:
        mode = "fake"

        async def download(
            self, file: DriveFile, destination: Path, *, max_bytes, resource_key=None
        ):
            data = build_text_pdf(["Hello world, enough chars here."])
            destination.write_bytes(data)
            return len(data)

    settings = Settings(max_parallel_files=1, ocr_default_language="eng")
    bm = BatchManager(drive=_FakeDrive(), settings=settings)
    file = drive_file("good.pdf", FileKind.PDF)
    batch = bm.create(
        folder_id="folder1",
        folder_url="https://drive.google.com/drive/folders/folder1",
        folder_name="Test",
        files=[file],
        ocr_language="eng",
    )
    await batch.task
    assert batch.finished_at is not None
    # Cancelling a finished batch is a no-op — returns it unchanged.
    result = bm.cancel(batch.id)
    assert result.finished_at is not None
