"""Public inbound acknowledgement endpoints: email link, Slack buttons, Twilio voice/SMS."""

import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.db import get_db
from app.main import create_app
from app.models import (
    ContactMethod,
    Escalation,
    EscalationEvent,
    EscalationStatus,
    NotificationJob,
    Role,
    SlackConnection,
    Urgency,
)
from app.routers import oncall_inbound as inbound
from app.services import oncall_engine as eng
from app.services.oncall_notify import make_ack_token
from app.services.users import create_user
from app.settings_store import oncall_settings, slack_settings

BASE = "http://testserver"
SIGNING_SECRET = "slack-signing-secret-xyz"
TWILIO_TOKEN = "twilio-auth-token-abc"
PHONE = "+15550001111"


@pytest.fixture
def client(db_session, get_db_override):
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override
    return TestClient(app)


@pytest.fixture
def ro(db_session):
    u = create_user(db_session, email="ro@x.io", name="Ro", role=Role.read_only, password="pw-1")
    db_session.commit()
    return u


def _esc(db_session, user, *, title="DB down", notified=True):
    now = datetime.now(UTC)
    esc = eng.start_escalation(
        db_session,
        urgency=Urgency.high,
        title=title,
        user_ids=[user.id],
        source="manual",
        now=now,
    )
    if notified:
        db_session.add(
            NotificationJob(
                escalation_id=esc.id,
                kind="notify",
                run_at=now,
                status="sent",
                user_id=user.id,
                channel="email",
                level=0,
            )
        )
    db_session.commit()
    return esc


def _status(db_session, esc) -> EscalationStatus:
    db_session.expire_all()
    return db_session.get(Escalation, esc.id).status


def _event_bodies(db_session, esc) -> list[str]:
    return list(
        db_session.scalars(
            select(EscalationEvent.body).where(EscalationEvent.escalation_id == esc.id)
        )
    )


# ---- email link ---------------------------------------------------------------


def test_email_link_get_does_not_ack_post_does(client, db_session, ro):
    esc = _esc(db_session, ro, title="Disk <full> & more")
    token = make_ack_token(esc.id, ro.id)

    r = client.get(f"/oncall/ack/{token}")
    assert r.status_code == 200
    assert "Disk &lt;full&gt; &amp; more" in r.text
    assert "triggered" in r.text.lower()
    assert f'action="/oncall/ack/{token}"' in r.text
    assert _status(db_session, esc) == EscalationStatus.triggered  # link scanners are harmless

    r = client.post(f"/oncall/ack/{token}")
    assert r.status_code == 200
    assert "acknowledged" in r.text.lower()
    db_session.expire_all()
    row = db_session.get(Escalation, esc.id)
    assert row.status == EscalationStatus.acknowledged
    assert row.acked_by == ro.id
    assert any("via email" in b for b in _event_bodies(db_session, esc))

    # Single-use in effect: a second POST does nothing and says so.
    r = client.post(f"/oncall/ack/{token}")
    assert r.status_code == 200
    assert "already" in r.text.lower()


def test_expired_token_rejected(client, db_session, ro, monkeypatch):
    esc = _esc(db_session, ro)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() - 2 * 86400)
    old = make_ack_token(esc.id, ro.id)
    monkeypatch.setattr(time, "time", real)

    for tok in (old, "garbage-token", make_ack_token(esc.id, ro.id) + "x"):
        r = client.get(f"/oncall/ack/{tok}")
        assert r.status_code == 400
        assert "expired" in r.text.lower() or "invalid" in r.text.lower()
        r = client.post(f"/oncall/ack/{tok}")
        assert r.status_code == 400
    assert _status(db_session, esc) == EscalationStatus.triggered


def test_email_link_for_unnotified_or_inactive_user_does_not_ack(client, db_session, ro):
    other = create_user(db_session, email="o@x.io", name="O", role=Role.read_only, password="pw-1")
    db_session.commit()
    esc = _esc(db_session, ro)
    r = client.post(f"/oncall/ack/{make_ack_token(esc.id, other.id)}")
    assert r.status_code == 403
    ro.is_active = False
    db_session.commit()
    r = client.post(f"/oncall/ack/{make_ack_token(esc.id, ro.id)}")
    assert r.status_code == 403
    assert _status(db_session, esc) == EscalationStatus.triggered


# ---- slack --------------------------------------------------------------------


