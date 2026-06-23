from __future__ import annotations

from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.config import get_settings

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def _host(value: str | None) -> str:
    """Reduce an Origin/Referer URL or a bare Host header to a lowercase host[:port]."""
    if not value:
        return ""
    return urlparse(value if "//" in value else "//" + value).netloc.lower()


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.method in _SAFE_METHODS or request.url.path.startswith("/ingest/"):
            return await call_next(request)
        source = request.headers.get("origin") or request.headers.get("referer")
        src_host = _host(source)
        if src_host:  # browsers always send Origin on cross-site unsafe requests;
            allowed = {_host(get_settings().base_url), _host(request.headers.get("host"))}
            allowed.discard("")
            if src_host not in allowed:
                return JSONResponse({"error": "CSRF origin check failed"}, status_code=403)
        return await call_next(request)
