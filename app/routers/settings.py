from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import require_role
from app.config import get_settings
from app.db import get_db
from app.models import Role, StatusLevel, User
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

    from app.models import GoogleConnection, SeverityLevel, SlackConnection, Webhook
    from app.services.custom_fields import FIELD_TYPES, list_field_defs
    from app.services.email import is_smtp_configured
    from app.services.incident_types import list_incident_types
    from app.services.roles import list_incident_role_types
    from app.settings_store import slack_settings, smtp_settings

    smtp = smtp_settings(db)
    slack = slack_settings(db)
    google = google_settings(db)
    sso = sso_settings(db)
    severity_levels = list(db.scalars(select(SeverityLevel).order_by(SeverityLevel.rank)))
    status_levels = list(db.scalars(select(StatusLevel).order_by(StatusLevel.rank)))
    slack_connections = list(db.scalars(select(SlackConnection).order_by(SlackConnection.id)))
    google_connections = list(db.scalars(select(GoogleConnection).order_by(GoogleConnection.id)))
    webhooks = list(db.scalars(select(Webhook).order_by(Webhook.id)))
    incident_role_types = list_incident_role_types(db)
    incident_types = list_incident_types(db)
    custom_field_defs = list_field_defs(db)
    from app.services.teams import list_teams

    teams = list_teams(db)
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
            "slack_can_connect": bool(slack.enabled and slack.client_id and slack.client_secret),
            "slack_connections": slack_connections,
            "google": google,
            "google_configured": bool(google.client_id and google.client_secret),
            "google_can_connect": bool(
                google.enabled and google.client_id and google.client_secret
            ),
            "google_connections": google_connections,
            "sso": sso,
            "base_url": get_settings().base_url,
            "severity_levels": severity_levels,
            "status_levels": status_levels,
            "webhooks": webhooks,
            "incident_role_types": incident_role_types,
            "incident_types": incident_types,
            "custom_field_defs": custom_field_defs,
            "field_types": FIELD_TYPES,
            "teams": teams,
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
    issuer: str = Form(""),
    client_id: str = Form(""),
    client_secret: str = Form(""),
    display_name: str = Form(""),
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    s = sso_settings(db)
    s.sso_enabled = sso_enabled
    s.allow_local_login = allow_local_login
    s.allowed_domains = allowed_domains or None
    s.issuer = issuer or None
    s.client_id = client_id or None
    s.display_name = display_name or None
    if client_secret:
        s.client_secret = client_secret
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


@router.post("/settings/statuses")
def settings_statuses_create(
    request: Request,
    label: str = Form(...),
    category: str = Form(...),
    rank: int = Form(100),
    is_default: bool = Form(False),
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.services.statuses import create_status_level

    try:
        create_status_level(db, label=label, category=category, rank=rank, is_default=is_default)
        db.commit()
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/statuses/{level_id}/delete")
def settings_statuses_delete(
    request: Request,
    level_id: int,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.services.statuses import delete_status_level

    try:
        delete_status_level(db, level_id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/statuses/{level_id}/default")
def settings_statuses_default(
    level_id: int,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.services.statuses import set_default_status_level

    set_default_status_level(db, level_id)
    db.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/incident-types")
def settings_incident_types_create(
    request: Request,
    label: str = Form(...),
    description: str | None = Form(None),
    rank: int = Form(100),
    is_default: bool = Form(False),
    default_severity_level_id: int | None = Form(None),
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.services.incident_types import create_incident_type

    try:
        create_incident_type(
            db,
            label=label,
            description=description,
            rank=rank,
            is_default=is_default,
            default_severity_level_id=default_severity_level_id,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/incident-types/{type_id}/delete")
def settings_incident_types_delete(
    request: Request,
    type_id: int,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.services.incident_types import delete_incident_type

    try:
        delete_incident_type(db, type_id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/incident-types/{type_id}/default")
def settings_incident_types_default(
    type_id: int,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.services.incident_types import set_default_incident_type

    set_default_incident_type(db, type_id)
    db.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/custom-fields")
def settings_custom_fields_create(
    request: Request,
    label: str = Form(...),
    field_type: str = Form(...),
    options: str | None = Form(None),
    required: bool = Form(False),
    rank: int = Form(100),
    incident_type_ids: list[int] = Form(default=[]),
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.services.custom_fields import create_field_def

    try:
        create_field_def(
            db,
            label=label,
            field_type=field_type,
            options=options,
            required=required,
            rank=rank,
            incident_type_ids=incident_type_ids,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/custom-fields/{field_id}/delete")
def settings_custom_fields_delete(
    request: Request,
    field_id: int,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.services.custom_fields import delete_field_def

    try:
        delete_field_def(db, field_id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/roles")
def settings_role_create(
    request: Request,
    label: str = Form(...),
    rank: int = Form(100),
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.services.roles import create_incident_role_type

    try:
        create_incident_role_type(db, label=label, rank=rank)
        db.commit()
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/roles/{role_type_id}/delete")
def settings_role_delete(
    request: Request,
    role_type_id: int,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.services.roles import delete_incident_role_type

    try:
        delete_incident_role_type(db, role_type_id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/webhooks")
def settings_webhook_create(
    request: Request,
    name: str = Form(...),
    url: str = Form(...),
    format: str = Form("generic"),
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.models import Webhook, WebhookFormat

    name, url = name.strip(), url.strip()
    if not name or not url:
        request.session["flash"] = "Webhook needs a name and a URL."
        return RedirectResponse("/settings", status_code=303)
    try:
        fmt = WebhookFormat(format)
    except ValueError:
        request.session["flash"] = "Unknown webhook format."
        return RedirectResponse("/settings", status_code=303)
    db.add(Webhook(name=name, url=url, format=fmt, created_by=user.id))
    db.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/webhooks/{webhook_id}/delete")
def settings_webhook_delete(
    webhook_id: int,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.models import Webhook

    w = db.get(Webhook, webhook_id)
    if w:
        db.delete(w)
        db.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/teams")
def settings_team_create(
    request: Request,
    name: str = Form(...),
    description: str | None = Form(None),
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.services.teams import create_team

    try:
        create_team(db, name=name, description=description)
        db.commit()
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/teams/{team_id}/delete")
def settings_team_delete(
    request: Request,
    team_id: int,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.services.teams import delete_team

    try:
        delete_team(db, team_id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
    return RedirectResponse("/settings", status_code=303)
