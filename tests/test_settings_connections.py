import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.models import SlackConnection
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
    g.service_account_json = '{"type":"service_account","client_email":"bot@proj.iam"}'
    g.impersonate_email = "ops@acme.io"
    g.enabled = True
    db_session.add(
        SlackConnection(team_id="T1", team_name="Acme", bot_token="xoxb-1", created_by=1)
    )
    db_session.flush()
    html = client.get("/settings").text
    assert "/connections/slack/install" in html and "Acme" in html
    # SA card: impersonate_email is rendered in the input value
    assert "ops@acme.io" in html
    # textarea placeholder shows "configured" when service_account_json is set
    assert "configured" in html


def test_settings_hides_connect_when_not_configured(client, db_session):
    _admin(client, db_session)
    html = client.get("/settings").text
    assert "/connections/slack/install" not in html
    # SA card is always shown; when not configured the textarea placeholder says "paste"
    assert "paste the service account JSON key" in html
    # The old OAuth connect link must not appear
    assert "/connections/google/connect" not in html


def test_settings_google_sa_card_present(client, db_session):
    _admin(client, db_session)
    html = client.get("/settings").text
    # SA card fields are present
    assert 'name="service_account_json"' in html
    assert 'name="impersonate_email"' in html
    # The old OAuth fields are gone from the Google card specifically
    # (client_id still legitimately appears in the Slack + SSO cards).
    google_card = html.split(">Google<", 1)[1].split('<div class="card"', 1)[0]
    assert 'name="client_id"' not in google_card
    assert 'name="client_secret"' not in google_card
    assert "/connections/google/connect" not in html


def test_connections_redirects_to_settings_for_admin(client, db_session):
    _admin(client, db_session)
    r = client.get("/connections", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings"
