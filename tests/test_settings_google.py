import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.services.users import bootstrap_admin
from app.settings_store import google_settings, sso_settings
from tests.settings_fixtures import service_account_json


@pytest.fixture
def client(db_session, get_db_override):
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override
    return TestClient(app)


def _admin(client, db_session):
    _, pw = bootstrap_admin(db_session, "admin@localhost")
    db_session.flush()
    client.post("/login", data={"email": "admin@localhost", "password": pw})
    client.post(
        "/account/password",
        data={"new_password": "Admin-123", "confirm": "Admin-123", "name": "Admin"},
    )


def test_save_google_and_sso(client, db_session):
    _admin(client, db_session)
    client.post(
        "/settings/google",
        data={
            "service_account_json": service_account_json(),
            "impersonate_email": "ops@example.com",
            "enabled": "true",
        },
    )
    client.post(
        "/settings/sso",
        data={
            "sso_enabled": "true",
            "issuer": "https://issuer.example.com",
            "client_id": "synthetic-client",
            "client_secret": "synthetic-secret",
            "allowed_domains": "acme.io,example.com",
        },
    )
    g = google_settings(db_session)
    s = sso_settings(db_session)
    assert g.impersonate_email == "ops@example.com"
    assert g.enabled is True
    assert '"service_account"' in g.service_account_json
    assert (
        s.sso_enabled is True
        and s.allow_local_login is False
        and s.allowed_domains == "acme.io,example.com"
    )
    # editing google without JSON keeps the stored JSON
    client.post(
        "/settings/google",
        data={
            "service_account_json": "",
            "impersonate_email": "ops2@example.com",
            "enabled": "true",
        },
    )
    g2 = google_settings(db_session)
    assert g2.impersonate_email == "ops2@example.com"
    assert '"service_account"' in g2.service_account_json
