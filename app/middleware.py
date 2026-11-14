"""Response hardening shared by every route."""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# The page loads its own script and stylesheet plus Google Fonts; nothing else.
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com",
        "img-src 'self' data:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)
# The API reference (app/static/docs.html) also loads Swagger UI from jsDelivr, pinned and
# checked by Subresource Integrity. Its boot script is a local file, so no 'unsafe-inline';
# connect-src 'self' means even a tampered bundle could not send data anywhere else.
DOCS_CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self' https://cdn.jsdelivr.net",
        "style-src 'self' https://cdn.jsdelivr.net",
        "img-src 'self' data:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)
_DOCS_PATH = "/api/docs"


class SecurityHeadersMiddleware:
    """Pure ASGI middleware so the SSE stream is passed through untouched."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        policy = (
            DOCS_CONTENT_SECURITY_POLICY if scope["path"] == _DOCS_PATH else CONTENT_SECURITY_POLICY
        )

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Content-Security-Policy", policy)
            await send(message)

        await self.app(scope, receive, send_with_headers)
