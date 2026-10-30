import json

import pytest
from pywebpush import WebPushException

from app.models import (
    ContactMethod,
    Escalation,
    NotificationRule,
    SlackConnection,
    Urgency,
    User,
)
from app.services import oncall_notify as n
from app.settings_store import oncall_settings, slack_settings, smtp_settings


def _user(db, email="a@x.io"):
    u = User(email=email, name="Ann")
    db.add(u)
    db.flush()
    return u


def _esc(db, urgency=Urgency.high):
    e = Escalation(
        source="manual", urgency=urgency, title="Payments 5xx above 5%", short_code="4821"
    )
    db.add(e)
    db.flush()
    return e


def test_email_channel_sends_ack_link_and_title(db_session, monkeypatch):
    u, e = _user(db_session), _esc(db_session)
    s = smtp_settings(db_session)
    s.host, s.from_address = "smtp.x", "pager@x.io"
    sent = []
    monkeypatch.setattr(n, "send_email", lambda smtp, **kw: sent.append(kw))
    ch = n.CHANNELS["email"]
    assert ch.available(db_session) and ch.reachable(db_session, u)
    url = n.ack_url("http://testserver", e, u)
    ch.send(db_session, user=u, escalation=e, ack_url=url)
    assert sent[0]["to"] == "a@x.io"
    assert "Payments 5xx above 5%" in sent[0]["subject"]
    assert url in sent[0]["text_body"] and url in sent[0]["html_body"]
    assert url.startswith("http://testserver/oncall/ack/")
    assert n.read_ack_token(url.rsplit("/", 1)[1]) == (e.id, u.id)


def test_email_unavailable_without_smtp(db_session):
    assert n.CHANNELS["email"].available(db_session) is False


class FakeWebClient:
    lookups: list = []
    posts: list = []
    opens: list = []
    users: dict = {}

    def __init__(self, token=None, **_kw):
        self.token = token

    def users_lookupByEmail(self, email):
        FakeWebClient.lookups.append((self.token, email))
        if self.token in FakeWebClient.users:
            return {"user": {"id": FakeWebClient.users[self.token]}}
        raise RuntimeError("users_not_found")

    def conversations_open(self, users):
        FakeWebClient.opens.append((self.token, users))
        return {"channel": {"id": "D1"}}

    def chat_postMessage(self, **kw):
        FakeWebClient.posts.append({"token": self.token, **kw})
        return {"ts": "123.45"}


@pytest.fixture
def fake_slack(db_session, monkeypatch):
    FakeWebClient.lookups, FakeWebClient.posts, FakeWebClient.users = [], [], {}
    FakeWebClient.opens = []
    monkeypatch.setattr(n, "WebClient", FakeWebClient)
    slack_settings(db_session).enabled = True
    db_session.add(SlackConnection(team_id="T1", bot_token="xoxb-1"))
    db_session.add(SlackConnection(team_id="T2", bot_token="xoxb-2"))
    db_session.flush()
    return FakeWebClient


def test_slack_channel_looks_up_user_by_email_once_and_caches_contact_method(
    db_session, fake_slack
):
    u, e = _user(db_session), _esc(db_session)
    fake_slack.users = {"xoxb-2": "U99"}
    ch = n.CHANNELS["slack"]
    assert ch.available(db_session) and ch.reachable(db_session, u)
    ref = ch.send(db_session, user=u, escalation=e, ack_url="http://testserver/oncall/ack/t")
    ch.send(db_session, user=u, escalation=e, ack_url="http://testserver/oncall/ack/t")
    assert [t for t, _ in fake_slack.lookups] == ["xoxb-1", "xoxb-2"]  # second send: cached
    cm = db_session.query(ContactMethod).filter_by(user_id=u.id, kind="slack").one()
    assert cm.value == "T2:U99"
    assert fake_slack.opens == [("xoxb-2", "U99")] * 2
    assert {p["token"] for p in fake_slack.posts} == {"xoxb-2"}
    assert ref == "123.45"
    blocks = fake_slack.posts[0]["blocks"]
    actions = [el for b in blocks if b["type"] == "actions" for el in b["elements"]]
    assert [a["action_id"] for a in actions] == ["oncall_ack", "oncall_escalate"]
    assert all(a["value"] == str(e.id) for a in actions)
    assert fake_slack.posts[0]["channel"] == "D1"


def test_slack_unreachable_when_user_not_found(db_session, fake_slack):
    u = _user(db_session)
    assert n.slack_user_id(db_session, u) is None
    assert n.CHANNELS["slack"].reachable(db_session, u) is False
    assert db_session.query(ContactMethod).count() == 0


def test_slack_unavailable_without_connection_or_enabled(db_session):
    assert n.CHANNELS["slack"].available(db_session) is False


def test_push_removes_subscription_on_410(db_session, monkeypatch):
    u, e = _user(db_session), _esc(db_session)
    o = oncall_settings(db_session)
    o.vapid_public_key, o.vapid_private_key = "pub", "priv"
    db_session.add(
        ContactMethod(user_id=u.id, kind="push", value=json.dumps({"endpoint": "https://p/1"}))
    )
    db_session.flush()
    calls = []

    class Resp:
        status_code = 410

    def boom(**kw):
        calls.append(kw)
        raise WebPushException("gone", response=Resp())

    monkeypatch.setattr(n.pywebpush, "webpush", boom)
    ch = n.CHANNELS["push"]
    assert ch.available(db_session) and ch.reachable(db_session, u)
    with pytest.raises(Exception):
        ch.send(db_session, user=u, escalation=e, ack_url="http://testserver/oncall/ack/t")
    assert calls[0]["vapid_private_key"] == "priv"
    assert calls[0]["vapid_claims"]["sub"] == "mailto:admin@localhost"
    assert db_session.query(ContactMethod).filter_by(kind="push").count() == 0


