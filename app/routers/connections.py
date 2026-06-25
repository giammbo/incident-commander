import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.config import get_settings
from app.db import get_db
from app.models import Role, SlackConnection, User
from app.services.slack import authorize_url, exchange_code
from app.settings_store import slack_settings

router = APIRouter()
SLACK_SCOPES = ["channels:manage", "groups:write", "chat:write"]


@router.get("/connections")
def connections_page(user: User = Depends(require_role(Role.admin))):
    return RedirectResponse("/settings", status_code=303)


@router.get("/connections/slack/install")
def slack_install(
    request: Request,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    slack = slack_settings(db)
    if not (slack.enabled and slack.client_id and slack.client_secret):
        request.session["flash"] = "Enable and configure Slack in Settings first."
        return RedirectResponse("/settings", status_code=303)
    state = secrets.token_urlsafe(24)
    request.session["slack_oauth_state"] = state
    redirect_uri = f"{get_settings().base_url.rstrip('/')}/connections/slack/callback"
    url = authorize_url(
        client_id=slack.client_id,
        redirect_uri=redirect_uri,
        state=state,
        scopes=SLACK_SCOPES,
    )
    return RedirectResponse(url, status_code=307)


@router.get("/connections/slack/callback")
def slack_callback(
    request: Request,
    code: str = "",
    state: str = "",
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    expected = request.session.pop("slack_oauth_state", None)
    if not state or state != expected:
        request.session["flash"] = "Slack connection failed: invalid state."
        return RedirectResponse("/settings", status_code=303)
    if not code:
        # User denied the OAuth flow (Slack calls back with ?error= and no code).
        request.session["flash"] = "Slack connection cancelled."
        return RedirectResponse("/settings", status_code=303)
    slack = slack_settings(db)
    redirect_uri = f"{get_settings().base_url.rstrip('/')}/connections/slack/callback"
    try:
        data = exchange_code(
            client_id=slack.client_id,
            client_secret=slack.client_secret,
            code=code,
            redirect_uri=redirect_uri,
        )
    except Exception as exc:  # noqa: BLE001 — surface the OAuth error, don't 500
        request.session["flash"] = f"Slack connection failed: {exc}"
        return RedirectResponse("/settings", status_code=303)
    team = data.get("team") or {}
    team_id = team.get("id")
    if not team_id:
        request.session["flash"] = "Slack connection failed: no team in response."
        return RedirectResponse("/settings", status_code=303)
    conn = db.scalar(select(SlackConnection).where(SlackConnection.team_id == team_id))
    if conn is None:
        conn = SlackConnection(team_id=team_id, created_by=user.id)
        db.add(conn)
    conn.team_name = team.get("name")
    conn.bot_user_id = data.get("bot_user_id")
    conn.app_id = data.get("app_id")
    conn.bot_token = data.get("access_token")
    conn.scopes = data.get("scope")
    conn.is_enterprise_install = bool(data.get("is_enterprise_install"))
    db.commit()
    request.session["flash"] = f"Connected Slack workspace {conn.team_name or team_id}."
    return RedirectResponse("/settings", status_code=303)
