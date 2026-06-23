from types import SimpleNamespace

import app.services.webhooks as wh
from app.models import WebhookFormat


def _inc():
    return SimpleNamespace(
        id=7,
        title="DB down",
        status=SimpleNamespace(category=SimpleNamespace(value="active"), label="Investigating"),
        severity_level=SimpleNamespace(label="SEV1"),
    )


def test_build_payload_per_format():
    inc = _inc()
    text = "hello"
    assert wh.build_payload(
        WebhookFormat.slack, text=text, incident=inc, event="opened", url="u"
    ) == {"text": "hello"}
    assert wh.build_payload(
        WebhookFormat.discord, text=text, incident=inc, event="opened", url="u"
    ) == {"content": "hello"}
    teams = wh.build_payload(WebhookFormat.teams, text=text, incident=inc, event="opened", url="u")
    assert teams["@type"] == "MessageCard" and teams["text"] == "hello"
    generic = wh.build_payload(
        WebhookFormat.generic, text=text, incident=inc, event="opened", url="u"
    )
    assert generic["event"] == "opened" and generic["incident"]["id"] == 7
    assert generic["incident"]["title"] == "DB down" and generic["incident"]["url"] == "u"


def test_incident_message_per_event():
    inc = _inc()
    assert "opened" in wh.incident_message(inc, "opened") and "DB down" in wh.incident_message(
        inc, "opened"
    )
    assert "updated" in wh.incident_message(inc, "updated").lower()
    assert "closed" in wh.incident_message(inc, "closed").lower()


def test_notify_posts_to_enabled_only_and_is_partial_failure_safe(db_session):
    from app.models import Webhook

    db_session.add_all(
        [
            Webhook(name="ok1", url="https://hooks/ok1", format=WebhookFormat.slack, enabled=True),
            Webhook(
                name="boom", url="https://hooks/boom", format=WebhookFormat.discord, enabled=True
            ),
            Webhook(name="off", url="https://hooks/off", format=WebhookFormat.slack, enabled=False),
        ]
    )
    db_session.flush()
    posted = []

    def fake_post(url, json=None, timeout=None):
        if "boom" in url:
            raise RuntimeError("network down")
        posted.append((url, json))

    inc = _inc()
    wh.notify(db_session, inc, "opened", base_url="https://app", post=fake_post)  # must NOT raise
    urls = [u for u, _ in posted]
    assert "https://hooks/ok1" in urls and "https://hooks/off" not in urls  # disabled skipped
    # the failing 'boom' webhook didn't stop 'ok1'
    assert any(j == {"text": wh.incident_message(inc, "opened")} for _, j in posted)


def test_notify_includes_stakeholder_message(db_session):
    from app.models import Webhook

    db_session.add_all(
        [
            Webhook(name="s", url="https://hooks/s", format=WebhookFormat.slack, enabled=True),
            Webhook(name="g", url="https://hooks/g", format=WebhookFormat.generic, enabled=True),
        ]
    )
    db_session.flush()
    posted = []
    inc = _inc()
    wh.notify(
        db_session,
        inc,
        "update",
        base_url="https://app",
        message="Mitigation deployed",
        post=lambda url, json=None, timeout=None: posted.append((url, json)),
    )
    by_url = dict(posted)
    assert by_url["https://hooks/s"] == {"text": "Mitigation deployed"}
    assert by_url["https://hooks/g"]["message"] == "Mitigation deployed"
