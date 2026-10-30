import base64
import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.models import ContactMethod, Escalation, Role, Urgency, User
from app.services import oncall_notify as n
from app.services import oncall_twilio as tw
from app.services.users import bootstrap_admin, create_user
from app.settings_store import oncall_settings


@pytest.fixture
def client(db_session, get_db_override):
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override
    return TestClient(app)


def _login(client, db_session, *, admin):
    _, pw = bootstrap_admin(db_session, "admin@localhost")
    db_session.flush()
    if admin:
        client.post("/login", data={"email": "admin@localhost", "password": pw})
        client.post(
            "/account/password",
            data={"new_password": "Admin-123", "confirm": "Admin-123", "name": "Admin"},
        )
    else:
        create_user(
            db_session,
            email="ic@x.io",
            name="IC",
            role=Role.incident_commander,
            password="pw-123456",
        )
        db_session.flush()
        client.post("/login", data={"email": "ic@x.io", "password": "pw-123456"})


# Example from Twilio's public "Validating requests" documentation.
REF_URL = "https://mycompany.com/myapp.php?foo=1&bar=2"
REF_PARAMS = {
    "CallSid": "CA1234567890ABCDE",
    "Caller": "+14158675310",
    "Digits": "1234",
    "From": "+14158675310",
    "To": "+18005551212",
}
REF_TOKEN = "12345"
REF_SIG = "GvWf1cFY/Q7PnoempGyD5oXAezc="


def test_valid_signature_matches_twilio_reference_example():
    assert tw.valid_signature(REF_TOKEN, REF_URL, REF_PARAMS, REF_SIG)
    assert not tw.valid_signature(REF_TOKEN, REF_URL, REF_PARAMS, "bogus=")
    assert not tw.valid_signature("other", REF_URL, REF_PARAMS, REF_SIG)
    # independent reimplementation
    data = REF_URL + "".join(k + REF_PARAMS[k] for k in sorted(REF_PARAMS))
    expected = base64.b64encode(hmac.new(b"12345", data.encode(), hashlib.sha1).digest()).decode()
    assert expected == REF_SIG


