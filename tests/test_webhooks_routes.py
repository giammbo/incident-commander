import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.services.webhooks as wh
from app.db import get_db
from app.main import create_app
from app.models import Role, SeverityLevel, Webhook
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
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


def test_creating_incident_fires_webhook(client, db_session, monkeypatch):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(
        db_session, email="ic@x.io", name="IC", role=Role.incident_commander, password="pw-123456"
    )
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db_session.add_all(
        [lvl, Webhook(name="W", url="https://hooks/x", format="slack", enabled=True)]
    )
    db_session.flush()
    posts = []
    monkeypatch.setattr(
        wh,
        "httpx",
        type(
            "H",
            (),
            {"post": staticmethod(lambda url, json=None, timeout=None: posts.append((url, json)))},
        ),
    )
    client.post("/login", data={"email": "ic@x.io", "password": "pw-123456"})
    client.post(
        "/incidents",
        data={"title": "Boom", "severity_level_id": str(lvl.id)},
        headers={"HX-Request": "true"},
    )
    assert posts and any("Boom" in (j.get("text") or "") for _, j in posts)
