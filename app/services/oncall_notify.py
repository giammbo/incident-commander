"""Notification channels for on-call escalations (email, Slack DM, Web Push).

``send`` returns a provider
reference and raises on failure; the engine records the outcome. Messages never
include secrets.
"""

from __future__ import annotations

import html
import json
from typing import NamedTuple, Protocol

import pywebpush
from itsdangerous import BadData, URLSafeTimedSerializer
from slack_sdk import WebClient
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.models import (
    Alert,
    ContactMethod,
    Escalation,
    Incident,
    NotificationRule,
    SlackConnection,
    Urgency,
    User,
)
from app.services import oncall_twilio
from app.services.email import is_smtp_configured, send_email
from app.services.incident_access import can_view_incident
from app.settings_store import oncall_settings, slack_settings, smtp_settings

_ACK_SALT = "oncall-ack"


class NotificationChannel(Protocol):
    key: str
    label: str

    def available(self, db: Session) -> bool: ...

    def reachable(self, db: Session, user: User) -> bool: ...

    def send(self, db: Session, *, user: User, escalation: Escalation, ack_url: str) -> str: ...


def can_receive_escalation(db: Session, user: User, escalation: Escalation) -> bool:
    """A page grants no access. Check *every* linked incident against current membership.

    Both links matter if an alert was subsequently attached to another incident.
    Refresh active status, group memberships/roles and incident assignments: any
    may have changed in another session during a provider reachability probe or
    while a notification or signed link was waiting.
    """
    user = db.scalar(
        select(User)
        .where(User.id == user.id)
        .options(selectinload(User.groups))
        .execution_options(populate_existing=True)
    )
    if user is None or not user.is_active:
        return False
    ids = {escalation.incident_id} if escalation.incident_id else set()
    if escalation.alert_id:
        alert_incident = db.scalar(select(Alert.incident_id).where(Alert.id == escalation.alert_id))
        if alert_incident:
            ids.add(alert_incident)
    for incident_id in ids:
        incident = db.scalar(
            select(Incident)
            .where(Incident.id == incident_id)
            .execution_options(populate_existing=True)
        )
        if incident is None:
            return False
        db.expire(incident, ["role_assignments"])
        if not can_view_incident(user, incident):
            return False
    return True


def _require_recipient_access(db: Session, user: User, escalation: Escalation) -> None:
    if not can_receive_escalation(db, user, escalation):
        raise PermissionError("Recipient no longer has access to the linked incident")


# ---- rules -----------------------------------------------------------------


def default_rules(urgency: Urgency) -> list[tuple[str, int]]:
    if urgency == Urgency.high:
        return [("email", 0), ("slack", 0), ("sms", 2), ("voice", 5)]
    return [("email", 0), ("slack", 0)]


def rules_for(db: Session, user: User, urgency: Urgency) -> list[tuple[str, int]]:
    rows = db.scalars(
        select(NotificationRule)
        .where(NotificationRule.user_id == user.id, NotificationRule.urgency == urgency)
        .order_by(NotificationRule.delay_minutes, NotificationRule.id)
    ).all()
    if not rows:
        return default_rules(urgency)
    return [(r.channel, r.delay_minutes) for r in rows]


# ---- ack tokens ------------------------------------------------------------


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().session_secret, salt=_ACK_SALT)


def make_ack_token(escalation_id: int, user_id: int) -> str:
    return _serializer().dumps([escalation_id, user_id])


def read_ack_token(token: str, max_age: int = 86400) -> tuple[int, int]:
    try:
        data = _serializer().loads(token, max_age=max_age)
        escalation_id, user_id = data
        return int(escalation_id), int(user_id)
    except (BadData, TypeError, ValueError) as exc:
        raise ValueError("invalid or expired ack token") from exc


def ack_url(base_url: str, escalation: Escalation, user: User) -> str:
    return f"{base_url.rstrip('/')}/oncall/ack/{make_ack_token(escalation.id, user.id)}"


def _escalation_url(escalation: Escalation) -> str:
    return f"{get_settings().base_url.rstrip('/')}/oncall/escalations/{escalation.id}"


def notification_text(escalation: Escalation) -> str:
    return (
        f"[{escalation.urgency.value.upper()}] {escalation.title} — "
        f"reply ACK {escalation.short_code} · {_escalation_url(escalation)}"
    )


