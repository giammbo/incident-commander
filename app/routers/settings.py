from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import require_role
from app.config import get_settings
from app.db import get_db
from app.models import Role, StatusLevel, User
from app.services import settings_catalog, settings_validation
from app.settings_store import app_settings, google_settings, sso_settings
from app.templating import templates

router = APIRouter()


def settings_result(request, section, *, message=None, error=None, form=None, db=None, user=None):
    if error:
        # Recover directly from this request: large drafts never enter a cookie.
        secret_fields = {
            "url",
            "secret",
            "password",
            "client_secret",
            "signing_secret",
            "service_account_json",
            "twilio_auth_token",
        }
        values = (
            {k: form.getlist(k) for k in form if k not in secret_fields} if form is not None else {}
        )
        # A rejected issuer can itself contain credentials or a token query.
        if "issuer" in values:
            from urllib.parse import urlsplit

            try:
                parsed = urlsplit(values["issuer"][0])
                if parsed.username or parsed.password or parsed.query or parsed.fragment:
                    values.pop("issuer")
            except ValueError:
                values.pop("issuer")
        return render_settings(
            request,
            user,
            db,
            draft={"action": request.url.path, "values": values, "section": section},
            error=error,
            status_code=422,
        )
    if message:
        request.session["settings_message"] = message
    return RedirectResponse(f"/settings#{section}", status_code=303)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    return render_settings(request, user, db)


def render_settings(request, user, db, *, draft=None, error=None, status_code=200):
    from sqlalchemy import select

    from app.models import SeverityLevel, SlackConnection, Webhook
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
    webhooks = list(db.scalars(select(Webhook).order_by(Webhook.id)))
    incident_role_types = list_incident_role_types(db)
    incident_types = list_incident_types(db)
    custom_field_defs = list_field_defs(db)
    from app.services.teams import list_teams

    teams = list_teams(db)
    from app.models import InboundIntegration

    integrations = list(db.scalars(select(InboundIntegration).order_by(InboundIntegration.id)))
    from app.settings_store import oncall_settings

    oncall = oncall_settings(db)
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
            "oncall": oncall,
            "twilio_configured": bool(
                oncall.twilio_account_sid and oncall.twilio_auth_token and oncall.twilio_from_number
            ),
            "google": google,
            "google_configured": bool(google.service_account_json and google.impersonate_email),
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
            "integrations": integrations,
            "masked_destination": settings_validation.masked_destination,
            "draft": draft or {},
            "settings_error": error,
            "settings_message": request.session.pop("settings_message", None),
        },
        status_code=status_code,
    )


@router.post("/settings/smtp")
async def settings_smtp(
    request: Request, user: User = Depends(require_role(Role.admin)), db: Session = Depends(get_db)
):
    from app.settings_store import smtp_settings

    form = await request.form()
    try:
        host = settings_catalog.text(form.get("host"), "SMTP host", 255, required=False)
        username = settings_catalog.text(form.get("username"), "Username", 255, required=False)
        address = settings_catalog.text(
            form.get("from_address"), "From address", 320, required=False
        )
        port = settings_catalog.integer(form.get("port"), "SMTP port", optional=True)
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("SMTP port must be between 1 and 65535.")
        if address and not settings_validation.email(address):
            raise ValueError("Enter a valid From email address.")
        if any(c.isspace() for c in host):
            raise ValueError("SMTP host must not contain whitespace.")
        s = smtp_settings(db)
        s.host, s.username, s.from_address, s.port = (
            host or None,
            username or None,
            address or None,
            port,
        )
        s.use_tls = settings_catalog.checked(form.get("use_tls", ""))
        if form.get("password"):
            s.password = form["password"]
        db.commit()
    except ValueError as exc:
        db.rollback()
        return settings_result(request, "smtp", error=str(exc), form=form, db=db, user=user)
    return settings_result(request, "smtp", message="SMTP settings saved. No email was sent.")


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
        except Exception:  # noqa: BLE001 — provider errors may include credentials
            request.session["flash"] = (
                "Test email failed. Check SMTP configuration and provider availability."
            )
    return settings_result(request, "smtp", message="Settings saved.")


@router.post("/settings/app")
async def settings_app(
    request: Request, user: User = Depends(require_role(Role.admin)), db: Session = Depends(get_db)
):
    form = await request.form()
    try:
        value = settings_validation.channel_template(
            str(form.get("slack_channel_name_template", "")).strip()
        )
        app_settings(db).slack_channel_name_template = value
        db.commit()
    except ValueError as exc:
        db.rollback()
        return settings_result(request, "application", error=str(exc), form=form, db=db, user=user)
    return settings_result(request, "application", message="Application settings saved.")