def test_push_success_payload(db_session, monkeypatch):
    u, e = _user(db_session), _esc(db_session)
    o = oncall_settings(db_session)
    o.vapid_public_key, o.vapid_private_key = "pub", "priv"
    db_session.add(
        ContactMethod(user_id=u.id, kind="push", value=json.dumps({"endpoint": "https://p/1"}))
    )
    db_session.flush()
    calls = []
    monkeypatch.setattr(n.pywebpush, "webpush", lambda **kw: calls.append(kw))
    n.CHANNELS["push"].send(db_session, user=u, escalation=e, ack_url="x")
    data = json.loads(calls[0]["data"])
    assert data["title"] and data["url"].endswith(f"/oncall/escalations/{e.id}")
    assert db_session.query(ContactMethod).filter_by(kind="push").count() == 1


def test_rules_for_falls_back_to_defaults_per_urgency(db_session):
    u = _user(db_session)
    assert n.rules_for(db_session, u, Urgency.high) == [
        ("email", 0),
        ("slack", 0),
        ("sms", 2),
        ("voice", 5),
    ]
    assert n.rules_for(db_session, u, Urgency.low) == [("email", 0), ("slack", 0)]
    db_session.add_all(
        [
            NotificationRule(user_id=u.id, urgency=Urgency.high, channel="push", delay_minutes=3),
            NotificationRule(user_id=u.id, urgency=Urgency.high, channel="slack", delay_minutes=0),
        ]
    )
    db_session.flush()
    assert n.rules_for(db_session, u, Urgency.high) == [("slack", 0), ("push", 3)]
    assert n.rules_for(db_session, u, Urgency.low) == [("email", 0), ("slack", 0)]


def test_ack_token_roundtrip_and_expiry():
    t = n.make_ack_token(7, 9)
    assert n.read_ack_token(t) == (7, 9)
    with pytest.raises(ValueError):
        n.read_ack_token(t, max_age=-1)
    with pytest.raises(ValueError):
        n.read_ack_token("garbage")


def test_notification_text(db_session):
    e = _esc(db_session)
    assert n.notification_text(e).startswith("[HIGH] Payments 5xx above 5% — reply ACK 4821 · ")
    assert n.notification_text(e).endswith(f"/oncall/escalations/{e.id}")


def test_slack_legacy_cached_value_is_relooked_up_and_overwritten(db_session, fake_slack):
    u, e = _user(db_session), _esc(db_session)
    fake_slack.users = {"xoxb-1": "U1"}
    db_session.add(ContactMethod(user_id=u.id, kind="slack", value="UOLD"))
    db_session.flush()
    n.CHANNELS["slack"].send(db_session, user=u, escalation=e, ack_url="x")
    cm = db_session.query(ContactMethod).filter_by(kind="slack").one()
    assert cm.value == "T1:U1"
    assert fake_slack.posts[0]["token"] == "xoxb-1"


def test_slack_cached_team_gone_relooks_up_once_then_raises(db_session, fake_slack):
    u, e = _user(db_session), _esc(db_session)
    db_session.add(ContactMethod(user_id=u.id, kind="slack", value="TGONE:U5"))
    db_session.flush()
    with pytest.raises(RuntimeError):
        n.CHANNELS["slack"].send(db_session, user=u, escalation=e, ack_url="x")
    assert fake_slack.posts == []


def test_email_provider_ref_has_no_address(db_session, monkeypatch):
    u, e = _user(db_session), _esc(db_session)
    s = smtp_settings(db_session)
    s.host, s.from_address = "smtp.x", "pager@x.io"
    monkeypatch.setattr(n, "send_email", lambda smtp, **kw: None)
    ref = n.CHANNELS["email"].send(db_session, user=u, escalation=e, ack_url="http://t/a")
    assert ref == "sent" and "@" not in ref


def test_push_webpush_called_with_timeout(db_session, monkeypatch):
    u, e = _user(db_session), _esc(db_session)
    o = oncall_settings(db_session)
    o.vapid_public_key, o.vapid_private_key = "pub", "priv"
    db_session.add(
        ContactMethod(user_id=u.id, kind="push", value=json.dumps({"endpoint": "https://p/1"}))
    )
    db_session.flush()
    calls = []
    monkeypatch.setattr(n.pywebpush, "webpush", lambda **kw: calls.append(kw))
    n.CHANNELS["push"].send(db_session, user=u, escalation=e, ack_url="x")
    assert calls[0]["timeout"] == 10


def test_slack_webclients_built_with_short_timeout(db_session, monkeypatch):
    kwargs = []

    class Capture(FakeWebClient):
        def __init__(self, token=None, **kw):
            kwargs.append(kw)
            super().__init__(token)

    monkeypatch.setattr(n, "WebClient", Capture)
    FakeWebClient.lookups, FakeWebClient.posts, FakeWebClient.opens = [], [], []
    FakeWebClient.users = {"xoxb-1": "U1"}
    slack_settings(db_session).enabled = True
    db_session.add(SlackConnection(team_id="T1", bot_token="xoxb-1"))
    u, e = _user(db_session), _esc(db_session)
    db_session.flush()
    n.CHANNELS["slack"].send(db_session, user=u, escalation=e, ack_url="http://t/a")
    assert len(kwargs) >= 2 and all(k.get("timeout") == 5 for k in kwargs)