class Resp:
    def __init__(self, status=201, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body


def _configured(db):
    s = oncall_settings(db)
    s.twilio_account_sid, s.twilio_auth_token, s.twilio_from_number = "ACx", "tok", "+15550001"
    db.flush()
    return s


def test_send_sms_posts_form_with_basic_auth(db_session):
    s = _configured(db_session)
    calls = []

    def post(url, **kw):
        calls.append((url, kw))
        return Resp(201, {"sid": "SM123"})

    assert tw.send_sms(s, "+39333", "hello", post=post) == "SM123"
    url, kw = calls[0]
    assert url == "https://api.twilio.com/2010-04-01/Accounts/ACx/Messages.json"
    assert kw["auth"] == ("ACx", "tok")
    assert kw["data"] == {"To": "+39333", "From": "+15550001", "Body": "hello"}


def test_place_call_and_error_hides_token(db_session):
    s = _configured(db_session)
    calls = []
    sid = tw.place_call(
        s,
        "+39333",
        "http://x/v",
        post=lambda u, **kw: (calls.append((u, kw)), Resp(201, {"sid": "CA1"}))[1],
    )
    assert sid == "CA1"
    assert calls[0][0].endswith("/Accounts/ACx/Calls.json")
    assert calls[0][1]["data"] == {"To": "+39333", "From": "+15550001", "Url": "http://x/v"}
    with pytest.raises(RuntimeError) as ei:
        tw.send_sms(
            s, "+1", "b", post=lambda u, **kw: Resp(400, {"code": 21211, "message": "Bad To"})
        )
    assert "21211" in str(ei.value) and "Bad To" in str(ei.value) and "tok" not in str(ei.value)


def test_sms_channel_unavailable_without_twilio_settings(db_session):
    assert n.CHANNELS["sms"].available(db_session) is False
    assert n.CHANNELS["voice"].available(db_session) is False
    _configured(db_session)
    assert n.CHANNELS["sms"].available(db_session) is True
    assert n.CHANNELS["voice"].available(db_session) is True


def _user_esc(db):
    u = User(email="a@x.io", name="Ann")
    e = Escalation(source="manual", urgency=Urgency.high, title="T", short_code="4821")
    db.add_all([u, e])
    db.flush()
    return u, e


def test_voice_unreachable_without_verified_phone(db_session):
    u, _ = _user_esc(db_session)
    assert n.CHANNELS["voice"].reachable(db_session, u) is False
    cm = ContactMethod(user_id=u.id, kind="phone", value="+39333")
    db_session.add(cm)
    db_session.flush()
    assert n.CHANNELS["voice"].reachable(db_session, u) is False
    assert n.CHANNELS["sms"].reachable(db_session, u) is False
    from datetime import UTC, datetime

    cm.verified_at = datetime.now(UTC)
    db_session.flush()
    assert n.CHANNELS["voice"].reachable(db_session, u) is True
    assert n.CHANNELS["sms"].reachable(db_session, u) is True


def test_sms_and_voice_send(db_session, monkeypatch):
    from datetime import UTC, datetime

    u, e = _user_esc(db_session)
    _configured(db_session)
    db_session.add(
        ContactMethod(user_id=u.id, kind="phone", value="+39333", verified_at=datetime.now(UTC))
    )
    db_session.flush()
    sms, call = [], []
    monkeypatch.setattr(
        tw, "send_sms", lambda s, to, body, **kw: (sms.append((to, body)), "SM1")[1]
    )
    monkeypatch.setattr(
        tw, "place_call", lambda s, to, url, **kw: (call.append((to, url)), "CA1")[1]
    )
    assert n.CHANNELS["sms"].send(db_session, user=u, escalation=e, ack_url="x") == "SM1"
    assert sms[0] == ("+39333", n.notification_text(e))
    assert n.CHANNELS["voice"].send(db_session, user=u, escalation=e, ack_url="x") == "CA1"
    assert call[0][1].endswith(f"/oncall/twilio/voice/{e.id}/{u.id}")


def test_settings_oncall_keeps_token_when_blank_and_is_admin_only(client, db_session):
    _login(client, db_session, admin=False)
    r = client.post("/settings/oncall", data={"twilio_account_sid": "AC1"}, follow_redirects=False)
    assert r.status_code in (302, 303, 403) and r.headers.get("location") != "/settings"
    assert oncall_settings(db_session).twilio_account_sid is None


def test_settings_oncall_admin_saves_and_keeps_token(client, db_session):
    _login(client, db_session, admin=True)
    client.post(
        "/settings/oncall",
        data={
            "twilio_account_sid": "AC1",
            "twilio_auth_token": "t1",
            "twilio_from_number": "+1555",
        },
        follow_redirects=False,
    )
    s = oncall_settings(db_session)
    assert (s.twilio_account_sid, s.twilio_auth_token, s.twilio_from_number) == (
        "AC1",
        "t1",
        "+1555",
    )
    client.post(
        "/settings/oncall",
        data={"twilio_account_sid": "AC2", "twilio_auth_token": "", "twilio_from_number": "+1555"},
        follow_redirects=False,
    )
    db_session.expire_all()
    s = oncall_settings(db_session)
    assert s.twilio_account_sid == "AC2" and s.twilio_auth_token == "t1"
    page = client.get("/settings").text
    assert "t1" not in page and "/slack/interactions" in page


def test_push_keys_generated_once(client, db_session):
    _login(client, db_session, admin=True)
    client.post("/settings/oncall/push-keys", follow_redirects=False)
    db_session.expire_all()
    s = oncall_settings(db_session)
    pub, priv = s.vapid_public_key, s.vapid_private_key
    assert pub and priv
    assert len(base64.urlsafe_b64decode(pub + "==")) == 65
    from py_vapid import Vapid

    Vapid.from_string(priv)  # format pywebpush accepts
    client.post("/settings/oncall/push-keys", follow_redirects=False)
    db_session.expire_all()
    s = oncall_settings(db_session)
    assert (s.vapid_public_key, s.vapid_private_key) == (pub, priv)
    assert priv not in client.get("/settings").text
