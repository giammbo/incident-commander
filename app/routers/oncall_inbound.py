"""Public on-call acknowledgement endpoints: email link, Slack buttons, Twilio voice/SMS.

None of these use the session. Each request is authenticated on its own:

* ``/oncall/ack/{token}`` — signed, expiring ack token (itsdangerous). GET only
  renders a confirmation page so mail link scanners never ack; POST acks. The
  POST is a same-origin form and stays under the CSRF origin check.
* ``/slack/interactions`` — Slack request signature (``v0`` HMAC-SHA256 with the
  signing secret, timestamp skew <= 5 minutes). CSRF-exempt.
* ``/oncall/twilio/*`` — ``X-Twilio-Signature`` over the public URL rebuilt from
  ``BASE_URL``. CSRF-exempt.

Slack timing: Slack wants an HTTP 200 within ~3 s, but ``acknowledge`` /
``escalate_now`` wait for the escalation row lock, which a worker may hold while
it is sending. The DB work runs synchronously under ``SET LOCAL lock_timeout``
(``SLACK_LOCK_TIMEOUT``); on timeout nothing changes and the user gets an
ephemeral "busy, try again". The ``response_url`` update (a network call to
Slack) runs as a background task after the 200 has been sent.

Responses and logs never include contact values (phone numbers, emails) or
secrets; TwiML and Slack text are escaped.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl
from xml.sax.saxutils import escape as xml_escape

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from slack_sdk import WebClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import (
    ContactMethod,
    Escalation,
    EscalationEvent,
    EscalationStatus,
    NotificationJob,
    SlackConnection,
    User,
)
from app.routers.oncall import _can_act
from app.services import oncall_engine as engine
from app.services import oncall_twilio
from app.services.oncall_notify import read_ack_token
from app.settings_store import oncall_settings, slack_settings
from app.templating import templates

router = APIRouter()
log = logging.getLogger(__name__)

SLACK_MAX_SKEW_SECONDS = 300
SLACK_LOCK_TIMEOUT = "2s"
# A double-clicked or replayed Slack "Escalate" must not climb more than one level.
ESCALATE_DEBOUNCE = timedelta(seconds=30)  # keeps the whole request well inside Slack's 3 s budget
_SLACK_RESPONSE_PREFIX = "https://hooks.slack.com/"
_MAX_BODY_BYTES = 65_536
_ACKABLE = (EscalationStatus.triggered, EscalationStatus.exhausted)
_OPEN = (EscalationStatus.triggered, EscalationStatus.acknowledged, EscalationStatus.exhausted)
_LOCK_NOT_AVAILABLE = "55P03"


def _http_post(url: str, *, json: dict) -> None:  # injectable in tests
    httpx.post(url, json=json, timeout=5.0)


def _slack_client(token: str) -> WebClient:  # injectable in tests
    return WebClient(token=token, timeout=2)


async def _raw_body(request: Request) -> bytes:
    return await request.body()


def _now() -> datetime:
    return datetime.now(UTC)


def _notified_user_ids(db: Session, esc_id: int, user_ids: list[int]) -> list[int]:
    """Users among ``user_ids`` with a notify job for this escalation (the UI's rule)."""
    if not user_ids:
        return []
    eligible = sorted(
        set(
            db.scalars(
                select(NotificationJob.user_id).where(
                    NotificationJob.escalation_id == esc_id,
                    NotificationJob.kind == "notify",
                    NotificationJob.user_id.in_(user_ids),
                )
            )
        )
    )
    esc = db.get(Escalation, esc_id)
    return [
        uid
        for uid in eligible
        if esc is not None
        and (user := _active_user(db, uid)) is not None
        and _can_act(db, user, esc)
    ]


def _active_user(db: Session, user_id: int | None) -> User | None:
    user = db.get(User, user_id) if user_id else None
    return user if user is not None and user.is_active else None


# ---- email ack link -------------------------------------------------------------

_BAD_TOKEN = "This acknowledgement link is invalid or has expired."


def _ack_page(request: Request, token: str, status_code: int = 200, **ctx) -> HTMLResponse:
    base = {"token": token, "esc": None, "error": None, "result": None, "can_ack": False}
    resp = templates.TemplateResponse(
        request, "oncall/ack_confirm.html", base | ctx, status_code=status_code
    )
    # The token is a bearer credential in the URL: never cache it or leak it onward.
    resp.headers["Cache-Control"] = "no-store"
    # same-origin, not no-referrer: with no-referrer browsers send ``Origin: null`` on
    # the form POST and the CSRF origin check rejects it. The token still never
    # leaves the origin.
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


def _resolve_token(db: Session, token: str):
    """Return (escalation, user, error_status, error_text)."""
    try:
        esc_id, user_id = read_ack_token(token)
    except ValueError:
        return None, None, 400, _BAD_TOKEN
    esc = db.get(Escalation, esc_id)
    if esc is None:
        return None, None, 404, "This page no longer exists."
    user = _active_user(db, user_id)
    if user is None or not _can_act(db, user, esc):
        return esc, None, 403, "This link can no longer be used to acknowledge the page."
    return esc, user, None, None


@router.get("/oncall/ack/{token}", response_class=HTMLResponse)
def ack_link_page(request: Request, token: str, db: Session = Depends(get_db)):
    esc, user, code, err = _resolve_token(db, token)
    if code is not None:
        return _ack_page(request, token, code, error=err)
    return _ack_page(
        request,
        token,
        esc=esc,
        can_ack=esc.status in _ACKABLE,
        result=None if esc.status in _ACKABLE else f"This page is already {esc.status.value}.",
    )


@router.post("/oncall/ack/{token}", response_class=HTMLResponse)
def ack_link_submit(request: Request, token: str, db: Session = Depends(get_db)):
    esc, user, code, err = _resolve_token(db, token)
    if code is not None:
        return _ack_page(request, token, code, error=err)
    ok = engine.acknowledge(db, esc, user_id=user.id, via="email", now=_now())
    db.commit()
    db.refresh(esc)
    if ok:
        return _ack_page(request, token, esc=esc, done=True, result="Thanks — paging has stopped.")
    return _ack_page(request, token, esc=esc, result=f"This page is already {esc.status.value}.")


# ---- slack interactivity -----------------------------------------------------------


def _slack_signature_ok(secret: str | None, headers, body: bytes, *, now: float) -> bool:
    ts = headers.get("x-slack-request-timestamp")
    sig = headers.get("x-slack-signature")
    if not secret or not ts or not sig:
        return False
    try:
        ts_int = int(ts)
    except ValueError:
        return False
    if abs(now - ts_int) > SLACK_MAX_SKEW_SECONDS:
        return False
    base = b"v0:" + ts.encode() + b":" + body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), sig.encode())


