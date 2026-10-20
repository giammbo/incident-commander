import hashlib
import hmac
import json
from types import SimpleNamespace

import httpx

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
    generic = wh.build_payload(
        WebhookFormat.generic, text=text, incident=inc, event="opened", url="u"
    )
    assert generic["event"] == "opened" and generic["incident"]["id"] == 7
    assert generic["incident"]["title"] == "DB down" and generic["incident"]["url"] == "u"


def test_teams_payload_is_a_workflows_adaptive_card():
    payload = wh.build_payload(
        WebhookFormat.teams,
        text=":rotating_light: *SEV1* incident opened: DB down",
        incident=_inc(),
        event="opened",
        url="https://app/incidents/7",
    )
    assert payload["type"] == "message" and "@type" not in payload
    (att,) = payload["attachments"]
    assert att["contentType"] == "application/vnd.microsoft.card.adaptive"
    card = att["content"]
    assert card["type"] == "AdaptiveCard" and card["version"] == "1.4"
    title, facts = card["body"][0], card["body"][1]
    assert title["type"] == "TextBlock" and title["text"] == "Incident opened: DB down"
    assert {"title": "Severity", "value": "SEV1"} in facts["facts"]
    assert {"title": "Status", "value": "Investigating"} in facts["facts"]
    assert card["actions"] == [
        {"type": "Action.OpenUrl", "title": "Open incident", "url": "https://app/incidents/7"}
    ]


def test_teams_payload_carries_stakeholder_message():
    card = wh.build_payload(
        WebhookFormat.teams,
        text="Mitigation deployed",
        incident=_inc(),
        event="update",
        url=None,
        message="Mitigation deployed",
    )["attachments"][0]["content"]
    assert card["body"][0]["text"] == "Stakeholder update: DB down"
    assert card["body"][-1] == {"type": "TextBlock", "text": "Mitigation deployed", "wrap": True}
    assert "actions" not in card


def test_incident_message_per_event():
    inc = _inc()
    assert "opened" in wh.incident_message(inc, "opened") and "DB down" in wh.incident_message(
        inc, "opened"
    )
    assert "updated" in wh.incident_message(inc, "updated").lower()
    assert "closed" in wh.incident_message(inc, "closed").lower()


def _recorder(*responses):
    """A fake post returning (or raising) the given outcomes in order."""
    calls = []
    outcomes = list(responses)

    def post(url, content=None, headers=None, timeout=None):
        calls.append({"url": url, "content": content, "headers": headers})
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return httpx.Response(out)

    return post, calls


def test_signed_delivery_has_hmac_header_over_the_exact_body():
    body = b'{"event":"opened"}'
    post, calls = _recorder(200)
    ok = wh.deliver_one(
        wh.Delivery(name="g", url="https://h", body=body, secret="s3cret"), post=post
    )
    assert ok
    expected = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert calls[0]["content"] == body
    assert calls[0]["headers"]["X-IC-Signature"] == f"sha256={expected}"
    assert calls[0]["headers"]["Content-Type"] == "application/json"


def test_unsigned_delivery_has_no_signature_header():
    post, calls = _recorder(200)
    wh.deliver_one(wh.Delivery(name="g", url="https://h", body=b"{}"), post=post)
    assert "X-IC-Signature" not in calls[0]["headers"]


def test_retries_network_errors_and_5xx_then_succeeds():
    post, calls = _recorder(httpx.ConnectError("refused"), 503, 200)
    sleeps = []
    ok = wh.deliver_one(
        wh.Delivery(name="g", url="https://h", body=b"{}"), post=post, sleep=sleeps.append
    )
    assert ok and len(calls) == 3
    assert sleeps == list(wh.RETRY_BACKOFF)


def test_gives_up_after_two_retries():
    post, calls = _recorder(500, 502, 503, 200)
    ok = wh.deliver_one(
        wh.Delivery(name="g", url="https://h", body=b"{}"), post=post, sleep=lambda s: None
    )
    assert not ok and len(calls) == 3  # 1 attempt + 2 retries


def test_4xx_is_not_retried():
    post, calls = _recorder(400, 200)
    ok = wh.deliver_one(
        wh.Delivery(name="g", url="https://h", body=b"{}"), post=post, sleep=lambda s: None
    )
    assert not ok and len(calls) == 1


def test_build_deliveries_snapshots_enabled_hooks_with_secret(db_session):
    from app.models import Webhook

    db_session.add_all(
        [
            Webhook(
                name="g",
                url="https://hooks/g",
                format=WebhookFormat.generic,
                secret="k",
                enabled=True,
            ),
            Webhook(name="off", url="https://hooks/off", format=WebhookFormat.slack, enabled=False),
        ]
    )
    db_session.flush()
    (d,) = wh.build_deliveries(db_session, _inc(), "opened", base_url="https://app/")
    assert d.url == "https://hooks/g" and d.secret == "k"
    assert json.loads(d.body)["incident"]["url"] == "https://app/incidents/7"


def test_notify_posts_to_enabled_only_and_is_partial_failure_safe(
    db_session, monkeypatch, webhook_posts
):
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
    capture = wh._http_post

    def flaky_post(url, **kwargs):
        if "boom" in url:
            raise RuntimeError("unexpected")
        return capture(url, **kwargs)

    monkeypatch.setattr(wh, "_http_post", flaky_post)
    inc = _inc()
    wh.notify(db_session, inc, "opened", base_url="https://app")  # must NOT raise
    urls = [u for u, _ in webhook_posts]
    assert "https://hooks/ok1" in urls and "https://hooks/off" not in urls  # disabled skipped
    # the failing 'boom' webhook didn't stop 'ok1'
    assert any(j == {"text": wh.incident_message(inc, "opened")} for _, j in webhook_posts)


def test_notify_includes_stakeholder_message(db_session, webhook_posts):
    from app.models import Webhook

    db_session.add_all(
        [
            Webhook(name="s", url="https://hooks/s", format=WebhookFormat.slack, enabled=True),
            Webhook(name="g", url="https://hooks/g", format=WebhookFormat.generic, enabled=True),
        ]
    )
    db_session.flush()
    wh.notify(db_session, _inc(), "update", base_url="https://app", message="Mitigation deployed")
    by_url = dict(webhook_posts)
    assert by_url["https://hooks/s"] == {"text": "Mitigation deployed"}
    assert by_url["https://hooks/g"]["message"] == "Mitigation deployed"


def test_notify_hands_off_to_the_background_pool(db_session, monkeypatch):
    from app.models import Webhook

    db_session.add(Webhook(name="s", url="https://hooks/s", format=WebhookFormat.slack))
    db_session.flush()
    submitted = []
    monkeypatch.setattr(wh._executor, "submit", lambda fn, *a: submitted.append((fn, a)))
    monkeypatch.setattr(wh, "_http_post", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    wh.notify(db_session, _inc(), "opened", base_url="https://app")
    ((fn, (deliveries,)),) = submitted
    assert fn is wh.deliver and deliveries[0].url == "https://hooks/s"
