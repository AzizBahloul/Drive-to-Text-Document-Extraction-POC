"""FastAPI application factory and entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import __version__
from app.api.routes import ThumbnailCache, drive_error_response, router
from app.batch.manager import BatchManager
from app.config import Settings, get_settings
from app.drive.client import DriveClient
from app.drive.errors import DriveError
from app.extraction import ocr as ocr_engine
from app.middleware import SecurityHeadersMiddleware

STATIC_DIR = Path(__file__).parent / "static"
# Paths whose 404s are for programs, not people: they keep FastAPI's JSON body.
_MACHINE_PATHS = ("/api/", "/static/", "/openapi.json")


def wants_page(request: Request) -> bool:
    """True for a browser navigating to an unknown address (not an API call or an asset)."""
    return (
        request.method in {"GET", "HEAD"}
        and not request.url.path.startswith(_MACHINE_PATHS)
        and "text/html" in request.headers.get("accept", "")
    )


def create_app(settings: Settings | None = None, drive: DriveClient | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(levelname)s %(name)s: %(message)s"
    )
    # httpx logs every request URL at INFO; keep that noise (and any query string) out.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    ocr_engine.configure(settings.tesseract_cmd, settings.tessdata_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        http = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15), follow_redirects=True)
        app.state.drive = drive or DriveClient(http, settings.google_api_key)
        app.state.batches = BatchManager(app.state.drive, settings)
        app.state.listings = {}
        app.state.thumbnails = ThumbnailCache()
        logging.getLogger(__name__).info(
            "Drive mode: %s; OCR languages: %s",
            app.state.drive.mode,
            ", ".join(ocr_engine.installed_languages()) or "none",
        )
        try:
            yield
        finally:
            await http.aclose()

    # One API reference page, served from app/static/docs.html (below) rather than FastAPI's
    # built-in one: that page pins Swagger UI by hash, sets a language and passes contrast.
    # ReDoc and the OAuth2 redirect helper are off: there is no OAuth, and the CSP would
    # leave both blank.
    app = FastAPI(
        title="Drive-to-Text",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        swagger_ui_oauth2_redirect_url=None,
    )
    app.include_router(router)
    hosts = settings.allowed_host_list
    app.add_middleware(SecurityHeadersMiddleware, allowed_hosts=hosts)
    # Without a Host allowlist a DNS-rebinding page could drive the local API.
    if hosts and "*" not in hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    @app.exception_handler(DriveError)
    async def handle_drive_error(_: Request, exc: DriveError) -> JSONResponse:
        return drive_error_response(exc)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 404 and wants_page(request):
            return FileResponse(STATIC_DIR / "404.html", status_code=404)
        return await http_exception_handler(request, exc)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/docs", include_in_schema=False)
    async def api_docs() -> FileResponse:
        return FileResponse(STATIC_DIR / "docs.html")

    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app", host=settings.host, port=settings.port, log_level=settings.log_level
    )


if __name__ == "__main__":
    main()
