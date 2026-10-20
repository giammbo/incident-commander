import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.services.webhooks as wh
from app.db import get_db
from app.main import create_app
from app.models import Role, SeverityLevel, Webhook
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session, get_db_override):
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override
    return TestClient(app)


def _admin(client, db_session):
    _, pw = bootstrap_admin(db_session, "admin@localhost")
    db_session.flush()
    client.post("/login", data={"email": "admin@localhost", "password": pw})
    client.post("/account/password", data={"new_password": "Admin-123", "confirm": "Admin-123"})


def test_admin_creates_and_deletes_webhook(client, db_session):
    _admin(client, db_session)
    r = client.post(
        "/settings/webhooks",
        data={"name": "Teams", "url": "https://outlook/hook", "format": "teams"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    w = db_session.scalar(select(Webhook).where(Webhook.name == "Teams"))
    assert w is not None and w.url == "https://outlook/hook"
    r2 = client.post(f"/settings/webhooks/{w.id}/delete", follow_redirects=False)
    assert r2.status_code == 303
    assert db_session.scalar(select(Webhook).where(Webhook.name == "Teams")) is None


def test_readonly_cannot_manage_webhooks(client, db_session):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(db_session, email="ro@x.io", name="RO", role=Role.read_only, password="pw-123456")
    db_session.flush()
    client.post("/login", data={"email": "ro@x.io", "password": "pw-123456"})
    assert (
        client.post(
            "/settings/webhooks", data={"name": "x", "url": "y", "format": "slack"}
        ).status_code
        == 403
    )


def test_creating_incident_fires_webhook(client, db_session, webhook_posts):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(
        db_session, email="ic@x.io", name="IC", role=Role.incident_commander, password="pw-123456"
    )
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db_session.add_all(
        [lvl, Webhook(name="W", url="https://hooks/x", format="slack", enabled=True)]
    )
    db_session.flush()
    client.post("/login", data={"email": "ic@x.io", "password": "pw-123456"})
    client.post(
        "/incidents",
        data={"title": "Boom", "severity_level_id": str(lvl.id)},
        headers={"HX-Request": "true"},
    )
    assert webhook_posts and any("Boom" in (j.get("text") or "") for _, j in webhook_posts)


def test_route_response_does_not_wait_for_webhooks(client, db_session, monkeypatch):
    # The delivery is parked on an Event: if the route waited for it, the request
    # would hang until the 5s wait times out instead of returning straight away.
    bootstrap_admin(db_session, "admin@localhost")
    create_user(
        db_session, email="ic@x.io", name="IC", role=Role.incident_commander, password="pw-123456"
    )
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db_session.add_all(
        [lvl, Webhook(name="W", url="https://hooks/slow", format="slack", enabled=True)]
    )
    db_session.flush()
    release, started, done = threading.Event(), threading.Event(), threading.Event()

    def slow_post(url, **kwargs):
        started.set()
        release.wait(5)
        done.set()
        return httpx.Response(200)

    monkeypatch.setattr(wh, "_http_post", slow_post)
    client.post("/login", data={"email": "ic@x.io", "password": "pw-123456"})
    t0 = time.monotonic()
    r = client.post(
        "/incidents",
        data={"title": "Slow hook", "severity_level_id": str(lvl.id)},
        headers={"HX-Request": "true"},
    )
    elapsed = time.monotonic() - t0
    assert r.status_code == 200
    assert started.wait(2), "webhook delivery never started"
    assert not done.is_set() and elapsed < 4, "response waited for the webhook"
    release.set()
    assert done.wait(2)


def test_admin_creates_signed_webhook(client, db_session):
    _admin(client, db_session)
    client.post(
        "/settings/webhooks",
        data={"name": "Signed", "url": "https://hooks/s", "format": "generic", "secret": " k3y "},
        follow_redirects=False,
    )
    client.post(
        "/settings/webhooks",
        data={"name": "Plain", "url": "https://hooks/p", "format": "generic", "secret": "  "},
        follow_redirects=False,
    )
    signed = db_session.scalar(select(Webhook).where(Webhook.name == "Signed"))
    plain = db_session.scalar(select(Webhook).where(Webhook.name == "Plain"))
    assert signed.secret == "k3y" and plain.secret is None
    page = client.get("/settings").text
    assert "k3y" not in page  # the secret is write-only
