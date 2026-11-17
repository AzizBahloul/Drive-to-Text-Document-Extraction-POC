"""Response hardening shared by every route."""

from __future__ import annotations

import time
from collections import defaultdict
from urllib.parse import urlparse

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
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


class RateLimiter:
    """Sliding-window in-memory rate limiter per client IP."""

    def __init__(self, max_requests: int = 120, window_seconds: float = 60.0) -> None:
        self.max_requests = max_requests
        self.window = window_seconds
        self.requests: dict[str, list[float]] = defaultdict(list)
        self._last_cleanup = time.monotonic()

    def is_allowed(self, client_ip: str) -> bool:
        now = time.monotonic()
        if now - self._last_cleanup > 30.0:
            self._cleanup(now)
        timestamps = self.requests[client_ip]
        cutoff = now - self.window
        self.requests[client_ip] = [t for t in timestamps if t > cutoff]
        if len(self.requests[client_ip]) >= self.max_requests:
            return False
        self.requests[client_ip].append(now)
        return True

    def _cleanup(self, now: float) -> None:
        self._last_cleanup = now
        cutoff = now - self.window
        stale = [ip for ip, times in self.requests.items() if not times or times[-1] <= cutoff]
        for ip in stale:
            del self.requests[ip]


class SecurityHeadersMiddleware:
    """Pure ASGI middleware providing security headers, CSRF origin checks, and rate limiting."""

    def __init__(self, app: ASGIApp, allowed_hosts: list[str] | None = None) -> None:
        self.app = app
        self.allowed_hosts = [h.lower() for h in (allowed_hosts or [])]
        self.limiter = RateLimiter(max_requests=120, window_seconds=60.0)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope["method"]
        path = scope["path"]

        # CSRF check: verify Origin header on mutating API calls
        if method in {"POST", "PUT", "DELETE", "PATCH"} and path.startswith("/api/"):
            headers_dict = dict(scope.get("headers", []))
            origin = headers_dict.get(b"origin")
            if origin:
                origin_str = origin.decode("latin1", errors="replace")
                parsed = urlparse(origin_str)
                origin_host = (parsed.hostname or "").lower()
                host_header = (
                    headers_dict.get(b"host", b"")
                    .decode("latin1", errors="replace")
                    .split(":")[0]
                    .lower()
                )
                if (
                    "*" not in self.allowed_hosts
                    and origin_host != host_header
                    and origin_host not in self.allowed_hosts
                ):
                    response = JSONResponse(
                        {"detail": "Cross-origin request forbidden."}, status_code=403
                    )
                    await response(scope, receive, send)
                    return

            # Rate limiting on mutating API requests
            client = scope.get("client")
            client_ip = client[0] if client else "unknown"
            if not self.limiter.is_allowed(client_ip):
                response = JSONResponse(
                    {"detail": "Rate limit exceeded. Please wait before submitting more requests."},
                    status_code=429,
                )
                await response(scope, receive, send)
                return

        policy = DOCS_CONTENT_SECURITY_POLICY if path == _DOCS_PATH else CONTENT_SECURITY_POLICY

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
                headers.setdefault("Content-Security-Policy", policy)
                # Only set HSTS when running over TLS; ignore for plain HTTP (localhost).
                if scope.get("scheme") == "https":
                    headers.setdefault(
                        "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
                    )
            await send(message)

        await self.app(scope, receive, send_with_headers)
