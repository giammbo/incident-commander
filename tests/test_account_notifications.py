import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import (
    ContactMethod,
    Escalation,
    EscalationEvent,
    NotificationJob,
    NotificationRule,
    Role,
    Urgency,
    User,
)
from app.routers import account as account_mod
from app.services import oncall_notify
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session, get_db_override):
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override
    return TestClient(app)


def _login(client, db_session, email="ic@x.io", role=Role.incident_commander):
    if db_session.scalar(select(User).where(User.email == "admin@localhost")) is None:
        bootstrap_admin(db_session, "admin@localhost")
    create_user(db_session, email=email, name=email.split("@")[0], role=role, password="pw-123456")
    db_session.flush()
    client.post("/login", data={"email": email, "password": "pw-123456"})
    return db_session.scalar(select(User).where(User.email == email))


@pytest.fixture
def sms(monkeypatch):
    sent = []

    def fake(settings, to, body, **kw):
        sent.append((to, body))
        return "SM1"

    monkeypatch.setattr(account_mod.oncall_twilio, "send_sms", fake)
    return sent


def _phone(db_session, user):
    # Unverified numbers are isolated from the active delivery contact.
    return db_session.scalar(
        select(ContactMethod)
        .where(ContactMethod.user_id == user.id, ContactMethod.kind.in_(["phone_pending", "phone"]))
        .order_by(ContactMethod.kind.desc())
    )


def _code(sent):
    return sent[-1][1].split()[-1].rstrip(".")


def test_phone_verification_flow(client, db_session, sms):
    user = _login(client, db_session)
    r = client.post(
        "/account/notifications/phone", data={"phone": "+393331234567"}, follow_redirects=False
    )
    assert r.status_code == 303
    cm = _phone(db_session, user)
    db_session.refresh(cm)
    assert cm.value == "+393331234567" and cm.verified_at is None
    code = _code(sms)
    assert len(code) == 6 and code.isdigit()
    assert cm.verification_hash and code not in cm.verification_hash
    client.post("/account/notifications/phone/verify", data={"code": code})
    db_session.refresh(cm)
    assert cm.verified_at is not None and cm.verification_hash is None


def test_new_number_keeps_old_verified_until_replacement_verified(client, db_session, sms):
    user = _login(client, db_session)
    client.post("/account/notifications/phone", data={"phone": "+393331234567"})
    client.post("/account/notifications/phone/verify", data={"code": _code(sms)})
    _phone(db_session, user).verification_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.flush()
    client.post("/account/notifications/phone", data={"phone": "+393339999999"})
    rows = db_session.scalars(
        select(ContactMethod).where(ContactMethod.user_id == user.id, ContactMethod.kind == "phone")
    ).all()
    assert len(rows) == 1 and rows[0].value == "+393331234567" and rows[0].verified_at
    pending = db_session.scalar(select(ContactMethod).where(ContactMethod.kind == "phone_pending"))
    assert pending.value == "+393339999999" and pending.verified_at is None
    client.post("/account/notifications/phone/verify", data={"code": _code(sms)})
    db_session.expire_all()
    rows = db_session.scalars(
        select(ContactMethod).where(ContactMethod.user_id == user.id, ContactMethod.kind == "phone")
    ).all()
    assert len(rows) == 1 and rows[0].value == "+393339999999" and rows[0].verified_at


def test_wrong_or_expired_code_rejected(client, db_session, sms):
    user = _login(client, db_session)
    client.post("/account/notifications/phone", data={"phone": "+393331234567"})
    code = _code(sms)
    cm = _phone(db_session, user)
    wrong = "000000" if code != "000000" else "111111"
    client.post("/account/notifications/phone/verify", data={"code": wrong})
    db_session.refresh(cm)
    assert cm.verified_at is None and cm.verification_hash  # a wrong code does not consume it
    # expired
    cm.verification_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.flush()
    client.post("/account/notifications/phone/verify", data={"code": code})
    db_session.refresh(cm)
    assert cm.verified_at is None


def test_five_wrong_attempts_invalidate_code(client, db_session, sms):
    user = _login(client, db_session)
    client.post("/account/notifications/phone", data={"phone": "+393331234567"})
    code = _code(sms)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        client.post("/account/notifications/phone/verify", data={"code": wrong})
    client.post("/account/notifications/phone/verify", data={"code": code})
    cm = _phone(db_session, user)
    db_session.refresh(cm)
    assert cm.verified_at is None and cm.verification_hash is None


