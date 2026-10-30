from __future__ import annotations

from urllib.parse import quote, unquote, urlsplit

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ROLE_RANK, Role, User, effective_role


def login_user(request: Request, user: User) -> None:
    request.session["user_id"] = user.id


def logout_user(request: Request) -> None:
    request.session.clear()


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    user: User | None = None
    uid = request.session.get("user_id")
    if uid is not None:
        candidate = db.scalar(select(User).where(User.id == uid))
        if candidate is not None and candidate.is_active:
            user = candidate
    # Stash for the template layer: the Jinja context processor in app.templating
    # reads request.state.current_user so routes don't have to pass it explicitly.
    request.state.current_user = user
    return user


# Paths that must remain reachable even when the user still owes a password
# change, otherwise the must_change_password enforcement would redirect-loop.
_PASSWORD_CHANGE_EXEMPT_PATHS = frozenset({"/account/password", "/logout"})


def safe_next(value: str | None) -> str:
    """Accept local destinations only, including after URL decoding by a browser."""
    if not value or len(value) > 2048:
        return "/"
    decoded = value
    for _ in range(5):
        if (
            not decoded.startswith("/")
            or decoded.startswith("//")
            or "\\" in decoded
            or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
        ):
            return "/"
        try:
            parts = urlsplit(decoded)
        except ValueError:
            return "/"
        if parts.scheme or parts.netloc or any(p in {".", ".."} for p in parts.path.split("/")):
            return "/"
        path = parts.path.rstrip("/")
        if path in {
            "/login",
            "/logout",
            "/account/password",
            "/auth/oidc/callback",
        } or path.startswith("/login/"):
            return "/"
        expanded = unquote(decoded)
        if expanded == decoded:
            return value
        decoded = expanded
    return "/"


def requested_destination(request: Request) -> str:
    target = request.url.path
    if request.url.query:
        target += "?" + request.url.query
    return safe_next(target)


def _redirect(request: Request, location: str) -> None:
    # For HTMX requests, a 303 body would get swapped into the target element; use the
    # HX-Redirect header so the browser does a full-page redirect instead.
    if request.headers.get("HX-Request"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, headers={"HX-Redirect": location}
        )
    raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": location})


def require_user(request: Request, user: User | None = Depends(get_current_user)) -> User:
    if user is None:
        target = requested_destination(request) if request.method == "GET" else "/"
        location = "/login" if target == "/" else "/login?next=" + quote(target, safe="")
        _redirect(request, location)
    if user.must_change_password and request.url.path not in _PASSWORD_CHANGE_EXEMPT_PATHS:
        if request.method == "GET":
            request.session["login_next"] = requested_destination(request)
        _redirect(request, "/account/password")
    return user


def require_role(min_role: Role):
    def _dep(user: User = Depends(require_user)) -> User:
        role = effective_role(user)
        if role is None or ROLE_RANK[role] < ROLE_RANK[min_role]:
            raise HTTPException(status_code=403, detail="Forbidden")
        return user

    return _dep
