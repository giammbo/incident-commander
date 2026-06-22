import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.models import GoogleConnection, SlackConnection
from app.services.users import bootstrap_admin
from app.settings_store import google_settings, slack_settings


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


def test_settings_shows_connect_buttons_when_configured(client, db_session):
    _admin(client, db_session)
    s = slack_settings(db_session)
    s.client_id, s.client_secret, s.enabled = "cid", "csec", True
    g = google_settings(db_session)
    g.client_id, g.client_secret, g.enabled = "gid", "gsec", True
    db_session.add(
        SlackConnection(team_id="T1", team_name="Acme", bot_token="xoxb-1", created_by=1)
    )
    db_session.add(GoogleConnection(account_email="ops@acme.io", refresh_token="r", created_by=1))
    db_session.flush()
    html = client.get("/settings").text
    assert "/connections/slack/install" in html and "Acme" in html
    assert "/connections/google/connect" in html and "ops@acme.io" in html


def test_settings_hides_connect_when_not_configured(client, db_session):
    _admin(client, db_session)
    html = client.get("/settings").text
    assert "/connections/slack/install" not in html
    assert "/connections/google/connect" not in html


def test_connections_redirects_to_settings_for_admin(client, db_session):
    _admin(client, db_session)
    r = client.get("/connections", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings"
