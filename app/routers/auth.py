from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import login_user, logout_user, safe_next
from app.db import get_db
from app.services.users import authenticate_local
from app.settings_store import sso_settings
from app.templating import templates

router = APIRouter()


def _sso_context(db: Session) -> dict:
    sso = sso_settings(db)
    return {
        "sso_enabled": bool(sso.sso_enabled and sso.issuer and sso.client_id and sso.client_secret),
        "sso_display_name": sso.display_name,
    }


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    target = safe_next(request.query_params.get("next", request.session.get("login_next")))
    request.session["login_next"] = target
    return templates.TemplateResponse(
        request, "login.html", {"error": None, "next": target, "email": "", **_sso_context(db)}
    )


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str | None = Form(None),
    db: Session = Depends(get_db),
):
    target = safe_next(next if next is not None else request.session.get("login_next"))
    request.session["login_next"] = target
    user = authenticate_local(db, email, password)
    if user is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid credentials", "next": target, "email": email, **_sso_context(db)},
            status_code=401,
        )
    sso = sso_settings(db)
    if sso.sso_enabled and not sso.allow_local_login and not user.is_protected_admin:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": "Local login is disabled — sign in with SSO.",
                **_sso_context(db),
                "next": target,
                "email": email,
            },
            status_code=401,
        )
    login_user(request, user)
    user.last_login_at = datetime.now(UTC)
    db.add(user)
    db.commit()
    if user.must_change_password:
        return RedirectResponse("/account/password", status_code=303)
    request.session.pop("login_next", None)
    return RedirectResponse(target, status_code=303)


@router.get("/logout")
def logout(request: Request):
    logout_user(request)
    return RedirectResponse("/login", status_code=303)