class FakeSlack:
    emails: dict[str, str] = {}
    info_calls: list = []

    def __init__(self, token):
        self.token = token

    def users_info(self, user):
        FakeSlack.info_calls.append((self.token, user))
        email = FakeSlack.emails.get(user)
        if email is None:
            raise RuntimeError("user_not_found")
        return {"user": {"id": user, "profile": {"email": email}}}


@pytest.fixture
def slack(db_session, monkeypatch):
    s = slack_settings(db_session)
    s.enabled = True
    s.signing_secret = SIGNING_SECRET
    db_session.add(SlackConnection(team_id="T1", bot_token="xoxb-1"))
    db_session.commit()
    FakeSlack.emails, FakeSlack.info_calls = {}, []
    monkeypatch.setattr(inbound, "_slack_client", FakeSlack)
    posts = []
    monkeypatch.setattr(inbound, "_http_post", lambda url, **kw: posts.append((url, kw["json"])))
    return posts


def _slack_body(esc_id, *, user="U1", team="T1", action="oncall_ack") -> bytes:
    payload = {
        "type": "block_actions",
        "user": {"id": user, "team_id": team},
        "team": {"id": team},
        "actions": [{"action_id": action, "value": str(esc_id)}],
        "response_url": "https://hooks.slack.com/actions/T1/1/abc",
    }
    return urlencode({"payload": json.dumps(payload)}).encode()


def _slack_post(client, body: bytes, *, ts=None, secret=SIGNING_SECRET, sig=None):
    ts = str(int(time.time())) if ts is None else str(ts)
    base = b"v0:" + ts.encode() + b":" + body
    good = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": sig if sig is not None else good,
    }
    return client.post("/slack/interactions", content=body, headers=headers)