# ---- email -----------------------------------------------------------------


class EmailChannel:
    key = "email"
    label = "Email"

    def available(self, db: Session) -> bool:
        return is_smtp_configured(smtp_settings(db))

    def reachable(self, db: Session, user: User) -> bool:
        return bool(user.email)

    def send(self, db: Session, *, user: User, escalation: Escalation, ack_url: str) -> str:
        _require_recipient_access(db, user, escalation)
        subject = f"[{escalation.urgency.value.upper()}] {escalation.title}"
        link = _escalation_url(escalation)
        text = (
            f"{escalation.title}\n\n"
            f"{escalation.message or ''}\n\n"
            f"Acknowledge: {ack_url}\nDetails: {link}\n"
        )
        body = (
            f"<p><strong>{html.escape(escalation.title)}</strong></p>"
            f"<p>{html.escape(escalation.message or '')}</p>"
            f'<p><a href="{html.escape(ack_url)}">Acknowledge</a> · '
            f'<a href="{html.escape(link)}">Details</a></p>'
        )
        send_email(
            smtp_settings(db), to=user.email, subject=subject, text_body=text, html_body=body
        )
        return "sent"


# ---- slack -----------------------------------------------------------------


def _slack_connections(db: Session) -> list[SlackConnection]:
    return list(db.scalars(select(SlackConnection).order_by(SlackConnection.id)).all())


class SlackIdentity(NamedTuple):
    team_id: str
    user_id: str


def _lookup_slack(db: Session, user: User) -> SlackIdentity | None:
    for conn in _slack_connections(db):
        try:
            resp = WebClient(token=conn.bot_token, timeout=5).users_lookupByEmail(email=user.email)
            uid = resp["user"]["id"]
        except Exception:
            continue
        if uid:
            return SlackIdentity(conn.team_id, uid)
    return None


def slack_user_id(db: Session, user: User, *, refresh: bool = False) -> SlackIdentity | None:
    """Return (team_id, slack_user_id), cached as "<team_id>:<user_id>"."""
    cm = db.scalar(
        select(ContactMethod)
        .where(ContactMethod.user_id == user.id, ContactMethod.kind == "slack")
        .order_by(ContactMethod.id)
    )
    if cm is not None and not refresh and ":" in cm.value:
        team_id, uid = cm.value.split(":", 1)
        return SlackIdentity(team_id, uid)
    found = _lookup_slack(db, user)
    if found is None:
        return None
    value = f"{found.team_id}:{found.user_id}"
    if cm is not None:
        cm.value = value
    else:
        db.add(ContactMethod(user_id=user.id, kind="slack", value=value))
    db.flush()
    return found


class SlackChannel:
    key = "slack"
    label = "Slack"

    def available(self, db: Session) -> bool:
        return bool(slack_settings(db).enabled and _slack_connections(db))

    def reachable(self, db: Session, user: User) -> bool:
        return slack_user_id(db, user) is not None

    @staticmethod
    def _conn(db: Session, team_id: str) -> SlackConnection | None:
        return next((c for c in _slack_connections(db) if c.team_id == team_id), None)

    def send(self, db: Session, *, user: User, escalation: Escalation, ack_url: str) -> str:
        _require_recipient_access(db, user, escalation)
        ident = slack_user_id(db, user)
        if ident is None:
            raise RuntimeError("Slack user not found")
        conn = self._conn(db, ident.team_id)
        if conn is None:
            ident = slack_user_id(db, user, refresh=True)
            if ident is None:
                raise RuntimeError("Slack user not found")
            conn = self._conn(db, ident.team_id)
            if conn is None:
                raise RuntimeError("Slack workspace not connected")
        uid = ident.user_id
        text = notification_text(escalation)
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "oncall_ack",
                        "style": "primary",
                        "text": {"type": "plain_text", "text": "Acknowledge"},
                        "value": str(escalation.id),
                    },
                    {
                        "type": "button",
                        "action_id": "oncall_escalate",
                        "text": {"type": "plain_text", "text": "Escalate"},
                        "value": str(escalation.id),
                    },
                ],
            },
        ]
        client = WebClient(token=conn.bot_token, timeout=5)
        dm = client.conversations_open(users=uid)
        channel = dm["channel"]["id"]
        resp = client.chat_postMessage(channel=channel, text=text, blocks=blocks)
        return str(resp["ts"])


