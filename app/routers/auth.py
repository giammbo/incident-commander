from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import login_user, logout_user
from app.db import get_db
from app.services.users import authenticate_local
from app.settings_store import sso_settings
from app.templating import templates

router = APIRouter()


def _sso_context(db: Session) -> dict:
    sso = sso_settings(db)
    return {"sso_enabled": bool(sso.sso_enabled), "sso_display_name": sso.display_name}


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "login.html", {"error": None, **_sso_context(db)})


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = authenticate_local(db, email, password)
    if user is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid credentials", **_sso_context(db)},
            status_code=401,
        )
    sso = sso_settings(db)
    if sso.sso_enabled and not sso.allow_local_login and not user.is_protected_admin:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": "Local login is disabled — sign in with SSO.",
                "sso_enabled": True,
                "sso_display_name": sso.display_name,
            },
            status_code=401,
        )
    login_user(request, user)
    user.last_login_at = datetime.now(UTC)
    db.add(user)
    db.commit()
    if user.must_change_password:
        return RedirectResponse("/account/password", status_code=303)
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    logout_user(request)
    return RedirectResponse("/login", status_code=303)