def test_slack_interaction_bad_signature_401_and_stale_timestamp_401(client, db_session, ro, slack):
    db_session.add(ContactMethod(user_id=ro.id, kind="slack", value="T1:U1"))
    db_session.commit()
    esc = _esc(db_session, ro)
    body = _slack_body(esc.id)

    r = client.post(
        "/slack/interactions",
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 401  # missing headers
    assert _slack_post(client, body, sig="v0=deadbeef").status_code == 401
    assert _slack_post(client, body, secret="wrong-secret").status_code == 401
    assert _slack_post(client, body, ts="not-a-number").status_code == 401
    assert _slack_post(client, body, ts=int(time.time()) - 6 * 60).status_code == 401
    assert _slack_post(client, body, ts=int(time.time()) + 6 * 60).status_code == 401
    assert _status(db_session, esc) == EscalationStatus.triggered
    assert slack == []


def test_slack_ack_button_acks_and_maps_user_by_contact_method(client, db_session, ro, slack):
    db_session.add(ContactMethod(user_id=ro.id, kind="slack", value="T1:U1"))
    db_session.commit()
    esc = _esc(db_session, ro)

    r = _slack_post(client, _slack_body(esc.id))
    assert r.status_code == 200
    db_session.expire_all()
    row = db_session.get(Escalation, esc.id)
    assert row.status == EscalationStatus.acknowledged
    assert row.acked_by == ro.id
    assert any("via slack" in b for b in _event_bodies(db_session, esc))
    assert FakeSlack.info_calls == []  # mapped by the cached contact method
    (url, body) = slack[-1]
    assert url.startswith("https://hooks.slack.com/")
    assert body["replace_original"] is True
    assert "acknowledged" in body["text"].lower()
    assert "Ro" in body["text"]


def test_slack_escalate_button_escalates(client, db_session, ro, slack):
    db_session.add(ContactMethod(user_id=ro.id, kind="slack", value="T1:U1"))
    db_session.commit()
    esc = _esc(db_session, ro)
    r = _slack_post(client, _slack_body(esc.id, action="oncall_escalate"))
    assert r.status_code == 200
    assert _status(db_session, esc) == EscalationStatus.triggered
    assert any("Escalated now by Ro" in b for b in _event_bodies(db_session, esc))
    assert "escalated" in slack[-1][1]["text"].lower()


def test_slack_maps_user_by_email_and_caches_pair(client, db_session, ro, slack):
    esc = _esc(db_session, ro)
    FakeSlack.emails = {"U9": "RO@x.io"}
    r = _slack_post(client, _slack_body(esc.id, user="U9"))
    assert r.status_code == 200
    assert _status(db_session, esc) == EscalationStatus.acknowledged
    assert FakeSlack.info_calls == [("xoxb-1", "U9")]
    cms = db_session.scalars(
        select(ContactMethod).where(ContactMethod.user_id == ro.id, ContactMethod.kind == "slack")
    ).all()
    assert [c.value for c in cms] == ["T1:U9"]


def test_slack_unknown_user_gets_error_no_ack(client, db_session, ro, slack):
    esc = _esc(db_session, ro)
    FakeSlack.emails = {"U7": "stranger@elsewhere.io"}
    r = _slack_post(client, _slack_body(esc.id, user="U7"))
    assert r.status_code == 200
    assert _status(db_session, esc) == EscalationStatus.triggered
    (url, body) = slack[-1]
    assert body["response_type"] == "ephemeral"
    assert body.get("replace_original") is False
    assert "stranger@elsewhere.io" not in body["text"]
    # Unknown workspace: no lookup at all, still an error and no action.
    slack.clear()
    r = _slack_post(client, _slack_body(esc.id, user="U1", team="TOTHER"))
    assert r.status_code == 200
    assert slack[-1][1]["response_type"] == "ephemeral"
    assert _status(db_session, esc) == EscalationStatus.triggered


def test_slack_mapped_but_not_paged_user_cannot_act(client, db_session, ro, slack):
    other = create_user(db_session, email="o@x.io", name="O", role=Role.read_only, password="pw-1")
    db_session.add(ContactMethod(user_id=other.id, kind="slack", value="T1:U2"))
    db_session.commit()
    esc = _esc(db_session, ro)
    r = _slack_post(client, _slack_body(esc.id, user="U2"))
    assert r.status_code == 200
    assert _status(db_session, esc) == EscalationStatus.triggered
    assert slack[-1][1]["response_type"] == "ephemeral"


def test_slack_busy_when_escalation_row_is_locked(client, db_session, ro, slack, pg_engine):
    db_session.add(ContactMethod(user_id=ro.id, kind="slack", value="T1:U1"))
    db_session.commit()
    esc = _esc(db_session, ro)
    with pg_engine.connect() as other:
        other.execute(text("SELECT id FROM escalations WHERE id = :i FOR UPDATE"), {"i": esc.id})
        started = time.monotonic()
        r = _slack_post(client, _slack_body(esc.id))
        elapsed = time.monotonic() - started
        other.rollback()
    assert r.status_code == 200
    assert elapsed < 2.9  # within Slack's 3-second budget
    assert _status(db_session, esc) == EscalationStatus.triggered
    body = slack[-1][1]
    assert body["response_type"] == "ephemeral"
    assert "try again" in body["text"].lower()


def test_slack_rejects_non_slack_response_url(client, db_session, ro, slack):
    db_session.add(ContactMethod(user_id=ro.id, kind="slack", value="T1:U1"))
    db_session.commit()
    esc = _esc(db_session, ro)
    payload = {
        "type": "block_actions",
        "user": {"id": "U1", "team_id": "T1"},
        "team": {"id": "T1"},
        "actions": [{"action_id": "oncall_ack", "value": str(esc.id)}],
        "response_url": "http://169.254.169.254/latest/meta-data",
    }
    body = urlencode({"payload": json.dumps(payload)}).encode()
    r = _slack_post(client, body)
    assert r.status_code == 200
    assert slack == []  # never POSTs to a non-Slack URL


# ---- twilio -------------------------------------------------------------------


@pytest.fixture
def twilio(db_session):
    s = oncall_settings(db_session)
    s.twilio_account_sid = "AC1"
    s.twilio_auth_token = TWILIO_TOKEN
    s.twilio_from_number = "+15559990000"
    db_session.commit()


def _twilio_post(client, path, params, *, token=TWILIO_TOKEN, sig=None):
    url = BASE + path
    data = url + "".join(k + params[k] for k in sorted(params))
    good = base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest())
    headers = {"X-Twilio-Signature": sig if sig is not None else good.decode()}
    return client.post(path, data=params, headers=headers)


def _phone(db_session, user, *, verified=True, value=PHONE):
    db_session.add(
        ContactMethod(
            user_id=user.id,
            kind="phone",
            value=value,
            verified_at=datetime.now(UTC) if verified else None,
        )
    )
    db_session.commit()