# ---- web push --------------------------------------------------------------


class PushChannel:
    key = "push"
    label = "Browser push"

    def available(self, db: Session) -> bool:
        s = oncall_settings(db)
        return bool(s.vapid_public_key and s.vapid_private_key)

    def _subs(self, db: Session, user: User) -> list[ContactMethod]:
        return list(
            db.scalars(
                select(ContactMethod)
                .where(ContactMethod.user_id == user.id, ContactMethod.kind == "push")
                .order_by(ContactMethod.id)
            ).all()
        )

    def reachable(self, db: Session, user: User) -> bool:
        return bool(self._subs(db, user))

    def send(self, db: Session, *, user: User, escalation: Escalation, ack_url: str) -> str:
        _require_recipient_access(db, user, escalation)
        s = oncall_settings(db)
        if not (s.vapid_public_key and s.vapid_private_key):
            raise RuntimeError("Web Push keys are not configured")
        from_address = smtp_settings(db).from_address
        claims = {"sub": "mailto:" + (from_address or "admin@localhost")}
        payload = json.dumps(
            {
                "title": f"[{escalation.urgency.value.upper()}] {escalation.title}",
                "body": f"Reply ACK {escalation.short_code}",
                "url": _escalation_url(escalation),
            }
        )
        sent, errors = 0, []
        for sub in self._subs(db, user):
            try:
                pywebpush.webpush(
                    subscription_info=json.loads(sub.value),
                    data=payload,
                    vapid_private_key=s.vapid_private_key,
                    vapid_claims=dict(claims),
                    timeout=10,
                )
                sent += 1
            except pywebpush.WebPushException as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in (404, 410):
                    db.delete(sub)
                    db.flush()
                    errors.append(f"subscription expired ({status})")
                else:
                    errors.append(f"push failed (HTTP {status})" if status else "push failed")
            except Exception as exc:
                errors.append(f"push failed: {type(exc).__name__}")
        if sent == 0:
            raise RuntimeError("; ".join(errors) or "no push subscription")
        return f"push:{sent}"


# ---- twilio (sms, voice) ---------------------------------------------------


def _twilio_configured(db: Session) -> bool:
    s = oncall_settings(db)
    return bool(s.twilio_account_sid and s.twilio_auth_token and s.twilio_from_number)


def _verified_phone(db: Session, user: User) -> str | None:
    cm = db.scalar(
        select(ContactMethod)
        .where(
            ContactMethod.user_id == user.id,
            ContactMethod.kind == "phone",
            ContactMethod.verified_at.is_not(None),
        )
        .order_by(ContactMethod.id)
    )
    return cm.value if cm is not None else None


class SmsChannel:
    key = "sms"
    label = "SMS"

    def available(self, db: Session) -> bool:
        return _twilio_configured(db)

    def reachable(self, db: Session, user: User) -> bool:
        return _verified_phone(db, user) is not None

    def send(self, db: Session, *, user: User, escalation: Escalation, ack_url: str) -> str:
        _require_recipient_access(db, user, escalation)
        phone = _verified_phone(db, user)
        if phone is None:
            raise RuntimeError("No verified phone number")
        return oncall_twilio.send_sms(oncall_settings(db), phone, notification_text(escalation))


class VoiceChannel:
    key = "voice"
    label = "Phone call"

    def available(self, db: Session) -> bool:
        return _twilio_configured(db)

    def reachable(self, db: Session, user: User) -> bool:
        return _verified_phone(db, user) is not None

    def send(self, db: Session, *, user: User, escalation: Escalation, ack_url: str) -> str:
        _require_recipient_access(db, user, escalation)
        phone = _verified_phone(db, user)
        if phone is None:
            raise RuntimeError("No verified phone number")
        base = get_settings().base_url.rstrip("/")
        url = f"{base}/oncall/twilio/voice/{escalation.id}/{user.id}"
        return oncall_twilio.place_call(oncall_settings(db), phone, url)


CHANNELS: dict[str, NotificationChannel] = {
    "email": EmailChannel(),
    "slack": SlackChannel(),
    "push": PushChannel(),
    "sms": SmsChannel(),
    "voice": VoiceChannel(),
}