def test_invalid_phone_rejected(client, db_session, sms):
    user = _login(client, db_session)
    for bad in ["3331234567", "+0123456789", "+12", "+39 333 1234567", "abc"]:
        r = client.post("/account/notifications/phone", data={"phone": bad}, follow_redirects=False)
        assert r.status_code == 303
    assert _phone(db_session, user) is None and sms == []


def test_sms_failure_reported_not_500(client, db_session, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("Twilio is not configured")

    monkeypatch.setattr(account_mod.oncall_twilio, "send_sms", boom)
    _login(client, db_session)
    r = client.post("/account/notifications/phone", data={"phone": "+393331234567"})
    assert r.status_code == 200
    assert "Twilio is not configured" in r.text


def test_rules_saved_per_urgency_and_isolated_per_user(client, db_session):
    a = _login(client, db_session, "a@x.io")
    r = client.post(
        "/account/notifications/rules",
        data={"urgency": "high", "channel[]": ["sms", "email"], "delay[]": ["3", "0"]},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert oncall_notify.rules_for(db_session, a, Urgency.high) == [("email", 0), ("sms", 3)]
    assert oncall_notify.rules_for(db_session, a, Urgency.low) == oncall_notify.default_rules(
        Urgency.low
    )
    # replace in bulk
    client.post(
        "/account/notifications/rules",
        data={"urgency": "high", "channel[]": ["push"], "delay[]": ["1"]},
    )
    assert oncall_notify.rules_for(db_session, a, Urgency.high) == [("push", 1)]
    # empty = defaults
    client.post("/account/notifications/rules", data={"urgency": "high"})
    assert oncall_notify.rules_for(db_session, a, Urgency.high) == oncall_notify.default_rules(
        Urgency.high
    )
    assert db_session.scalars(select(NotificationRule)).all() == []


def test_user_cannot_edit_other_users_rules(client, db_session):
    other = _login(client, db_session, "b@x.io")
    client.post(
        "/account/notifications/rules",
        data={"urgency": "low", "channel[]": ["slack"], "delay[]": ["0"]},
    )
    client.get("/logout")
    me = _login(client, db_session, "a@x.io")
    # a user id in the payload is ignored: routes only act on the session user
    client.post(
        "/account/notifications/rules",
        data={
            "urgency": "low",
            "user_id": str(other.id),
            "channel[]": ["sms"],
            "delay[]": ["5"],
        },
    )
    assert oncall_notify.rules_for(db_session, other, Urgency.low) == [("slack", 0)]
    assert oncall_notify.rules_for(db_session, me, Urgency.low) == [("sms", 5)]


def test_invalid_rules_rejected_and_unchanged(client, db_session):
    a = _login(client, db_session)
    for data in (
        {"urgency": "low", "channel[]": ["fax"], "delay[]": ["0"]},
        {"urgency": "low", "channel[]": ["sms"], "delay[]": ["61"]},
        {"urgency": "low", "channel[]": ["sms"], "delay[]": ["x"]},
        {"urgency": "nope", "channel[]": ["sms"], "delay[]": ["1"]},
    ):
        r = client.post("/account/notifications/rules", data=data, follow_redirects=False)
        assert r.status_code == 303
    assert oncall_notify.rules_for(db_session, a, Urgency.low) == oncall_notify.default_rules(
        Urgency.low
    )


def test_push_subscription_saved_and_deduped(client, db_session):
    user = _login(client, db_session)
    sub = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "k", "auth": "a"}}
    assert client.post("/account/notifications/push", json=sub).status_code == 200
    assert client.post("/account/notifications/push", json=sub).status_code == 200
    rows = db_session.scalars(
        select(ContactMethod).where(ContactMethod.user_id == user.id, ContactMethod.kind == "push")
    ).all()
    assert len(rows) == 1 and "push.example/abc" in rows[0].value
    assert client.post("/account/notifications/push", json={"endpoint": "x"}).status_code == 400


