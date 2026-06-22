import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import login_user
from app.config import get_settings
from app.db import get_db
from app.models import User
from app.services import oidc
from app.services.users import _get_or_create_group
from app.settings_store import sso_settings

router = APIRouter()


def _sso_ready(sso) -> bool:
    return bool(sso.sso_enabled and sso.issuer and sso.client_id and sso.client_secret)


def _redirect_uri() -> str:
    return f"{get_settings().base_url.rstrip('/')}/auth/oidc/callback"


def _domain_allowed(claims: dict, allowed_domains: str | None) -> bool:
    # reject only an explicit False; many IdPs (e.g. Entra) omit email_verified — do NOT use `not`
    if claims.get("email_verified") is False:
        return False
    allowed = [d.strip().lower() for d in (allowed_domains or "").split(",") if d.strip()]
    if not allowed:
        return True  # issuer is the trust boundary
    email = (claims.get("email") or "").strip().lower()
    hd = claims.get("hd")
    domain = hd.lower() if hd else (email.rsplit("@", 1)[-1] if "@" in email else "")
    return domain in allowed


@router.get("/login/oidc")
def login_oidc(request: Request, db: Session = Depends(get_db)):
    sso = sso_settings(db)
    if not _sso_ready(sso):
        request.session["flash"] = "Single sign-on is not enabled."
        return RedirectResponse("/login", status_code=303)
    try:
        conf = oidc.discover(sso.issuer)
    except Exception:  # noqa: BLE001
        request.session["flash"] = "Single sign-on is misconfigured (discovery failed)."
        return RedirectResponse("/login", status_code=303)
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    request.session["oidc_state"] = state
    request.session["oidc_nonce"] = nonce
    return RedirectResponse(
        oidc.authorize_url(
            authorization_endpoint=conf["authorization_endpoint"],
            client_id=sso.client_id,
            redirect_uri=_redirect_uri(),
            state=state,
            nonce=nonce,
        ),
        status_code=307,
    )


@router.get("/auth/oidc/callback")
def oidc_callback(request: Request, code: str = "", state: str = "", db: Session = Depends(get_db)):
    expected_state = request.session.pop("oidc_state", None)
    nonce = request.session.pop("oidc_nonce", None)
    sso = sso_settings(db)
    if not state or state != expected_state or not code or not _sso_ready(sso):
        request.session["flash"] = "Single sign-on failed."
        return RedirectResponse("/login", status_code=303)
    try:
        conf = oidc.discover(sso.issuer)
        tokens = oidc.exchange_code(
            token_endpoint=conf["token_endpoint"],
            client_id=sso.client_id,
            client_secret=sso.client_secret,
            code=code,
            redirect_uri=_redirect_uri(),
        )
        claims = oidc.verify_id_token(
            id_token=tokens["id_token"],
            jwks_uri=conf["jwks_uri"],
            issuer=sso.issuer,
            client_id=sso.client_id,
            nonce=nonce,
        )
    except Exception:  # noqa: BLE001
        request.session["flash"] = "Single sign-on failed."
        return RedirectResponse("/login", status_code=303)

    if not _domain_allowed(claims, sso.allowed_domains):
        request.session["flash"] = "Your account is not permitted to sign in here."
        return RedirectResponse("/login", status_code=303)

    email = (claims.get("email") or "").strip().lower()
    sub, iss = claims.get("sub"), sso.issuer
    user = db.scalar(select(User).where(User.oidc_issuer == iss, User.oidc_subject == sub))
    if user is None:
        by_email = db.scalar(select(User).where(User.email == email)) if email else None
        if by_email is not None and (by_email.password_hash or by_email.is_protected_admin):
            request.session["flash"] = (
                "An account with this email already exists — sign in with your password."
            )
            return RedirectResponse("/login", status_code=303)
        user = by_email
    if user is None:
        user = User(email=email, name=claims.get("name") or email, is_active=True)
        user.groups = [_get_or_create_group(db, sso.auto_provision_role)]
        db.add(user)
    user.oidc_issuer = iss
    user.oidc_subject = sub
    user.is_active = True
    db.commit()
    login_user(request, user)
    return RedirectResponse("/", status_code=303)
