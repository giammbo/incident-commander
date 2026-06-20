import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import login_user
from app.config import get_settings
from app.db import get_db
from app.models import Group, Role, User  # noqa: F401
from app.services.google import SSO_SCOPES, authorize_url, exchange_code, verify_id_token
from app.services.users import _get_or_create_group  # role→group helper
from app.settings_store import google_settings, sso_settings

router = APIRouter()


def _sso_ready(google, sso) -> bool:
    return bool(google.enabled and google.client_id and google.client_secret and sso.sso_enabled)


@router.get("/login/google")
def login_google(request: Request, db: Session = Depends(get_db)):
    google, sso = google_settings(db), sso_settings(db)
    if not _sso_ready(google, sso):
        request.session["flash"] = "Google sign-in is not enabled."
        return RedirectResponse("/login", status_code=303)
    state = secrets.token_urlsafe(24)
    request.session["google_oauth_state"] = state
    redirect_uri = f"{get_settings().base_url.rstrip('/')}/auth/google/callback"
    return RedirectResponse(
        authorize_url(
            client_id=google.client_id, redirect_uri=redirect_uri, state=state, scopes=SSO_SCOPES
        ),
        status_code=307,
    )


@router.get("/auth/google/callback")
def google_callback(
    request: Request, code: str = "", state: str = "", db: Session = Depends(get_db)
):
    expected = request.session.pop("google_oauth_state", None)
    google, sso = google_settings(db), sso_settings(db)
    if not state or state != expected or not code or not _sso_ready(google, sso):
        request.session["flash"] = "Google sign-in failed."
        return RedirectResponse("/login", status_code=303)
    redirect_uri = f"{get_settings().base_url.rstrip('/')}/auth/google/callback"
    try:
        tokens = exchange_code(
            client_id=google.client_id,
            client_secret=google.client_secret,
            code=code,
            redirect_uri=redirect_uri,
        )
        claims = verify_id_token(tokens["id_token"], client_id=google.client_id)
    except Exception:  # noqa: BLE001
        request.session["flash"] = "Google sign-in failed."
        return RedirectResponse("/login", status_code=303)

    email = (claims.get("email") or "").strip().lower()
    hd = claims.get("hd")
    allowed = [d.strip().lower() for d in (sso.allowed_domains or "").split(",") if d.strip()]
    if not claims.get("email_verified") or not hd or hd.lower() not in allowed:
        request.session["flash"] = "Your Google account is not permitted to sign in here."
        return RedirectResponse("/login", status_code=303)

    sub = claims.get("sub")
    user = db.scalar(select(User).where(User.google_sub == sub))
    if user is None:
        by_email = db.scalar(select(User).where(User.email == email))
        if by_email is not None and (by_email.password_hash or by_email.is_protected_admin):
            # Never let SSO silently take over an existing password/admin account by matching
            # email — that would be an account-takeover vector for anyone in the trusted domain.
            request.session["flash"] = (
                "An account with this email already exists — sign in with your password."
            )
            return RedirectResponse("/login", status_code=303)
        user = by_email
    if user is None:
        user = User(email=email, name=claims.get("name") or email, is_active=True, google_sub=sub)
        group = _get_or_create_group(db, sso.auto_provision_role)
        user.groups = [group]
        db.add(user)
    else:
        user.google_sub = sub
        user.is_active = True
    db.commit()
    login_user(request, user)
    return RedirectResponse("/", status_code=303)