@router.post("/settings/slack")
async def settings_slack(
    request: Request, user: User = Depends(require_role(Role.admin)), db: Session = Depends(get_db)
):
    from app.settings_store import slack_settings

    form = await request.form()
    s = slack_settings(db)
    try:
        client_id = settings_catalog.text(form.get("client_id"), "Client ID", 255, required=False)
        enabled = settings_catalog.checked(form.get("enabled", ""))
        secret = str(form.get("client_secret", "")) or s.client_secret
        if enabled and not (client_id and secret):
            raise ValueError("Client ID and client secret are required to enable Slack.")
        s.client_id, s.enabled, s.client_secret = client_id or None, enabled, secret
        if form.get("signing_secret"):
            s.signing_secret = form["signing_secret"]
        db.commit()
    except ValueError as exc:
        db.rollback()
        return settings_result(request, "slack", error=str(exc), form=form, db=db, user=user)
    return settings_result(
        request,
        "slack",
        message="Slack settings saved. Credentials have not been tested with Slack.",
    )


@router.post("/settings/oncall")
async def settings_oncall(
    request: Request, user: User = Depends(require_role(Role.admin)), db: Session = Depends(get_db)
):
    from app.settings_store import oncall_settings

    form = await request.form()
    try:
        sid = settings_catalog.text(
            form.get("twilio_account_sid"), "Account SID", 64, required=False
        )
        number = settings_catalog.text(
            form.get("twilio_from_number"), "From number", 32, required=False
        )
        s = oncall_settings(db)
        s.twilio_account_sid, s.twilio_from_number = sid or None, number or None
        if form.get("twilio_auth_token"):
            s.twilio_auth_token = form["twilio_auth_token"]
        db.commit()
    except ValueError as exc:
        db.rollback()
        return settings_result(request, "oncall", error=str(exc), form=form, db=db, user=user)
    return settings_result(
        request, "oncall", message="On-call settings saved. No notification was sent."
    )


