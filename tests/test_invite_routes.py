import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.models import Role
from app.services.invites import create_invite
from app.services.users import authenticate_local, bootstrap_admin, create_user


@pytest.fixture
def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def test_accept_page_renders(client, db_session):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(db_session, email="dev@x.io", name="Dev", role=Role.read_only)
    db_session.flush()
    _, token = create_invite(db_session, email="dev@x.io", created_by=1)
    db_session.flush()
    r = client.get(f"/invite/accept?token={token}")
    assert r.status_code == 200
    assert "password" in r.text.lower()


def test_accept_sets_password_and_activates(client, db_session):
    bootstrap_admin(db_session, "admin@localhost")
    u = create_user(db_session, email="dev@x.io", name="Dev", role=Role.read_only)
    u.is_active = False
    db_session.flush()
    _, token = create_invite(db_session, email="dev@x.io", created_by=1)
    db_session.flush()
    r = client.post(
        "/invite/accept",
        data={"token": token, "new_password": "Brand-New-1", "confirm": "Brand-New-1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    assert authenticate_local(db_session, "dev@x.io", "Brand-New-1") is not None


def test_accept_bad_token_shows_error(client, db_session):
    r = client.post(
        "/invite/accept",
        data={"token": "nope", "new_password": "Brand-New-1", "confirm": "Brand-New-1"},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_invited_user_cannot_login_before_accept(client, db_session):
    # An invited user is created inactive with no password — they must not be able to log in
    # until they accept the invitation and set a password.
    bootstrap_admin(db_session, "admin@localhost")
    u = create_user(db_session, email="dev@x.io", name="Dev", role=Role.read_only)
    u.is_active = False
    db_session.flush()
    create_invite(db_session, email="dev@x.io", created_by=1)
    db_session.flush()
    assert authenticate_local(db_session, "dev@x.io", "anything") is None
