from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import require_role
from app.config import get_settings
from app.db import get_db
from app.models import Role, User
from app.settings_store import app_settings, google_settings, sso_settings
from app.templating import templates

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from sqlalchemy import select

    from app.models import SeverityLevel
    from app.services.email import is_smtp_configured
    from app.settings_store import slack_settings, smtp_settings

    smtp = smtp_settings(db)
    slack = slack_settings(db)
    google = google_settings(db)
    sso = sso_settings(db)
    severity_levels = list(db.scalars(select(SeverityLevel).order_by(SeverityLevel.rank)))
    from app.models import Service

    services = list(db.scalars(select(Service).order_by(Service.name)))
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "current_user": user,
            "app": app_settings(db),
            "smtp": smtp,
            "smtp_configured": is_smtp_configured(smtp),
            "slack": slack,
            "slack_configured": bool(slack.client_id and slack.client_secret),
            "google": google,
            "google_configured": bool(google.client_id and google.client_secret),
            "sso": sso,
            "base_url": get_settings().base_url,
            "notice": request.session.pop("flash", None),
            "severity_levels": severity_levels,
            "services": services,
        },
    )


@router.post("/settings/smtp")
def settings_smtp(
    host: str = Form(""),
    port: int | None = Form(None),
    username: str = Form(""),
    password: str = Form(""),
    from_address: str = Form(""),
    use_tls: bool = Form(False),
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.settings_store import smtp_settings

    s = smtp_settings(db)
    s.host = host or None
    s.port = port
    s.username = username or None
    s.from_address = from_address or None
    s.use_tls = use_tls
    if password:  # only overwrite when a new secret is provided
        s.password = password
    db.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/smtp/test")
def settings_smtp_test(
    request: Request,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.services.email import is_smtp_configured, send_email
    from app.settings_store import smtp_settings

    s = smtp_settings(db)
    if not is_smtp_configured(s):
        request.session["flash"] = "Configure SMTP host and From address first."
    else:
        try:
            send_email(
                s,
                to=user.email,
                subject="Incident Commander test email",
                text_body="SMTP is working.",
                html_body="<p>SMTP is working.</p>",
            )
            request.session["flash"] = f"Test email sent to {user.email}."
        except Exception as exc:  # noqa: BLE001 — surface the SMTP error to the admin
            request.session["flash"] = f"Test email failed: {exc}"
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/app")
def settings_app(
    slack_channel_name_template: str = Form(...),
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    s = app_settings(db)
    s.slack_channel_name_template = slack_channel_name_template
    db.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/slack")
def settings_slack(
    client_id: str = Form(""),
    client_secret: str = Form(""),
    signing_secret: str = Form(""),
    enabled: bool = Form(False),
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.settings_store import slack_settings

    s = slack_settings(db)
    s.client_id = client_id or None
    s.enabled = enabled
    if client_secret:
        s.client_secret = client_secret
    if signing_secret:
        s.signing_secret = signing_secret
    db.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/google")
def settings_google(
    client_id: str = Form(""),
    client_secret: str = Form(""),
    enabled: bool = Form(False),
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    g = google_settings(db)
    g.client_id = client_id or None
    g.enabled = enabled
    if client_secret:
        g.client_secret = client_secret
    db.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/sso")
def settings_sso(
    sso_enabled: bool = Form(False),
    allow_local_login: bool = Form(False),
    allowed_domains: str = Form(""),
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    s = sso_settings(db)
    s.sso_enabled = sso_enabled
    s.allow_local_login = allow_local_login
    s.allowed_domains = allowed_domains or None
    db.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/severity")
def settings_severity_create(
    request: Request,
    label: str = Form(...),
    color: str = Form(...),
    rank: int = Form(100),
    is_default: bool = Form(False),
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.services.catalog import create_severity_level

    try:
        create_severity_level(db, label=label, color=color, rank=rank, is_default=is_default)
        db.commit()
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/services")
def settings_service_create(
    name: str = Form(...),
    description: str = Form(""),
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.services.catalog import create_service

    create_service(db, name=name.strip(), description=description or None, created_by=user.id)
    db.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/services/{service_id}/deps")
def settings_service_deps(
    service_id: int,
    depends_on_ids: list[int] = Form(default=[]),
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.models import Service
    from app.services.catalog import set_service_dependencies

    svc = db.get(Service, service_id)
    if svc:
        set_service_dependencies(db, svc, depends_on_ids)
        db.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/services/{service_id}/delete")
def settings_service_delete(
    service_id: int,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.services.catalog import delete_service

    delete_service(db, service_id)
    db.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/severity/{level_id}/delete")
def settings_severity_delete(
    request: Request,
    level_id: int,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.services.catalog import delete_severity_level

    try:
        delete_severity_level(db, level_id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/severity/{level_id}/default")
def settings_severity_default(
    level_id: int,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.services.catalog import set_default_severity_level

    set_default_severity_level(db, level_id)
    db.commit()
    return RedirectResponse("/settings", status_code=303)