def test_twilio_voice_digit_1_acks(client, db_session, ro, twilio):
    esc = _esc(db_session, ro, title="Disk <full> & more")
    r = _twilio_post(client, f"/oncall/twilio/voice/{esc.id}/{ro.id}", {"CallSid": "CA1"})
    assert r.status_code == 200
    assert "xml" in r.headers["content-type"]
    assert f'<Gather numDigits="1" action="/oncall/twilio/gather/{esc.id}/{ro.id}"' in r.text
    assert "Disk &lt;full&gt; &amp; more" in r.text
    assert "press 1" in r.text.lower()

    r = _twilio_post(client, f"/oncall/twilio/gather/{esc.id}/{ro.id}", {"Digits": "2"})
    assert r.status_code == 200
    assert "<Gather" in r.text  # repeats the message
    assert _status(db_session, esc) == EscalationStatus.triggered

    r = _twilio_post(client, f"/oncall/twilio/gather/{esc.id}/{ro.id}", {"Digits": "1"})
    assert r.status_code == 200
    assert "Acknowledged" in r.text
    assert "<Gather" not in r.text
    db_session.expire_all()
    row = db_session.get(Escalation, esc.id)
    assert row.status == EscalationStatus.acknowledged and row.acked_by == ro.id
    assert any("via voice" in b for b in _event_bodies(db_session, esc))


def test_twilio_voice_test_call_has_no_gather(client, db_session, ro, twilio):
    r = _twilio_post(client, f"/oncall/twilio/voice/0/{ro.id}", {"CallSid": "CA1"})
    assert r.status_code == 200
    assert "This is a test call from Incident Commander." in r.text
    assert "<Gather" not in r.text


def test_twilio_gather_from_unpaged_user_does_not_ack(client, db_session, ro, twilio):
    other = create_user(db_session, email="o@x.io", name="O", role=Role.read_only, password="pw-1")
    db_session.commit()
    esc = _esc(db_session, ro)
    r = _twilio_post(client, f"/oncall/twilio/gather/{esc.id}/{other.id}", {"Digits": "1"})
    assert r.status_code == 200
    assert _status(db_session, esc) == EscalationStatus.triggered


def test_twilio_sms_ack_with_code(client, db_session, ro, twilio):
    _phone(db_session, ro)
    esc = _esc(db_session, ro)
    r = _twilio_post(
        client, "/oncall/twilio/sms", {"From": PHONE, "Body": f"  ack {esc.short_code} "}
    )
    assert r.status_code == 200
    assert "<Message>" in r.text
    assert esc.short_code in r.text
    db_session.expire_all()
    row = db_session.get(Escalation, esc.id)
    assert row.status == EscalationStatus.acknowledged and row.acked_by == ro.id
    assert any("via sms" in b for b in _event_bodies(db_session, esc))
    assert PHONE not in r.text


def test_twilio_sms_bare_ack_ambiguous_lists_codes_no_ack(client, db_session, ro, twilio):
    _phone(db_session, ro)
    a = _esc(db_session, ro, title="A")
    b = _esc(db_session, ro, title="B")
    r = _twilio_post(client, "/oncall/twilio/sms", {"From": PHONE, "Body": "ACK"})
    assert r.status_code == 200
    assert "<Message>" in r.text
    assert a.short_code in r.text and b.short_code in r.text
    assert _status(db_session, a) == EscalationStatus.triggered
    assert _status(db_session, b) == EscalationStatus.triggered


def test_twilio_sms_bare_ack_single_open_acks(client, db_session, ro, twilio):
    _phone(db_session, ro)
    esc = _esc(db_session, ro)
    r = _twilio_post(client, "/oncall/twilio/sms", {"From": PHONE, "Body": "Ack"})
    assert r.status_code == 200
    assert _status(db_session, esc) == EscalationStatus.acknowledged


def test_sms_ack_from_unverified_or_unnotified_number_rejected(client, db_session, ro, twilio):
    _phone(db_session, ro, verified=False)
    esc = _esc(db_session, ro)
    r = _twilio_post(client, "/oncall/twilio/sms", {"From": PHONE, "Body": f"ACK {esc.short_code}"})
    assert r.status_code == 200
    assert _status(db_session, esc) == EscalationStatus.triggered

    # Verified number of a user who was not notified for that escalation.
    other = create_user(
        db_session, email="o@x.io", name="O", role=Role.incident_commander, password="pw-1"
    )
    db_session.commit()
    _phone(db_session, other, value="+15550002222")
    r = _twilio_post(
        client, "/oncall/twilio/sms", {"From": "+15550002222", "Body": f"ACK {esc.short_code}"}
    )
    assert r.status_code == 200
    assert _status(db_session, esc) == EscalationStatus.triggered
    r = _twilio_post(client, "/oncall/twilio/sms", {"From": "+15550002222", "Body": "ACK"})
    assert _status(db_session, esc) == EscalationStatus.triggered
    assert esc.short_code not in r.text