def test_test_notification_reports_failure_reason(client, db_session, monkeypatch):
    user = _login(client, db_session)
    seen = {}

    class Boom:
        key = "sms"
        label = "SMS"

        def available(self, db):
            return True

        def reachable(self, db, user):
            return True

        def send(self, db, *, user, escalation, ack_url):
            seen["esc"] = escalation
            raise RuntimeError("No verified phone number")

    monkeypatch.setitem(oncall_notify.CHANNELS, "sms", Boom())
    r = client.post("/account/notifications/test/sms")
    assert r.status_code == 200
    assert "No verified phone number" in r.text
    assert seen["esc"].id == 0 and seen["esc"].urgency == Urgency.high
    assert db_session.scalars(select(Escalation)).all() == []
    assert db_session.scalars(select(EscalationEvent)).all() == []
    assert db_session.scalars(select(NotificationJob)).all() == []
    assert user.id
    assert client.post("/account/notifications/test/fax").status_code == 404


def test_test_notification_success(client, db_session, monkeypatch):
    _login(client, db_session)

    class Ok:
        key = "email"
        label = "Email"

        def available(self, db):
            return True

        def reachable(self, db, user):
            return True

        def send(self, db, *, user, escalation, ack_url):
            return "ref"

    monkeypatch.setitem(oncall_notify.CHANNELS, "email", Ok())
    r = client.post("/account/notifications/test/email")
    assert "Test Email sent" in r.text


def test_page_renders_for_read_only_and_shows_reasons(client, db_session):
    _login(client, db_session, "ro@x.io", Role.read_only)
    r = client.get("/account/notifications")
    assert r.status_code == 200
    assert "oncall-sw.js" in r.text and "Browser push" in r.text
    assert "Not configured" in r.text


def test_hash_is_sha256_of_code(client, db_session, sms):
    user = _login(client, db_session)
    client.post("/account/notifications/phone", data={"phone": "+393331234567"})
    cm = _phone(db_session, user)
    assert cm.verification_hash[:60] == hashlib.sha256(_code(sms).encode()).hexdigest()[:60]


def test_resend_cooldown_blocks_second_sms(client, db_session, sms):
    user = _login(client, db_session)
    client.post("/account/notifications/phone", data={"phone": "+393331234567"})
    r = client.post("/account/notifications/phone", data={"phone": "+393339999999"})
    assert "Please wait a minute" in r.text
    assert len(sms) == 1
    assert _phone(db_session, user).value == "+393331234567"


def test_resend_allowed_after_cooldown(client, db_session, sms):
    user = _login(client, db_session)
    client.post("/account/notifications/phone", data={"phone": "+393331234567"})
    cm = _phone(db_session, user)
    cm.verification_expires_at = datetime.now(UTC) + timedelta(minutes=10) - timedelta(seconds=61)
    db_session.flush()
    client.post("/account/notifications/phone", data={"phone": "+393331234567"})
    assert len(sms) == 2


def test_test_notification_through_real_email_channel(client, db_session, monkeypatch):
    _login(client, db_session)
    calls = []
    monkeypatch.setattr(oncall_notify, "send_email", lambda s, **kw: calls.append(kw))
    monkeypatch.setattr(oncall_notify.EmailChannel, "available", lambda self, db: True)
    r = client.post("/account/notifications/test/email")
    assert "Test Email sent" in r.text
    assert len(calls) == 1
    assert "Test notification from Incident Commander" in calls[0]["subject"]
    assert "Test notification from Incident Commander" in calls[0]["text_body"]
    assert db_session.scalars(select(Escalation)).all() == []
    assert db_session.scalars(select(EscalationEvent)).all() == []
    assert db_session.scalars(select(NotificationJob)).all() == []


def test_cooldown_survives_exhausted_attempts(client, db_session, sms):
    user = _login(client, db_session)
    client.post("/account/notifications/phone", data={"phone": "+393331234567"})
    code = _code(sms)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        client.post("/account/notifications/phone/verify", data={"code": wrong})
    cm = _phone(db_session, user)
    db_session.refresh(cm)
    assert cm.verification_hash is None
    r = client.post("/account/notifications/phone", data={"phone": "+393339999999"})
    assert "Please wait a minute" in r.text
    assert len(sms) == 1


def test_cooldown_survives_successful_verify(client, db_session, sms):
    _login(client, db_session)
    client.post("/account/notifications/phone", data={"phone": "+393331234567"})
    client.post("/account/notifications/phone/verify", data={"code": _code(sms)})
    r = client.post("/account/notifications/phone", data={"phone": "+393339999999"})
    assert "Please wait a minute" in r.text
    assert len(sms) == 1
