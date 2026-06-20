import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.services.users import bootstrap_admin
from app.settings_store import google_settings, sso_settings


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


def test_save_google_and_sso(client, db_session):
    _admin(client, db_session)
    client.post(
        "/settings/google", data={"client_id": "gid", "client_secret": "gsec", "enabled": "true"}
    )
    client.post(
        "/settings/sso", data={"sso_enabled": "true", "allowed_domains": "acme.io,example.com"}
    )
    g = google_settings(db_session)
    s = sso_settings(db_session)
    assert g.client_id == "gid" and g.client_secret == "gsec" and g.enabled is True
    assert (
        s.sso_enabled is True
        and s.allow_local_login is False
        and s.allowed_domains == "acme.io,example.com"
    )
    # editing google without secret keeps it
    client.post(
        "/settings/google", data={"client_id": "gid2", "client_secret": "", "enabled": "true"}
    )
    assert google_settings(db_session).client_secret == "gsec"
