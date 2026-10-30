from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.config import get_settings
from app.db import get_db
from app.models import Group, Invite, Role, User, effective_role
from app.security.passwords import generate_password, hash_password
from app.services.email import is_smtp_configured, send_email
from app.services.invites import create_invite, lock_invite_user, revoke_invites
from app.services.users import create_user, deactivate_user, set_user_groups
from app.settings_store import smtp_settings
from app.templating import templates

router = APIRouter()
ROLE_LABELS = {
    Role.read_only: "Read-only — view incidents",
    Role.incident_commander: "Incident commander — manage incidents",
    Role.admin: "Administrator — manage settings and access",
}


def _page(request, user, db, *, error=None, form=None, status_code=200):
    members = list(db.scalars(select(User).order_by(User.id)))
    now = datetime.now(UTC)
    latest, pending = {}, {}
    for invite in db.scalars(select(Invite).order_by(Invite.id.desc())):
        latest.setdefault(invite.email, invite)
        if not invite.accepted_at and invite.expires_at > now:
            pending.setdefault(invite.email, invite)
    # A failed resend creates an expired token; the earlier delivered link still works.
    latest.update(pending)
    return templates.TemplateResponse(
        request,
        "users.html",
        {
            "current_user": user,
            "users": members,
            "groups": list(db.scalars(select(Group).order_by(Group.id))),
            "roles": [Role.read_only, Role.incident_commander, Role.admin],
            "role_labels": ROLE_LABELS,
            "effective_role": effective_role,
            "invites": latest,
            "now": now,
            "smtp_ready": is_smtp_configured(smtp_settings(db)),
            "error": error,
            "form": form or {"email": "", "name": "", "role": Role.read_only.value},
        },
        status_code=status_code,
    )


def _back(request: Request, message: str) -> RedirectResponse:
    request.session["flash"] = message
    return RedirectResponse("/users", status_code=303)


def _target(db: Session, user_id: int) -> User:
    target = db.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    return target


def _send_invitation(db: Session, target: User, admin: User, *, reset: bool = False) -> str:
    smtp = smtp_settings(db)
    if not is_smtp_configured(smtp):
        return "Email is not configured. Use Set temporary password to restore access."
    # Keep the account lock through delivery and finalization. Committing the
    # candidate first would let overlapping sends revoke each other's links.
    target = lock_invite_user(db, target.email)
    ttl = 72
    invite, token = create_invite(db, email=target.email, created_by=admin.id, ttl_hours=ttl)
    accept_url = f"{get_settings().base_url.rstrip('/')}/invite/accept?token={token}"
    ctx = {"accept_url": accept_url, "ttl_hours": ttl, "password_reset": reset}
    try:
        send_email(
            smtp,
            to=target.email,
            subject="Reset your Incident Commander password"
            if reset
            else "You're invited to Incident Commander",
            text_body=templates.get_template("email/invite.txt").render(ctx),
            html_body=templates.get_template("email/invite.html").render(ctx),
        )
    except Exception as exc:  # noqa: BLE001 — preserve credentials and previous links
        invite.expires_at = datetime.now(UTC)
        db.commit()
        return f"Invite email failed: {exc}. Existing access is unchanged; retry using Resend invitation."
    # A delivery failure must not invalidate the link a person already received.
    revoke_invites(db, target.email, except_id=invite.id)
    db.commit()
    label = "Password reset link" if reset else "Invitation"
    return f"{label} emailed to {target.email}. It expires in {ttl} hours."


@router.get("/users", response_class=HTMLResponse)
def users_page(
    request: Request,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    return _page(request, user, db)


@router.post("/users")
def users_create(
    request: Request,
    email: str = Form(...),
    name: str = Form(...),
    role: Role = Form(Role.read_only),
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    smtp = smtp_settings(db)
    norm_email = email.strip().lower()
    if db.scalar(select(User).where(User.email == norm_email)):
        return _back(
            request,
            f"A user with email {norm_email} already exists. Use the recovery actions below.",
        )
    temp = None if is_smtp_configured(smtp) else generate_password(12)
    try:
        target = create_user(
            db,
            email=email,
            name=name,
            role=role,
            password=temp,
            must_change_password=bool(temp),
        )
    except ValueError as exc:
        return _page(
            request,
            user,
            db,
            error=str(exc),
            form={"email": email, "name": name, "role": role.value},
            status_code=400,
        )
    if temp is None:
        target.is_active = False
        db.commit()
        return _back(request, _send_invitation(db, target, user))
    db.commit()
    return _back(
        request,
        f"Created {target.email}. Temporary password: {temp} — they must change it on first login.",
    )


@router.post("/users/{user_id}/resend-invite")
def users_resend_invite(
    request: Request,
    user_id: int,
    admin: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    target = _target(db, user_id)
    if target.is_protected_admin:
        return _back(request, "The protected administrator changes their password from Account.")
    return _back(request, _send_invitation(db, target, admin))


@router.post("/users/{user_id}/revoke-invite")
def users_revoke_invite(
    request: Request,
    user_id: int,
    admin: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    target = _target(db, user_id)
    revoke_invites(db, target.email)
    db.commit()
    return _back(
        request,
        f"Outstanding invitation links revoked for {target.email}. Existing credentials are unchanged.",
    )


@router.post("/users/{user_id}/reset-password")
def users_reset_password(
    request: Request,
    user_id: int,
    admin: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    target = _target(db, user_id)
    if target.is_protected_admin:
        return _back(request, "The protected administrator changes their password from Account.")
    if is_smtp_configured(smtp_settings(db)):
        return _back(request, _send_invitation(db, target, admin, reset=True))
    temp = generate_password(12)
    target.password_hash = hash_password(temp)
    target.must_change_password = True
    target.is_active = True
    revoke_invites(db, target.email)
    db.commit()
    return _back(
        request,
        f"Reset {target.email}. Temporary password: {temp} — they must change it on first login.",
    )


@router.post("/users/{user_id}/reactivate")
def users_reactivate(
    request: Request,
    user_id: int,
    admin: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    target = _target(db, user_id)
    if not target.password_hash and not target.oidc_subject:
        return _back(
            request,
            "This account has no sign-in credential. Resend the invitation or set a temporary password.",
        )
    target.is_active = True
    db.commit()
    return _back(request, f"Reactivated {target.email}.")


@router.post("/users/{user_id}/groups")
def users_set_groups(
    request: Request,
    user_id: int,
    group_ids: list[int] = Form(default=[]),
    admin: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    target = _target(db, user_id)
    try:
        set_user_groups(db, target, group_ids)
        db.commit()
    except ValueError:
        db.rollback()
        return _back(request, "Cannot remove the protected admin from the Admins group.")
    return _back(request, f"Groups updated for {target.email}.")


@router.post("/users/{user_id}/deactivate")
def users_deactivate(
    request: Request,
    user_id: int,
    admin: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    target = _target(db, user_id)
    try:
        deactivate_user(db, target)
        revoke_invites(db, target.email)
        db.commit()
    except ValueError:
        db.rollback()
        return _back(request, "The protected administrator cannot be deactivated.")
    return _back(request, f"Deactivated {target.email} and revoked outstanding invitation links.")