def test_twilio_bad_signature_403(client, db_session, ro, twilio):
    _phone(db_session, ro)
    esc = _esc(db_session, ro)
    sms = {"From": PHONE, "Body": f"ACK {esc.short_code}"}
    assert _twilio_post(client, "/oncall/twilio/sms", sms, sig="bogus").status_code == 403
    assert _twilio_post(client, "/oncall/twilio/sms", sms, token="wrong").status_code == 403
    r = client.post("/oncall/twilio/sms", data=sms)  # no header
    assert r.status_code == 403
    gather = f"/oncall/twilio/gather/{esc.id}/{ro.id}"
    assert _twilio_post(client, gather, {"Digits": "1"}, sig="bogus").status_code == 403
    voice = f"/oncall/twilio/voice/{esc.id}/{ro.id}"
    assert _twilio_post(client, voice, {}, sig="bogus").status_code == 403
    assert _status(db_session, esc) == EscalationStatus.triggered


def test_twilio_not_configured_403(client, db_session, ro):
    esc = _esc(db_session, ro)
    r = _twilio_post(client, f"/oncall/twilio/gather/{esc.id}/{ro.id}", {"Digits": "1"})
    assert r.status_code == 403
    assert _status(db_session, esc) == EscalationStatus.triggered


# ---- csrf ---------------------------------------------------------------------


def test_public_inbound_paths_are_csrf_exempt_but_ack_form_is_not(client, db_session, ro):
    foreign = {"Origin": "https://evil.example"}
    r = client.post("/slack/interactions", content=b"", headers=foreign)
    assert r.status_code == 401  # reached the handler (signature), not the CSRF 403
    r = client.post("/oncall/twilio/sms", data={}, headers=foreign)
    assert r.status_code == 403 and "CSRF" not in r.text
    esc = _esc(db_session, ro)
    r = client.post(f"/oncall/ack/{make_ack_token(esc.id, ro.id)}", headers=foreign)
    assert r.status_code == 403 and "CSRF" in r.text
    assert _status(db_session, esc) == EscalationStatus.triggered


# ---- fix round 1 ------------------------------------------------------------------


def test_email_ack_form_post_from_real_browser_origin_acks(client, db_session, ro):
    esc = _esc(db_session, ro)
    token = make_ack_token(esc.id, ro.id)
    r = client.get(f"/oncall/ack/{token}")
    assert r.status_code == 200
    # no-referrer would make browsers send "Origin: null" on the form POST.
    assert r.headers["Referrer-Policy"] != "no-referrer"
    assert r.headers["Referrer-Policy"] == "same-origin"

    r = client.post(f"/oncall/ack/{token}", headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    assert _status(db_session, esc) == EscalationStatus.acknowledged


def test_email_ack_form_post_with_null_origin_still_rejected(client, db_session, ro):
    esc = _esc(db_session, ro)
    r = client.post(f"/oncall/ack/{make_ack_token(esc.id, ro.id)}", headers={"Origin": "null"})
    assert r.status_code == 403 and "CSRF" in r.text
    assert _status(db_session, esc) == EscalationStatus.triggered


def test_slack_escalate_is_debounced_for_30_seconds(client, db_session, ro, slack, monkeypatch):
    db_session.add(ContactMethod(user_id=ro.id, kind="slack", value="T1:U1"))
    db_session.commit()
    esc = _esc(db_session, ro)
    t0 = datetime.now(UTC)
    clock = {"now": t0}
    monkeypatch.setattr(inbound, "_now", lambda: clock["now"])

    def escalated_count():
        db_session.expire_all()
        return sum(1 for b in _event_bodies(db_session, esc) if b.startswith("Escalated now"))

    body = _slack_body(esc.id, action="oncall_escalate")
    assert _slack_post(client, body).status_code == 200
    assert escalated_count() == 1

    clock["now"] = t0 + timedelta(seconds=5)  # double click / replay
    assert _slack_post(client, body).status_code == 200
    assert escalated_count() == 1
    assert slack[-1][1]["response_type"] == "ephemeral"
    assert "already escalated" in slack[-1][1]["text"].lower()

    clock["now"] = t0 + timedelta(seconds=31)
    assert _slack_post(client, body).status_code == 200
    assert escalated_count() == 2


def test_twilio_sms_ack_code_without_space(client, db_session, ro, twilio):
    _phone(db_session, ro)
    esc = _esc(db_session, ro)
    r = _twilio_post(
        client, "/oncall/twilio/sms", {"From": PHONE, "Body": f" ACK{esc.short_code} "}
    )
    assert r.status_code == 200
    assert _status(db_session, esc) == EscalationStatus.acknowledged
