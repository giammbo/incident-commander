"""Recoverable browser errors with the original FastAPI contract for machine clients."""

from collections.abc import Mapping

from fastapi import Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from app.templating import templates

# These endpoints speak JSON, signatures, callback protocols or downloadable artifacts,
# independently of a caller's Accept header. Browser form/ack pages stay eligible.
_MACHINE_PREFIXES = ("/api/", "/ingest/", "/oncall/twilio/", "/static/")
_MACHINE_PATHS = {
    "/api",
    "/healthz",
    "/openapi.json",
    "/slack/interactions",
    "/auth/oidc/callback",
    "/connections/slack/callback",
    "/account/notifications/push",
    "/oncall-sw.js",
}
_COPY = {
    403: ("Access unavailable", "You cannot complete this action with your current access."),
    404: (
        "Page unavailable",
        "This page is unavailable. Return to incidents or sign in to continue.",
    ),
    422: (
        "Check your request",
        "Some details are missing or invalid. Review the form fields or open the page again.",
    ),
}


def _accept_quality(accept: str, media_type: str) -> float:
    """Only an explicit media type opts in; */* is the usual API client default."""
    quality = 0.0
    for item in accept.lower().split(","):
        kind, *parameters = item.strip().split(";")
        if kind.strip() != media_type:
            continue
        value = 1.0
        for parameter in parameters:
            key, _, raw = parameter.strip().partition("=")
            if key == "q":
                try:
                    value = float(raw)
                except ValueError:
                    value = 0.0
        quality = max(quality, value if 0 <= value <= 1 else 0.0)
    return quality


def wants_html_error(request: Request) -> bool:
    path = request.url.path
    if (
        path in _MACHINE_PATHS
        or path.startswith(_MACHINE_PREFIXES)
        or path.endswith((".json", ".md"))
        or request.headers.get("content-type", "").split(";", 1)[0].endswith("json")
    ):
        return False
    accept = request.headers.get("accept", "")
    html = _accept_quality(accept, "text/html")
    json = _accept_quality(accept, "application/json")
    if json > html:
        return False
    if request.headers.get("hx-request", "").lower() == "true":
        return True
    return html > 0


def browser_error_response(
    request: Request, status_code: int, headers: Mapping[str, str] | None = None
):
    """Render fixed copy only: no exception detail, submitted value, URL or Referer."""
    response_headers = dict(headers or {})
    response_headers.setdefault("Cache-Control", "no-store")
    vary = next((value for key, value in response_headers.items() if key.lower() == "vary"), "")
    response_headers["Vary"] = ", ".join(filter(None, [vary, "Accept", "HX-Request"]))
    fragment = request.headers.get("hx-request", "").lower() == "true"
    if fragment:
        response_headers.update({"HX-Retarget": "#global-error", "HX-Reswap": "innerHTML"})
    title, message = _COPY[status_code]
    # Error handling must not lazily query expired ORM objects or assume that the
    # outer CSRF middleware has run SessionMiddleware. Use only already loaded data.
    user = getattr(request.state, "current_user", None)
    email = vars(user).get("email") if user is not None else None
    return templates.TemplateResponse(
        request,
        "partials/error.html" if fragment else "error.html",
        {
            "error_title": title,
            "error_message": message,
            "error_status": status_code,
            "error_email": email,
        },
        status_code=status_code,
        headers=response_headers,
    )


async def browser_http_exception_handler(request: Request, exc: HTTPException):
    # Authentication redirects, including HTMX's 401 + HX-Redirect, keep their
    # original status, headers and body. Never swap a recovery fragment over them.
    redirect = any(key.lower() in {"location", "hx-redirect"} for key in (exc.headers or {}))
    if exc.status_code in _COPY and not redirect and wants_html_error(request):
        return browser_error_response(request, exc.status_code, exc.headers)
    return await http_exception_handler(request, exc)


async def browser_validation_exception_handler(request: Request, exc: RequestValidationError):
    if wants_html_error(request):
        return browser_error_response(request, 422)
    return await request_validation_exception_handler(request, exc)
