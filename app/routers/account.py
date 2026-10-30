import hashlib
import hmac
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.auth import require_user, safe_next
from app.db import get_db
from app.models import ContactMethod, NotificationRule, Urgency, User
from app.security.passwords import hash_password
from app.services import oncall_notify, oncall_twilio
from app.services import users as users_svc
from app.settings_store import oncall_settings
from app.templating import templates

router = APIRouter()


@router.get("/oncall-sw.js", include_in_schema=False)
def push_worker():
    return FileResponse(
        Path(__file__).resolve().parents[1] / "static" / "oncall-sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


def _asks_name(user: User) -> bool:
    """The forced first password change also asks the bootstrap admin for a real name."""
    return bool(
        user.must_change_password
        and user.is_protected_admin
        and user.name == users_svc.DEFAULT_ADMIN_NAME
    )


@router.get("/account/password", response_class=HTMLResponse)
def password_page(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(
        request,
        "account_password.html",
        {"current_user": user, "error": None, "ask_name": _asks_name(user)},
    )


@router.post("/account/password")
def password_submit(
    request: Request,
    new_password: str = Form(...),
    confirm: str = Form(...),
    name: str | None = Form(None),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    ask_name = _asks_name(user)
    clean_name = (name or "").strip()
    error = None
    if new_password != confirm or len(new_password) < 8:
        error = "Passwords must match and be at least 8 chars"
    elif ask_name and not 1 <= len(clean_name) <= 200:
        error = "Enter your name (1-200 characters)"
    if error:
        return templates.TemplateResponse(
            request,
            "account_password.html",
            {"current_user": user, "error": error, "ask_name": ask_name, "name": clean_name},
            status_code=400,
        )
    if ask_name:
        user.name = clean_name
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    db.add(user)
    db.commit()
    return RedirectResponse(safe_next(request.session.pop("login_next", None)), status_code=303)


# ---- My notifications -------------------------------------------------------

PHONE_RE = re.compile(r"^\+[1-9]\d{6,14}$")
CODE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5
RESEND_COOLDOWN = timedelta(seconds=60)
# ContactMethod.verification_hash is String(64). To count wrong attempts without a
# new column it holds sha256(code).hexdigest()[:60] followed by a 4-digit attempt
# counter; verification_expires_at doubles as the "last code sent" marker (resend
# cooldown) and is never cleared; after MAX_ATTEMPTS wrong codes it is set to NULL (code invalidated).
_HASH_LEN = 60
_BACK = "/account/notifications"


def _digest(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()[:_HASH_LEN]


def _back(request: Request, message: str) -> RedirectResponse:
    request.session["flash"] = message
    return RedirectResponse(_BACK, status_code=303)


def _phone_row(db: Session, user: User) -> ContactMethod | None:
    return db.scalar(
        select(ContactMethod)
        .where(ContactMethod.user_id == user.id, ContactMethod.kind == "phone")
        .order_by(ContactMethod.id)
    )


def _pending_phone_row(db: Session, user: User) -> ContactMethod | None:
    pending = db.scalar(
        select(ContactMethod)
        .where(ContactMethod.user_id == user.id, ContactMethod.kind == "phone_pending")
        .order_by(ContactMethod.id.desc())
    )
    # Old installs may have an unverified phone from before pending contacts existed.
    legacy = _phone_row(db, user) if pending is None else None
    return pending or (legacy if legacy and not legacy.verified_at else None)


_UNAVAILABLE = "Not configured by an admin yet."
_UNREACHABLE = {
    "email": "Your account has no email address.",
    "slack": "No Slack account found for your email.",
    "push": "Enable browser notifications below on this device.",
    "sms": "Add and verify a phone number below.",
    "voice": "Add and verify a phone number below.",
}


def _channel_status(db: Session, user: User) -> list[dict]:
    out = []
    for key, ch in oncall_notify.CHANNELS.items():
        available = ch.available(db)
        reachable, reason = False, _UNAVAILABLE
        if available:
            try:
                reachable = ch.reachable(db, user)
            except Exception:  # noqa: BLE001 — a flaky lookup must not break the page
                reachable = False
            reason = "" if reachable else _UNREACHABLE.get(key, "Not reachable.")
        out.append(
            {
                "key": key,
                "label": ch.label,
                "available": available,
                "reachable": reachable,
                "reason": reason,
            }
        )
    return out


@router.get("/account/notifications", response_class=HTMLResponse)
def notifications_page(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    phone = _phone_row(db, user)
    pending = _pending_phone_row(db, user)
    settings = oncall_settings(db)
    return templates.TemplateResponse(
        request,
        "account_notifications.html",
        {
            "current_user": user,
            "channels": _channel_status(db, user),
            "rules": {u.value: oncall_notify.rules_for(db, user, u) for u in Urgency},
            "custom": {
                u.value: db.scalar(
                    select(NotificationRule.id).where(
                        NotificationRule.user_id == user.id, NotificationRule.urgency == u
                    )
                )
                is not None
                for u in Urgency
            },
            "phone": phone.value if phone else None,
            "phone_verified": bool(phone and phone.verified_at),
            "phone_pending": pending.value if pending else None,
            "phone_code_pending": bool(pending and pending.verification_hash),
            "phone_provider_ready": bool(
                settings.twilio_account_sid
                and settings.twilio_auth_token
                and settings.twilio_from_number
            ),
            "vapid_public_key": settings.vapid_public_key if settings.vapid_private_key else "",
            "push_count": db.query(ContactMethod)
            .filter(ContactMethod.user_id == user.id, ContactMethod.kind == "push")
            .count(),
        },
    )


@router.post("/account/notifications/phone")
def phone_save(
    request: Request,
    phone: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    phone = phone.strip()
    if not PHONE_RE.match(phone):
        return _back(request, "Enter the phone number in international format, e.g. +393331234567.")
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    for existing in (_phone_row(db, user), _pending_phone_row(db, user)):
        if (
            existing is not None
            and existing.verification_expires_at is not None
            and datetime.now(UTC) - (existing.verification_expires_at - CODE_TTL) < RESEND_COOLDOWN
        ):
            return _back(request, "Please wait a minute before requesting another code.")
    code = f"{secrets.randbelow(10**6):06d}"
    try:
        oncall_twilio.send_sms(
            oncall_settings(db), phone, f"Incident Commander verification code: {code}"
        )
    except Exception as exc:  # noqa: BLE001 — surfaced to the user, never the token
        return _back(
            request, f"Could not send the verification SMS: {exc}. Your active number is unchanged."
        )
    db.execute(
        delete(ContactMethod).where(
            ContactMethod.user_id == user.id, ContactMethod.kind == "phone_pending"
        )
    )
    db.add(
        ContactMethod(
            user_id=user.id,
            kind="phone_pending",
            value=phone,
            verified_at=None,
            verification_hash=_digest(code) + "0000",
            verification_expires_at=datetime.now(UTC) + CODE_TTL,
        )
    )
    db.commit()
    return _back(request, f"Verification code sent to {phone}. It expires in 10 minutes.")


@router.post("/account/notifications/phone/verify")
def phone_verify(
    request: Request,
    code: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    row = _pending_phone_row(db, user)
    now = datetime.now(UTC)
    if (
        row is None
        or not row.verification_hash
        or row.verification_expires_at is None
        or row.verification_expires_at < now
    ):
        return _back(
            request, "No valid verification code. Save your number again to get a new one."
        )
    stored, attempts = (
        row.verification_hash[:_HASH_LEN],
        int(row.verification_hash[_HASH_LEN:] or 0),
    )
    if hmac.compare_digest(stored.encode(), _digest(code.strip()).encode()):
        db.execute(
            delete(ContactMethod).where(
                ContactMethod.user_id == user.id,
                ContactMethod.kind == "phone",
                ContactMethod.id != row.id,
            )
        )
        row.kind = "phone"
        row.verified_at = now
        row.verification_hash = None
        db.commit()
        return _back(request, "Phone number verified.")
    attempts += 1
    if attempts >= MAX_ATTEMPTS:
        row.verification_hash = None
        msg = "Too many wrong codes. Save your number again to get a new one."
    else:
        row.verification_hash = f"{stored}{attempts:04d}"
        msg = "Wrong code."
    db.commit()
    return _back(request, msg)


@router.post("/account/notifications/push")
async def push_save(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    try:
        sub = await request.json()
        endpoint = sub["endpoint"]
        keys = sub["keys"]
        if not (
            isinstance(endpoint, str)
            and endpoint.startswith("https://")
            and len(endpoint) <= 2048
            and isinstance(keys, dict)
            and keys.get("p256dh")
            and keys.get("auth")
        ):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        return JSONResponse({"ok": False, "error": "invalid subscription"}, status_code=400)
    for cm in db.scalars(
        select(ContactMethod).where(ContactMethod.user_id == user.id, ContactMethod.kind == "push")
    ):
        try:
            same = json.loads(cm.value).get("endpoint") == endpoint
        except (ValueError, AttributeError):
            same = True
        if same:
            db.delete(cm)
    db.add(ContactMethod(user_id=user.id, kind="push", value=json.dumps(sub), verified_at=None))
    db.commit()
    return JSONResponse({"ok": True})


@router.post("/account/notifications/rules")
def rules_save(
    request: Request,
    urgency: str = Form(""),
    channel: list[str] = Form(default=[], alias="channel[]"),
    delay: list[str] = Form(default=[], alias="delay[]"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    try:
        urg = Urgency(urgency)
    except ValueError:
        return _back(request, "Unknown urgency.")
    pairs: list[tuple[str, int]] = []
    for i, ch in enumerate(channel):
        if not ch.strip():
            continue
        if ch not in oncall_notify.CHANNELS:
            return _back(request, f"Unknown channel: {ch}")
        raw = delay[i].strip() if i < len(delay) else "0"
        try:
            minutes = int(raw or 0)
        except ValueError:
            return _back(request, "Delay must be a whole number of minutes.")
        if not 0 <= minutes <= 60:
            return _back(request, "Delay must be between 0 and 60 minutes.")
        pairs.append((ch, minutes))
    db.execute(
        delete(NotificationRule).where(
            NotificationRule.user_id == user.id, NotificationRule.urgency == urg
        )
    )
    for ch, minutes in pairs:
        db.add(NotificationRule(user_id=user.id, urgency=urg, channel=ch, delay_minutes=minutes))
    db.commit()
    return _back(
        request,
        f"{urg.value.capitalize()}-urgency rules saved."
        if pairs
        else f"{urg.value.capitalize()}-urgency rules reset to defaults.",
    )


@router.post("/account/notifications/test/{channel}")
def send_test(
    channel: str,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    ch = oncall_notify.CHANNELS.get(channel)
    if ch is None:
        raise HTTPException(status_code=404)
    # Transient, never persisted.
    esc = SimpleNamespace(
        id=0,
        title="Test notification from Incident Commander",
        message="This is a test. Nothing needs your attention.",
        urgency=Urgency.high,
        short_code="0000",
        status="triggered",
        incident_id=None,
        alert_id=None,
    )
    try:
        ch.send(db, user=user, escalation=esc, ack_url=f"{_base()}{_BACK}")
        db.commit()
    except Exception as exc:  # noqa: BLE001 — report the reason to the user
        db.rollback()
        return _back(request, f"Test {getattr(ch, 'label', channel)} failed: {exc}")
    return _back(request, f"Test {getattr(ch, 'label', channel)} sent.")


def _base() -> str:
    from app.config import get_settings

    return get_settings().base_url.rstrip("/")
