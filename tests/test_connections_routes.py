from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.routers.connections as conn_router
from app.db import get_db
from app.main import create_app
from app.models import SlackConnection
from app.services.users import bootstrap_admin
from app.settings_store import slack_settings


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


def test_connections_page_requires_login(client):
    assert client.get("/connections", follow_redirects=False).status_code in (303, 307)


def test_slack_callback_stores_connection(client, db_session, monkeypatch):
    _admin(client, db_session)
    s = slack_settings(db_session)
    s.client_id, s.client_secret, s.enabled = "cid", "csec", True
    db_session.flush()

    def fake_exchange(**kwargs):
        return {
            "access_token": "xoxb-abc",
            "token_type": "bot",
            "bot_user_id": "U1",
            "app_id": "A1",
            "scope": "chat:write",
            "team": {"id": "T123", "name": "Acme"},
            "is_enterprise_install": False,
        }

    monkeypatch.setattr(conn_router, "exchange_code", fake_exchange)
    # seed the expected state in the session by starting the install first
    r0 = client.get("/connections/slack/install", follow_redirects=False)
    assert r0.status_code in (302, 307)
    # parse the state out of the Slack authorize URL in the Location header
    state = parse_qs(urlparse(r0.headers["location"]).query)["state"][0]
    r = client.get(
        "/connections/slack/callback",
        params={"code": "c", "state": state},
        follow_redirects=False,
    )
    assert r.status_code == 303
    conn = db_session.scalar(select(SlackConnection).where(SlackConnection.team_id == "T123"))
    assert conn is not None and conn.bot_token == "xoxb-abc" and conn.team_name == "Acme"


def test_slack_callback_rejects_bad_state(client, db_session, monkeypatch):
    _admin(client, db_session)
    s = slack_settings(db_session)
    s.client_id, s.client_secret = "cid", "csec"
    db_session.flush()

    def boom(**kwargs):  # exchange must never be called on a bad state
        raise AssertionError("exchange_code must not run when state is invalid")

    monkeypatch.setattr(conn_router, "exchange_code", boom)
    # No install was performed, so there is no session state to match.
    r = client.get(
        "/connections/slack/callback",
        params={"code": "c", "state": "forged"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert db_session.scalar(select(SlackConnection)) is None  # nothing stored
