from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.config import get_settings
from app.db import get_db
from app.models import Group, Role, User, effective_role
from app.security.passwords import generate_password
from app.services.email import is_smtp_configured, send_email
from app.services.invites import create_invite
from app.services.users import create_user, deactivate_user, set_user_groups
from app.settings_store import smtp_settings
from app.templating import templates

router = APIRouter()


@router.get("/users", response_class=HTMLResponse)
def users_page(
    request: Request,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    users = list(db.scalars(select(User).order_by(User.id)))
    groups = list(db.scalars(select(Group).order_by(Group.id)))
    return templates.TemplateResponse(
        request,
        "users.html",
        {
            "current_user": user,
            "users": users,
            "groups": groups,
            "roles": list(Role),
            "effective_role": effective_role,
        },
    )


@router.post("/users")
def users_create(
    request: Request,
    email: str = Form(...),
    name: str = Form(...),
    role: Role = Form(...),
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    smtp = smtp_settings(db)
    norm_email = email.strip().lower()
    if db.scalar(select(User).where(User.email == norm_email)):
        request.session["flash"] = f"A user with email {norm_email} already exists."
        return RedirectResponse("/users", status_code=303)
    if is_smtp_configured(smtp):
        target = create_user(db, email=email, name=name, role=role, password=None)
        target.is_active = False
        ttl = 72
        _, token = create_invite(db, email=email, created_by=user.id, ttl_hours=ttl)
        db.commit()
        accept_url = f"{get_settings().base_url.rstrip('/')}/invite/accept?token={token}"
        ctx = {"accept_url": accept_url, "ttl_hours": ttl}
        try:
            send_email(
                smtp,
                to=target.email,
                subject="You're invited to Incident Commander",
                text_body=templates.get_template("email/invite.txt").render(ctx),
                html_body=templates.get_template("email/invite.html").render(ctx),
            )
            request.session["flash"] = f"Invitation emailed to {target.email}."
        except Exception as exc:  # noqa: BLE001 — surface delivery failure to the admin
            request.session["flash"] = f"User created but the invite email failed: {exc}"
        return RedirectResponse("/users", status_code=303)

    # Fallback: no SMTP — generate a temporary password shown once to the admin.
    temp = generate_password(12)
    create_user(db, email=email, name=name, role=role, password=temp, must_change_password=True)
    db.commit()
    # Pass the one-time temp password via the (signed) session, never the URL — keeps it out
    # of server access logs and the Referer header.
    request.session["flash"] = (
        f"Created {email}. Temporary password: {temp} — they must change it on first login."
    )
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/groups")
def users_set_groups(
    request: Request,
    user_id: int,
    group_ids: list[int] = Form(default=[]),
    admin: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    target = db.scalar(select(User).where(User.id == user_id))
    if target is None:
        return RedirectResponse("/users", status_code=303)
    try:
        set_user_groups(db, target, group_ids)
        db.commit()
    except ValueError:
        db.rollback()
        request.session["flash"] = "Cannot remove the protected admin from the Admins group."
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/deactivate")
def users_deactivate(
    user_id: int, admin: User = Depends(require_role(Role.admin)), db: Session = Depends(get_db)
):
    target = db.scalar(select(User).where(User.id == user_id))
    if target is not None:
        try:
            deactivate_user(db, target)
            db.commit()
        except ValueError:
            db.rollback()
    return RedirectResponse("/users", status_code=303)
