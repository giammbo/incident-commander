from __future__ import annotations

from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.config import get_settings
from app.errors import browser_error_response, wants_html_error

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
# Public machine-to-machine endpoints, authenticated by their own signatures/tokens
# (inbound alert tokens, Slack request signing, Twilio request signing).
_EXEMPT_PREFIXES = ("/ingest/", "/oncall/twilio/")
_EXEMPT_PATHS = {"/slack/interactions"}


def _host(value: str | None) -> str:
    """Reduce an Origin/Referer URL or a bare Host header to a lowercase host[:port]."""
    if not value:
        return ""
    return urlparse(value if "//" in value else "//" + value).netloc.lower()


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if (
            request.method in _SAFE_METHODS
            or path in _EXEMPT_PATHS
            or path.startswith(_EXEMPT_PREFIXES)
        ):
            return await call_next(request)
        source = request.headers.get("origin") or request.headers.get("referer")
        src_host = _host(source)
        if src_host:  # browsers always send Origin on cross-site unsafe requests;
            allowed = {_host(get_settings().base_url), _host(request.headers.get("host"))}
            allowed.discard("")
            if src_host not in allowed:
                if wants_html_error(request):
                    return browser_error_response(request, 403)
                return JSONResponse({"error": "CSRF origin check failed"}, status_code=403)
        return await call_next(request)