def _slack_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slack_user(db: Session, team_id: str, slack_uid: str) -> User | None:
    """Map a Slack (team, user) pair to an active app user.

    First by the cached ``"<team_id>:<user_id>"`` slack ContactMethod, then by the
    Slack profile email via the matching workspace connection (the pair is then
    cached on the user's slack ContactMethod).
    """
    if not team_id or not slack_uid:
        return None
    pair = f"{team_id}:{slack_uid}"
    methods = db.scalars(
        select(ContactMethod).where(ContactMethod.kind == "slack").order_by(ContactMethod.id)
    ).all()
    for cm in methods:
        if cm.value == pair:
            return _active_user(db, cm.user_id)
    conn = db.scalar(select(SlackConnection).where(SlackConnection.team_id == team_id))
    if conn is None:
        return None
    try:
        info = _slack_client(conn.bot_token).users_info(user=slack_uid)
        member = info["user"]
        email = (member.get("profile") or {}).get("email")
    except Exception as exc:  # noqa: BLE001 — a lookup failure just means "not mapped"
        log.warning("Slack users.info failed: %s", type(exc).__name__)
        return None
    if not email or member.get("deleted") or member.get("is_bot"):
        return None
    user = db.scalar(
        select(User).where(func.lower(User.email) == email.lower(), User.is_active.is_(True))
    )
    if user is None:
        return None
    mine = next((cm for cm in methods if cm.user_id == user.id), None)
    if mine is not None:
        mine.value = pair
    else:
        db.add(ContactMethod(user_id=user.id, kind="slack", value=pair))
    db.flush()
    return user


