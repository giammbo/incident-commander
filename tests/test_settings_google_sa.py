import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.services.users import bootstrap_admin
from app.settings_store import google_settings


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


def test_settings_google_saves_sa(client, db_session):
    _admin(client, db_session)
    r = client.post(
        "/settings/google",
        data={
            "service_account_json": '{"type":"service_account","client_email":"x@y.iam"}',
            "impersonate_email": "bot@example.com",
            "enabled": "true",
        },
        follow_redirects=False,
    )
    assert r.status_code in (303, 302)
    g = google_settings(db_session)
    assert g.impersonate_email == "bot@example.com"
    assert g.enabled is True
    assert '"service_account"' in g.service_account_json


def test_settings_google_blank_json_keeps_current(client, db_session):
    _admin(client, db_session)
    client.post(
        "/settings/google",
        data={
            "service_account_json": '{"type":"service_account"}',
            "impersonate_email": "bot@example.com",
            "enabled": "true",
        },
    )
    client.post(
        "/settings/google",
        data={
            "service_account_json": "",
            "impersonate_email": "bot2@example.com",
            "enabled": "true",
        },
    )
    g = google_settings(db_session)
    assert g.impersonate_email == "bot2@example.com"
    assert '"service_account"' in g.service_account_json  # not wiped by blank


def test_settings_google_sa_write_only(client, db_session):
    """The service_account_json must never be rendered back in the page HTML."""
    _admin(client, db_session)
    client.post(
        "/settings/google",
        data={
            "service_account_json": '{"type":"service_account","client_email":"secret@proj.iam"}',
            "impersonate_email": "admin@example.com",
            "enabled": "true",
        },
    )
    html = client.get("/settings").text
    # The stored JSON must not appear verbatim in the rendered page
    assert "secret@proj.iam" not in html
    # But impersonate_email IS rendered (it's not a secret)
    assert "admin@example.com" in html
    # Placeholder confirms it's configured
    assert "configured" in html