@router.post("/settings/oncall/push-keys")
def settings_oncall_push_keys(
    request: Request,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    import base64

    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from py_vapid import Vapid

    from app.settings_store import oncall_settings

    s = oncall_settings(db)
    if s.vapid_public_key and s.vapid_private_key:
        request.session["flash"] = "Push keys already exist."
        return settings_result(request, "oncall", message="Settings saved.")
    v = Vapid()
    v.generate_keys()
    raw_private = v.private_key.private_numbers().private_value.to_bytes(32, "big")
    raw_public = v.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    s.vapid_private_key = base64.urlsafe_b64encode(raw_private).rstrip(b"=").decode()
    s.vapid_public_key = base64.urlsafe_b64encode(raw_public).rstrip(b"=").decode()
    db.commit()
    request.session["flash"] = "Web Push keys generated."
    return settings_result(request, "oncall", message="Settings saved.")


@router.post("/settings/google")
async def settings_google(
    request: Request, user: User = Depends(require_role(Role.admin)), db: Session = Depends(get_db)
):
    form = await request.form()
    g = google_settings(db)
    try:
        key = str(form.get("service_account_json", "")).strip() or g.service_account_json
        enabled = settings_catalog.checked(form.get("enabled", ""))
        impersonate = settings_catalog.text(
            form.get("impersonate_email"), "Impersonation email", 255, required=False
        )
        if key and (enabled or str(form.get("service_account_json", "")).strip()):
            settings_validation.google_service_account(key)
        if impersonate and not settings_validation.email(impersonate):
            raise ValueError("Enter a valid impersonation email address.")
        if enabled and (not key or not impersonate):
            raise ValueError(
                "A complete service-account key and impersonation email are required to enable Google."
            )
        g.service_account_json, g.impersonate_email, g.enabled = key, impersonate or None, enabled
        db.commit()
    except ValueError as exc:
        db.rollback()
        return settings_result(request, "google", error=str(exc), form=form, db=db, user=user)
    return settings_result(
        request,
        "google",
        message="Google settings saved. Structure validated locally; provider access has not been tested.",
    )


@router.post("/settings/sso")
async def settings_sso(
    request: Request, user: User = Depends(require_role(Role.admin)), db: Session = Depends(get_db)
):
    form = await request.form()
    s = sso_settings(db)
    try:
        enabled = settings_catalog.checked(form.get("sso_enabled", ""))
        issuer = settings_catalog.text(form.get("issuer"), "Issuer URL", 512, required=False)
        client_id = settings_catalog.text(form.get("client_id"), "Client ID", 255, required=False)
        secret = str(form.get("client_secret", "")) or s.client_secret
        if issuer:
            settings_validation.http_url(issuer)
            from urllib.parse import urlsplit

            parsed = urlsplit(issuer)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError(
                    "Issuer must not contain credentials, query parameters or a fragment."
                )
        if enabled and not (issuer and client_id and secret):
            raise ValueError("Issuer URL, client ID and client secret are required to enable SSO.")
        domains = settings_catalog.text(
            form.get("allowed_domains"), "Allowed domains", 1000, required=False
        )
        if any(
            not settings_validation.email("user@" + domain.strip())
            for domain in domains.split(",")
            if domain.strip()
        ):
            raise ValueError("Enter valid comma-separated domain names.")
        display = settings_catalog.text(
            form.get("display_name"), "Button label", 120, required=False
        )
        s.sso_enabled, s.issuer, s.client_id, s.client_secret = (
            enabled,
            issuer or None,
            client_id or None,
            secret,
        )
        s.allow_local_login = settings_catalog.checked(form.get("allow_local_login", ""))
        s.allowed_domains, s.display_name = domains or None, display or None
        db.commit()
    except ValueError as exc:
        db.rollback()
        return settings_result(request, "sso", error=str(exc), form=form, db=db, user=user)
    return settings_result(
        request,
        "sso",
        message="SSO settings saved. Protected admin password login remains available.",
    )


# Explicit allowlisted catalog paths preserve existing URLs and IDs.
async def catalog_save(request, kind, db, user, *, record_id=None):
    form = await request.form()
    try:
        settings_catalog.save(db, kind, form, record_id=record_id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return settings_result(request, kind, error=str(exc), form=form, db=db, user=user)
    return settings_result(request, kind, message="Changes saved.")


def register_catalog(kind):
    async def create(
        request: Request,
        user: User = Depends(require_role(Role.admin)),
        db: Session = Depends(get_db),
    ):
        return await catalog_save(request, kind, db, user)

    async def edit(
        record_id: int,
        request: Request,
        user: User = Depends(require_role(Role.admin)),
        db: Session = Depends(get_db),
    ):
        return await catalog_save(request, kind, db, user, record_id=record_id)

    def delete(
        record_id: int,
        request: Request,
        user: User = Depends(require_role(Role.admin)),
        db: Session = Depends(get_db),
    ):
        try:
            settings_catalog.delete(db, kind, record_id)
            db.commit()
        except ValueError as exc:
            db.rollback()
            return settings_result(request, kind, error=str(exc), db=db, user=user)
        return settings_result(request, kind, message="Record deleted.")

    def make_default(
        record_id: int,
        request: Request,
        user: User = Depends(require_role(Role.admin)),
        db: Session = Depends(get_db),
    ):
        try:
            settings_catalog.make_default(db, kind, record_id)
            db.commit()
        except ValueError as exc:
            db.rollback()
            return settings_result(request, kind, error=str(exc), db=db, user=user)
        return settings_result(request, kind, message="Default updated.")

    router.add_api_route(
        f"/settings/{kind}", create, methods=["POST"], name=f"settings_{kind}_create"
    )
    router.add_api_route(
        f"/settings/{kind}/{{record_id}}/edit", edit, methods=["POST"], name=f"settings_{kind}_edit"
    )
    router.add_api_route(
        f"/settings/{kind}/{{record_id}}/delete",
        delete,
        methods=["POST"],
        name=f"settings_{kind}_delete",
    )
    if kind in ("severity", "statuses", "incident-types"):
        router.add_api_route(
            f"/settings/{kind}/{{record_id}}/default",
            make_default,
            methods=["POST"],
            name=f"settings_{kind}_default",
        )


for _kind in settings_catalog.MODELS:
    register_catalog(_kind)


@router.post("/settings/webhooks")
@router.post("/settings/webhooks/{webhook_id}/edit")
async def settings_webhook_save(
    request: Request,
    webhook_id: int | None = None,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.models import Webhook, WebhookFormat

    form = await request.form()
    hook = db.get(Webhook, webhook_id) if webhook_id is not None else None
    if webhook_id is not None and hook is None:
        return HTMLResponse("Webhook not found", status_code=404)
    try:
        name = settings_catalog.text(form.get("name"), "Webhook name", 120)
        url = str(form.get("url", "")).strip() or (hook.url if hook else "")
        settings_validation.http_url(url)
        try:
            fmt = WebhookFormat(form.get("format", "generic"))
        except ValueError:
            raise ValueError("Choose a valid webhook format.") from None
        if hook is None:
            hook = Webhook(name=name, url=url, format=fmt, created_by=user.id)
            db.add(hook)
        else:
            hook.name, hook.url, hook.format = name, url, fmt
        secret = str(form.get("secret", "")).strip()
        if len(secret) > 4096:
            raise ValueError("Signing secret must be 4096 characters or fewer.")
        if secret:
            hook.secret = secret
        db.commit()
    except ValueError as exc:
        db.rollback()
        return settings_result(request, "webhooks", error=str(exc), form=form, db=db, user=user)
    return settings_result(request, "webhooks", message="Webhook saved. No request was sent.")


@router.post("/settings/webhooks/{webhook_id}/toggle")
def settings_webhook_toggle(
    webhook_id: int,
    request: Request,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.models import Webhook

    hook = db.get(Webhook, webhook_id)
    if hook is None:
        return HTMLResponse("Webhook not found", status_code=404)
    try:
        if not hook.enabled:
            settings_validation.http_url(hook.url)
        hook.enabled = not hook.enabled
        db.commit()
    except ValueError as exc:
        db.rollback()
        return settings_result(request, "webhooks", error=str(exc), db=db, user=user)
    return settings_result(
        request, "webhooks", message="Webhook enabled." if hook.enabled else "Webhook disabled."
    )


@router.post("/settings/webhooks/{webhook_id}/preview")
def settings_webhook_preview(
    webhook_id: int,
    request: Request,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    import json
    from types import SimpleNamespace

    from app.models import StatusCategory, Webhook
    from app.services.webhooks import build_payload

    hook = db.get(Webhook, webhook_id)
    if hook is None:
        return HTMLResponse("Webhook not found", status_code=404)
    sample = SimpleNamespace(
        id=0,
        title="Sample incident (synthetic)",
        severity_level=SimpleNamespace(label="SEV2"),
        status=SimpleNamespace(label="Investigating", category=StatusCategory.active),
    )
    payload = build_payload(
        hook.format, text="Sample incident opened", incident=sample, event="opened", url=None
    )
    return templates.TemplateResponse(
        request,
        "webhook_preview.html",
        {
            "current_user": user,
            "hook": hook,
            "destination": settings_validation.masked_destination(hook.url),
            "payload": json.dumps(payload, indent=2),
        },
    )


@router.post("/settings/webhooks/{webhook_id}/delete")
def settings_webhook_delete(
    webhook_id: int,
    request: Request,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.models import Webhook

    hook = db.get(Webhook, webhook_id)
    if hook:
        db.delete(hook)
        db.commit()
    return settings_result(
        request,
        "webhooks",
        message="Webhook deleted. Future events will no longer be delivered to it.",
    )


@router.post("/settings/integrations")
async def settings_integrations_create(
    request: Request, user: User = Depends(require_role(Role.admin)), db: Session = Depends(get_db)
):
    import secrets

    from app.models import InboundIntegration

    form = await request.form()
    try:
        name = settings_catalog.text(form.get("name"), "Integration name", 120)
        kind = str(form.get("kind", ""))
        if kind not in ("sns", "alertmanager", "generic"):
            raise ValueError("Choose a valid integration kind.")
        db.add(InboundIntegration(name=name, kind=kind, token=secrets.token_urlsafe(32)))
        db.commit()
    except ValueError as exc:
        db.rollback()
        return settings_result(request, "integrations", error=str(exc), form=form, db=db, user=user)
    return settings_result(request, "integrations", message="Inbound integration created.")


GENERIC_MAPPING_KEYS = ("dedup_key", "title", "status", "severity", "resolved_value")


@router.post("/settings/integrations/{integration_id}/settings")
async def settings_integrations_mapping(
    request: Request,
    integration_id: int,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    """Save only the fixed generic parser mapping keys."""
    from app.models import InboundIntegration

    integration = db.get(InboundIntegration, integration_id)
    if integration is None:
        return HTMLResponse("Integration not found", status_code=404)
    form = await request.form()
    try:
        if integration.kind != "generic":
            raise ValueError("Only Generic JSON integrations have a field mapping.")
        mapping = {
            k: settings_catalog.text(
                form.get(k), k.replace("_", " ").title(), 500, required=k == "dedup_key"
            )
            for k in GENERIC_MAPPING_KEYS
        }
        mapping = {k: v for k, v in mapping.items() if v}
        for k, value in mapping.items():
            if k != "resolved_value" and (
                any(c.isspace() for c in value) or any(not part for part in value.split("."))
            ):
                raise ValueError(
                    "Mapping paths must contain nonempty dot-separated keys without whitespace."
                )
        kept = {
            k: v for k, v in (integration.settings or {}).items() if k not in GENERIC_MAPPING_KEYS
        }
        integration.settings = {**kept, **mapping}
        db.commit()
    except ValueError as exc:
        db.rollback()
        return settings_result(request, "integrations", error=str(exc), form=form, db=db, user=user)
    return settings_result(request, "integrations", message="Inbound field mapping saved.")


@router.post("/settings/integrations/{integration_id}/delete")
def settings_integrations_delete(
    request: Request,
    integration_id: int,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from sqlalchemy import select

    from app.models import InboundIntegration

    integ = db.scalar(select(InboundIntegration).where(InboundIntegration.id == integration_id))
    if integ is not None:
        db.delete(integ)
        db.commit()
    return settings_result(request, "integrations", message="Settings saved.")
