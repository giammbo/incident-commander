from __future__ import annotations

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Webhook, WebhookFormat


def incident_message(incident, event: str) -> str:
    sev = incident.severity_level.label if incident.severity_level else "—"
    if event == "opened":
        return f":rotating_light: *{sev}* incident opened: {incident.title}"
    if event == "closed":
        return f":white_check_mark: Incident closed: {incident.title}"
    return f":pencil2: Incident updated — *{sev}* · {incident.title}"


def build_payload(
    fmt: WebhookFormat,
    *,
    text: str,
    incident,
    event: str,
    url: str | None,
    message: str | None = None,
) -> dict:
    if fmt == WebhookFormat.slack:
        return {"text": text}
    if fmt == WebhookFormat.discord:
        return {"content": text}
    if fmt == WebhookFormat.teams:
        return {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "summary": f"Incident {event}",
            "text": text,
        }
    return {
        "event": event,
        "incident": {
            "id": incident.id,
            "title": incident.title,
            "severity": incident.severity_level.label if incident.severity_level else None,
            "status": incident.status.category.value if incident.status else None,
            "url": url,
        },
        "message": message,
    }


def notify(
    db: Session, incident, event: str, *, base_url: str, post=None, message: str | None = None
) -> None:
    post = post or httpx.post
    try:
        hooks = list(db.scalars(select(Webhook).where(Webhook.enabled.is_(True))))
        if not hooks:
            return
        url = f"{base_url.rstrip('/')}/incidents/{incident.id}"
        text = message or incident_message(incident, event)
        for hook in hooks:
            try:
                post(
                    hook.url,
                    json=build_payload(
                        hook.format,
                        text=text,
                        incident=incident,
                        event=event,
                        url=url,
                        message=message,
                    ),
                    timeout=5,
                )
            except Exception:  # noqa: BLE001 — one webhook must never break the others
                continue
    except Exception:  # noqa: BLE001 — notify is best-effort; never break the incident op
        return
