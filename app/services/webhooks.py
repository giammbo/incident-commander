from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Webhook, WebhookFormat

log = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-IC-Signature"
TIMEOUT = 5
# 2 retries on network errors / 5xx, with a short backoff before each
RETRY_BACKOFF = (0.5, 1.5)

# Deliveries run off the request thread: one dead endpoint can take
# (1 + retries) * TIMEOUT seconds, which must not hold up the HTTP response.
# In-process and best-effort; the interpreter drains the queue on a clean exit.
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="webhooks")


@dataclass(frozen=True)
class Delivery:
    name: str
    url: str
    body: bytes
    secret: str | None = None


def incident_message(incident, event: str) -> str:
    sev = incident.severity_level.label if incident.severity_level else "—"
    if event == "opened":
        return f":rotating_light: *{sev}* incident opened: {incident.title}"
    if event == "closed":
        return f":white_check_mark: Incident closed: {incident.title}"
    return f":pencil2: Incident updated — *{sev}* · {incident.title}"


_TEAMS_HEADINGS = {
    "opened": "Incident opened",
    "closed": "Incident closed",
    "update": "Stakeholder update",
}


def _teams_card(incident, event: str, url: str | None, message: str | None) -> dict:
    # Power Automate "Workflows" webhook format (the O365 connectors that took
    # MessageCard are retired): a message wrapping one Adaptive Card.
    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": f"{_TEAMS_HEADINGS.get(event, 'Incident updated')}: {incident.title}",
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True,
        },
        {
            "type": "FactSet",
            "facts": [
                {
                    "title": "Severity",
                    "value": incident.severity_level.label if incident.severity_level else "—",
                },
                {"title": "Status", "value": incident.status.label if incident.status else "—"},
            ],
        },
    ]
    if message:
        body.append({"type": "TextBlock", "text": message, "wrap": True})
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
    }
    if url:
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open incident", "url": url}]
    return {
        "type": "message",
        "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
        ],
    }


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
        return _teams_card(incident, event, url, message)
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


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def build_deliveries(
    db: Session, incident, event: str, *, base_url: str, message: str | None = None
) -> list[Delivery]:
    """Snapshot every enabled webhook's request now, while the incident is loaded,
    so the background delivery needs neither the DB session nor the ORM object."""
    hooks = list(db.scalars(select(Webhook).where(Webhook.enabled.is_(True))))
    if not hooks:
        return []
    url = f"{base_url.rstrip('/')}/incidents/{incident.id}"
    text = message or incident_message(incident, event)
    return [
        Delivery(
            name=hook.name,
            url=hook.url,
            secret=hook.secret or None,
            body=json.dumps(
                build_payload(
                    hook.format, text=text, incident=incident, event=event, url=url, message=message
                ),
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode(),
        )
        for hook in hooks
    ]


def _http_post(url: str, **kwargs) -> httpx.Response:
    return httpx.post(url, **kwargs)


def deliver_one(d: Delivery, *, post=None, sleep=time.sleep) -> bool:
    post = post or _http_post
    headers = {"Content-Type": "application/json"}
    if d.secret:
        headers[SIGNATURE_HEADER] = sign(d.secret, d.body)
    for attempt in range(len(RETRY_BACKOFF) + 1):
        if attempt:
            sleep(RETRY_BACKOFF[attempt - 1])
        try:
            resp = post(d.url, content=d.body, headers=headers, timeout=TIMEOUT)
        except httpx.TransportError as exc:
            log.warning("webhook %r: network error (attempt %d): %s", d.name, attempt + 1, exc)
            continue
        if resp.status_code < 500:
            if resp.status_code >= 400:
                log.warning("webhook %r: rejected with HTTP %d", d.name, resp.status_code)
            return resp.status_code < 400
        log.warning("webhook %r: HTTP %d (attempt %d)", d.name, resp.status_code, attempt + 1)
    return False


def deliver(deliveries: list[Delivery], *, post=None, sleep=time.sleep) -> None:
    for d in deliveries:
        try:
            deliver_one(d, post=post, sleep=sleep)
        except Exception:  # noqa: BLE001 — one webhook must never break the others
            log.exception("webhook %r: delivery failed", d.name)


def _dispatch(deliveries: list[Delivery]) -> None:
    _executor.submit(deliver, deliveries)


def notify(db: Session, incident, event: str, *, base_url: str, message: str | None = None) -> None:
    """Fire the enabled webhooks for an incident event without blocking the caller.

    Payloads are built here (the caller's session and incident are still valid);
    the HTTP calls, with signature and retries, run on the background pool."""
    try:
        deliveries = build_deliveries(db, incident, event, base_url=base_url, message=message)
    except Exception:  # noqa: BLE001 — notify is best-effort; never break the incident op
        log.exception("webhooks: could not build %r deliveries", event)
        return
    if deliveries:
        _dispatch(deliveries)
