from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.routers.connections as conn_router
from app.db import get_db
from app.main import create_app
from app.models import GoogleConnection
from app.services.users import bootstrap_admin
from app.settings_store import google_settings


@pytest.fixture
def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def test_google_callback_stores_connection(client, db_session, monkeypatch):
    _, pw = bootstrap_admin(db_session, "admin@localhost")
    g = google_settings(db_session)
    g.client_id, g.client_secret, g.enabled = "gid", "gsec", True
    db_session.flush()
    client.post("/login", data={"email": "admin@localhost", "password": pw})
    client.post("/account/password", data={"new_password": "Admin-123", "confirm": "Admin-123"})

    monkeypatch.setattr(
        conn_router,
        "google_exchange_code",
        lambda **k: {"refresh_token": "rt-1", "id_token": "tok"},
    )
    monkeypatch.setattr(
        conn_router,
        "google_verify_id_token",
        lambda tok, **k: {"sub": "s1", "email": "ops@acme.io", "email_verified": True},
    )
    r0 = client.get("/connections/google/connect", follow_redirects=False)
    state = parse_qs(urlparse(r0.headers["location"]).query)["state"][0]
    r = client.get(
        "/connections/google/callback", params={"code": "c", "state": state}, follow_redirects=False
    )
    assert r.status_code == 303
    conn = db_session.scalar(
        select(GoogleConnection).where(GoogleConnection.account_email == "ops@acme.io")
    )
    assert conn is not None and conn.refresh_token == "rt-1" and conn.calendar_id == "primary"