def _slack_status_body(db: Session, esc: Escalation, *, actor: User, action: str, ok: bool):
    title = _slack_escape(esc.title)
    if esc.status == EscalationStatus.acknowledged:
        who = db.get(User, esc.acked_by) if esc.acked_by else None
        state = f"acknowledged by {_slack_escape(who.name)}" if who else "acknowledged"
    elif ok and action == "oncall_escalate":
        state = f"escalated by {_slack_escape(actor.name)}"
    else:
        state = esc.status.value
    text_ = f"*[{esc.urgency.value.upper()}] {title}* — {state} (code {esc.short_code})"
    blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": text_}}]
    if esc.status in _ACKABLE:
        buttons = [
            {
                "type": "button",
                "action_id": "oncall_ack",
                "style": "primary",
                "text": {"type": "plain_text", "text": "Acknowledge"},
                "value": str(esc.id),
            }
        ]
        if esc.status == EscalationStatus.triggered:
            buttons.append(
                {
                    "type": "button",
                    "action_id": "oncall_escalate",
                    "text": {"type": "plain_text", "text": "Escalate"},
                    "value": str(esc.id),
                }
            )
        blocks.append({"type": "actions", "elements": buttons})
    return {"replace_original": True, "text": text_, "blocks": blocks}


def _ephemeral(message: str) -> dict:
    return {"response_type": "ephemeral", "replace_original": False, "text": message}


def _respond_to_slack(url: str, body: dict) -> None:
    try:
        _http_post(url, json=body)
    except Exception as exc:  # noqa: BLE001 — best effort, the action already happened
        log.warning("Slack response_url update failed: %s", type(exc).__name__)


def _recently_escalated(db: Session, esc: Escalation, now: datetime) -> bool:
    """True if the escalation was escalated within ``ESCALATE_DEBOUNCE``.

    Takes the escalation row lock first (the engine's serialization point, under
    the caller's lock_timeout), so two concurrent clicks cannot both pass the check:
    the second waits for the first's commit and then sees its event.
    """
    locked = db.scalar(
        select(Escalation.id)
        .where(Escalation.id == esc.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked is None:
        return False
    return (
        db.scalar(
            select(EscalationEvent.id)
            .where(
                EscalationEvent.escalation_id == esc.id,
                EscalationEvent.kind == "escalated",
                EscalationEvent.created_at > now - ESCALATE_DEBOUNCE,
            )
            .limit(1)
        )
        is not None
    )


def _is_lock_timeout(exc: OperationalError) -> bool:
    return getattr(exc.orig, "sqlstate", None) == _LOCK_NOT_AVAILABLE


@router.post("/slack/interactions")
def slack_interactions(
    request: Request,
    background: BackgroundTasks,
    body: bytes = Depends(_raw_body),
    db: Session = Depends(get_db),
):
    if len(body) > _MAX_BODY_BYTES:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    secret = slack_settings(db).signing_secret
    if not _slack_signature_ok(secret, request.headers, body, now=time.time()):
        return JSONResponse({"error": "invalid signature"}, status_code=401)
    try:
        form = dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
        payload = json.loads(form["payload"])
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
    except (KeyError, ValueError, UnicodeDecodeError):
        return JSONResponse({"error": "invalid payload"}, status_code=400)
    if payload.get("type") != "block_actions":
        return Response(status_code=200)
    action = next(
        (
            a
            for a in payload.get("actions") or []
            if isinstance(a, dict) and a.get("action_id") in ("oncall_ack", "oncall_escalate")
        ),
        None,
    )
    if action is None:
        return Response(status_code=200)

    url = payload.get("response_url")
    url = url if isinstance(url, str) and url.startswith(_SLACK_RESPONSE_PREFIX) else None

    def reply(message_body: dict) -> Response:
        if url is not None:
            background.add_task(_respond_to_slack, url, message_body)
        return Response(status_code=200)

    slack_user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    team = payload.get("team") if isinstance(payload.get("team"), dict) else {}
    team_id = str(team.get("id") or slack_user.get("team_id") or "")
    user = _slack_user(db, team_id, str(slack_user.get("id") or ""))
    if user is None:
        return reply(
            _ephemeral(
                "Your Slack account isn't linked to an Incident Commander user, "
                "so nothing was done. Check your email address in both, or open the page "
                "in the web app."
            )
        )
    try:
        esc = db.get(Escalation, int(action.get("value")))
    except (TypeError, ValueError):
        esc = None
    if esc is None:
        return reply(_ephemeral("This page no longer exists."))
    if not _can_act(db, user, esc):
        return reply(_ephemeral("You weren't paged for this escalation, so nothing was done."))
    db.commit()  # keep a freshly cached Slack mapping even if the action times out

    action_id = action["action_id"]
    try:
        db.execute(text(f"SET LOCAL lock_timeout = '{SLACK_LOCK_TIMEOUT}'"))
        if action_id == "oncall_ack":
            ok = engine.acknowledge(db, esc, user_id=user.id, via="slack", now=_now())
        else:
            now = _now()
            if _recently_escalated(db, esc, now):
                db.rollback()
                return reply(_ephemeral("Already escalated a moment ago."))
            ok = engine.escalate_now(db, esc, user_id=user.id, now=now)
        db.commit()
    except OperationalError as exc:
        if not _is_lock_timeout(exc):
            raise
        db.rollback()
        return reply(
            _ephemeral(
                "This page is busy right now (a notification is going out). "
                "Please try again in a few seconds."
            )
        )
    db.refresh(esc)
    return reply(_slack_status_body(db, esc, actor=user, action=action_id, ok=ok))


# ---- twilio voice & sms -----------------------------------------------------------


def _twilio_params(body: bytes) -> dict[str, str]:
    return dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))


