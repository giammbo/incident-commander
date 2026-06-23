from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.models import Incident, SlackConnection
from app.services import google, slack
from app.settings_store import app_settings, google_settings


def _scope_summary(incident: Incident) -> str:
    if incident.system is None:
        return ""
    if incident.components:
        return f"{incident.system.name}: " + ", ".join(c.name for c in incident.components)
    return f"{incident.system.name}: whole system"


def open_incident_slack(db: Session, incident: Incident, connection: SlackConnection) -> None:
    state = dict(incident.creation_state or {})
    template = app_settings(db).slack_channel_name_template
    date_str = (incident.created_at or datetime.now(UTC)).strftime("%Y%m%d")
    name = slack.build_channel_name(template, title=incident.title, date_str=date_str)
    token = connection.bot_token
    try:
        ch = slack.create_channel(token, name=name, is_private=incident.is_private)
        incident.slack_channel_id = ch["id"]
        incident.slack_channel_name = ch["name"]
        incident.slack_channel_url = slack.channel_url(connection.team_id, ch["id"])
        sev = incident.severity_level.label if incident.severity_level else "—"
        scope = _scope_summary(incident)
        topic = f"{sev} · {incident.title}" + (f" · {scope}" if scope else "")
        slack.set_topic_purpose(
            token,
            channel_id=ch["id"],
            topic=topic,
            purpose=f"Incident channel for: {incident.title}",
        )
        state["channel"] = "ok"
    except Exception:  # noqa: BLE001 — partial-failure-safe; surfaced via creation_state
        state["channel"] = "failed"
        incident.creation_state = state
        db.flush()
        return
    try:
        text = f":rotating_light: *{sev}* incident opened: {incident.title}"
        if scope:
            text += f"\nScope: {scope}"
        if incident.meet_url:
            text += f"\nMeet: {incident.meet_url}"
        slack.post_message(
            token,
            channel_id=incident.slack_channel_id,
            text=text,
        )
        state["announce"] = "ok"
    except Exception:  # noqa: BLE001
        state["announce"] = "failed"
    incident.creation_state = state
    db.flush()


def open_incident_google(db: Session, incident: Incident, connection) -> None:
    state = dict(incident.creation_state or {})
    g = google_settings(db)
    start = datetime.now(UTC)
    end = start + timedelta(hours=1)
    try:
        link = google.create_meet(
            client_id=g.client_id,
            client_secret=g.client_secret,
            refresh_token=connection.refresh_token,
            calendar_id=connection.calendar_id,
            summary=f"Incident: {incident.title}",
            now_iso=start.isoformat(),
            end_iso=end.isoformat(),
        )
        incident.meet_url = link
        state["meet"] = "ok" if link else "failed"
    except Exception:  # noqa: BLE001
        state["meet"] = "failed"
    incident.creation_state = state
    db.flush()


def update_incident_slack(db: Session, incident: Incident, connection: SlackConnection) -> None:
    if not incident.slack_channel_id:
        return
    state = dict(incident.creation_state or {})
    sev = incident.severity_level.label if incident.severity_level else "—"
    scope = _scope_summary(incident)
    try:
        text = f":pencil2: Incident updated — *{sev}* · {incident.title}"
        if scope:
            text += f"\nScope: {scope}"
        topic = f"{sev} · {incident.title}" + (f" · {scope}" if scope else "")
        slack.post_message(
            connection.bot_token,
            channel_id=incident.slack_channel_id,
            text=text,
        )
        slack.set_topic_purpose(
            connection.bot_token,
            channel_id=incident.slack_channel_id,
            topic=topic,
            purpose=f"Incident channel for: {incident.title}",
        )
        state["updated_announce"] = "ok"
    except Exception:  # noqa: BLE001
        state["updated_announce"] = "failed"
    incident.creation_state = state
    db.flush()


def post_announcement(
    db: Session, incident: Incident, connection: SlackConnection, text: str
) -> None:
    if not incident.slack_channel_id:
        return
    state = dict(incident.creation_state or {})
    try:
        slack.post_message(connection.bot_token, channel_id=incident.slack_channel_id, text=text)
        state["update_announce"] = "ok"
    except Exception:  # noqa: BLE001
        state["update_announce"] = "failed"
    incident.creation_state = state
    db.flush()


def close_incident_slack(db: Session, incident: Incident, connection: SlackConnection) -> None:
    if not incident.slack_channel_id:
        return
    state = dict(incident.creation_state or {})
    try:
        slack.post_message(
            connection.bot_token,
            channel_id=incident.slack_channel_id,
            text=f":white_check_mark: Incident closed: {incident.title}",
        )
        state["closed_announce"] = "ok"
    except Exception:  # noqa: BLE001
        state["closed_announce"] = "failed"
    incident.creation_state = state
    db.flush()


def announce_meet_in_slack(db: Session, incident: Incident, connection: SlackConnection) -> None:
    if not (incident.slack_channel_id and incident.meet_url):
        return
    state = dict(incident.creation_state or {})
    try:
        slack.post_message(
            connection.bot_token,
            channel_id=incident.slack_channel_id,
            text=f":movie_camera: Meet added: {incident.meet_url}",
        )
        state["meet_announce"] = "ok"
    except Exception:  # noqa: BLE001 — partial-failure-safe; surfaced via creation_state
        state["meet_announce"] = "failed"
    incident.creation_state = state
    db.flush()
