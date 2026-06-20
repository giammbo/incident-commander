from __future__ import annotations

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
    uid = request.session.get("user_id")
    if uid is None:
        return None
    user = db.scalar(select(User).where(User.id == uid))
    if user is None or not user.is_active:
        return None
    return user


# Paths that must remain reachable even when the user still owes a password
# change, otherwise the must_change_password enforcement would redirect-loop.
_PASSWORD_CHANGE_EXEMPT_PATHS = frozenset({"/account/password", "/logout"})


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
        _redirect(request, "/login")
    if user.must_change_password and request.url.path not in _PASSWORD_CHANGE_EXEMPT_PATHS:
        _redirect(request, "/account/password")
    return user


def require_role(min_role: Role):
    def _dep(user: User = Depends(require_user)) -> User:
        role = effective_role(user)
        if role is None or ROLE_RANK[role] < ROLE_RANK[min_role]:
            raise HTTPException(status_code=403, detail="Forbidden")
        return user

    return _dep