def _twilio_ok(db: Session, request: Request, params: dict[str, str]) -> bool:
    token = oncall_settings(db).twilio_auth_token
    sig = request.headers.get("x-twilio-signature")
    if not token or not sig:
        return False
    url = get_settings().base_url.rstrip("/") + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    return oncall_twilio.valid_signature(token, url, params, sig)


def _twiml(*verbs: str) -> Response:
    xml = '<?xml version="1.0" encoding="UTF-8"?><Response>' + "".join(verbs) + "</Response>"
    return Response(content=xml, media_type="application/xml")


def _say(message: str) -> str:
    return f"<Say>{xml_escape(message)}</Say>"


def _sms(message: str) -> Response:
    return _twiml(f"<Message>{xml_escape(message)}</Message>")


def _forbidden() -> Response:
    return Response(status_code=403)


def _voice_text(esc: Escalation) -> str:
    message = (esc.message or "").strip()
    if len(message) > 300:
        message = message[:299] + "…"
    parts = [f"Incident Commander page, {esc.urgency.value} urgency.", f"{esc.title}."]
    if message:
        parts.append(message)
    parts.append("Press 1 to acknowledge.")
    return " ".join(parts)


def _gather(esc: Escalation, user_id: int) -> Response:
    action = f"/oncall/twilio/gather/{esc.id}/{user_id}"
    return _twiml(
        f'<Gather numDigits="1" action="{action}" method="POST">'
        + _say(_voice_text(esc))
        + "</Gather>",
        _say("No input received. Goodbye."),
    )


@router.post("/oncall/twilio/voice/{escalation_id}/{user_id}")
def twilio_voice(
    escalation_id: int,
    user_id: int,
    request: Request,
    body: bytes = Depends(_raw_body),
    db: Session = Depends(get_db),
):
    if len(body) > _MAX_BODY_BYTES or not _twilio_ok(db, request, _twilio_params(body)):
        return _forbidden()
    if escalation_id == 0:  # "Send me a test" call
        return _twiml(_say("This is a test call from Incident Commander."))
    esc = db.get(Escalation, escalation_id)
    if esc is None:
        return _twiml(_say("This page no longer exists. Goodbye."))
    user = _active_user(db, user_id)
    if user is None or not _can_act(db, user, esc):
        return _twiml(_say("You can't acknowledge this page. Goodbye."))
    if esc.status not in _ACKABLE:
        return _twiml(_say(f"This page is already {esc.status.value}. Goodbye."))
    return _gather(esc, user_id)


