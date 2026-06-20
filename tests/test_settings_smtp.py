import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.services.users import bootstrap_admin
from app.settings_store import smtp_settings


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


def test_save_smtp_and_keep_password(client, db_session):
    _admin(client, db_session)
    client.post(
        "/settings/smtp",
        data={
            "host": "smtp.x",
            "port": "587",
            "username": "u",
            "password": "secret",
            "from_address": "bot@x.io",
            "use_tls": "true",
        },
        follow_redirects=False,
    )
    s = smtp_settings(db_session)
    assert s.host == "smtp.x" and s.password == "secret"
    # editing without a password keeps the stored one
    client.post(
        "/settings/smtp",
        data={
            "host": "smtp.y",
            "port": "587",
            "username": "u",
            "password": "",
            "from_address": "bot@x.io",
            "use_tls": "true",
        },
        follow_redirects=False,
    )
    s2 = smtp_settings(db_session)
    assert s2.host == "smtp.y" and s2.password == "secret"


def test_non_admin_cannot_save_smtp(client, db_session):
    from app.models import Role
    from app.services.users import create_user

    bootstrap_admin(db_session, "admin@localhost")
    create_user(
        db_session, email="ic@x.io", name="IC", role=Role.incident_commander, password="pw-123456"
    )
    db_session.flush()
    client.post("/login", data={"email": "ic@x.io", "password": "pw-123456"})
    assert (
        client.post("/settings/smtp", data={"host": "x", "from_address": "a@x.io"}).status_code
        == 403
    )