@router.post("/oncall/twilio/gather/{escalation_id}/{user_id}")
def twilio_gather(
    escalation_id: int,
    user_id: int,
    request: Request,
    body: bytes = Depends(_raw_body),
    db: Session = Depends(get_db),
):
    params = _twilio_params(body)
    if len(body) > _MAX_BODY_BYTES or not _twilio_ok(db, request, params):
        return _forbidden()
    esc = db.get(Escalation, escalation_id)
    if esc is None:
        return _twiml(_say("This page no longer exists. Goodbye."))
    user = _active_user(db, user_id)
    if user is None or not _can_act(db, user, esc):
        return _twiml(_say("You can't acknowledge this page. Goodbye."))
    if esc.status not in _ACKABLE:
        return _twiml(_say(f"This page is already {esc.status.value}. Goodbye."))
    if params.get("Digits") != "1":
        return _gather(esc, user_id)
    user = _active_user(db, user_id)
    if user is None or not _can_act(db, user, esc):
        return _twiml(_say("You can't acknowledge this page. Goodbye."))
    ok = engine.acknowledge(db, esc, user_id=user.id, via="voice", now=_now())
    db.commit()
    if not ok:
        db.refresh(esc)
        return _twiml(_say(f"This page is already {esc.status.value}. Goodbye."))
    return _twiml(_say("Acknowledged. Paging has stopped. Goodbye."))


_SMS_ACK = re.compile(r"^\s*ack\s*(?:(\d{4}))?\s*[.!]?\s*$", re.IGNORECASE)
_PHONE_JUNK = re.compile(r"[\s().-]")


def _phone_users(db: Session, sender: str) -> list[int]:
    """Active users whose *verified* phone matches ``sender`` (values are encrypted,
    so the comparison happens after decryption, in Python)."""
    wanted = _PHONE_JUNK.sub("", sender or "")
    if not wanted:
        return []
    methods = db.scalars(
        select(ContactMethod).where(
            ContactMethod.kind == "phone", ContactMethod.verified_at.is_not(None)
        )
    ).all()
    ids = {cm.user_id for cm in methods if _PHONE_JUNK.sub("", cm.value or "") == wanted}
    return sorted(u for u in ids if _active_user(db, u) is not None)


def _paged_escalations(db: Session, user_ids: list[int], statuses) -> list[Escalation]:
    notified = (
        select(NotificationJob.escalation_id)
        .where(NotificationJob.kind == "notify", NotificationJob.user_id.in_(user_ids))
        .scalar_subquery()
    )
    rows = list(
        db.scalars(
            select(Escalation)
            .where(Escalation.status.in_(statuses), Escalation.id.in_(notified))
            .order_by(Escalation.id)
        )
    )

    return [esc for esc in rows if _notified_user_ids(db, esc.id, user_ids)]


def _sms_ack(db: Session, esc: Escalation, user_ids: list[int]) -> Response:
    actor = _notified_user_ids(db, esc.id, user_ids)[0]
    ok = engine.acknowledge(db, esc, user_id=actor, via="sms", now=_now())
    db.commit()
    if not ok:
        db.refresh(esc)
        return _sms(f"Page {esc.short_code} is already {esc.status.value}.")
    return _sms(f"Acknowledged page {esc.short_code}: {esc.title}")


@router.post("/oncall/twilio/sms")
def twilio_sms(
    request: Request,
    body: bytes = Depends(_raw_body),
    db: Session = Depends(get_db),
):
    params = _twilio_params(body)
    if len(body) > _MAX_BODY_BYTES or not _twilio_ok(db, request, params):
        return _forbidden()
    user_ids = _phone_users(db, params.get("From", ""))
    if not user_ids:
        return _twiml()  # unknown or unverified number: no reply, no action
    match = _SMS_ACK.match(params.get("Body", ""))
    if match is None:
        return _sms("Reply ACK followed by the page code (for example ACK 1234) to acknowledge.")
    code = match.group(1)
    if code:
        esc = next(
            (e for e in _paged_escalations(db, user_ids, _OPEN) if e.short_code == code), None
        )
        if esc is None:
            return _sms(f"No open page with code {code} was sent to you.")
        if esc.status not in _ACKABLE:
            return _sms(f"Page {code} is already {esc.status.value}.")
        return _sms_ack(db, esc, user_ids)
    waiting = _paged_escalations(db, user_ids, _ACKABLE)
    if not waiting:
        return _sms("You have no pages waiting for acknowledgement.")
    if len(waiting) == 1:
        return _sms_ack(db, waiting[0], user_ids)
    codes = ", ".join(e.short_code for e in waiting)
    return _sms(
        f"You have {len(waiting)} open pages: {codes}. "
        "Reply ACK followed by a code to acknowledge one. Nothing was acknowledged."
    )
