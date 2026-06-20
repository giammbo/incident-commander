import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.security.passwords import verify_password
from app.services.users import bootstrap_admin


@pytest.fixture
def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def test_change_password_clears_flag(client, db_session):
    admin, pw = bootstrap_admin(db_session, "admin@localhost")
    db_session.flush()
    client.post("/login", data={"email": "admin@localhost", "password": pw}, follow_redirects=False)
    r = client.post(
        "/account/password",
        data={"new_password": "Brand-New-1", "confirm": "Brand-New-1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db_session.refresh(admin)
    assert admin.must_change_password is False
    assert verify_password("Brand-New-1", admin.password_hash)


def test_must_change_password_redirects_protected_routes(client, db_session):
    # Fresh bootstrap admin has must_change_password=True.
    _, pw = bootstrap_admin(db_session, "admin@localhost")
    db_session.flush()
    client.post("/login", data={"email": "admin@localhost", "password": pw}, follow_redirects=False)

    # Any other authenticated route bounces to the password page (303), no loop.
    r = client.get("/users", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/account/password"

    # The password page itself does NOT redirect (no loop).
    assert client.get("/account/password", follow_redirects=False).status_code == 200

    # After changing the password the protected route is reachable normally.
    client.post(
        "/account/password",
        data={"new_password": "Brand-New-1", "confirm": "Brand-New-1"},
        follow_redirects=False,
    )
    assert client.get("/users", follow_redirects=False).status_code == 200


def test_password_submit_rejects_mismatch(client, db_session):
    _, pw = bootstrap_admin(db_session, "admin@localhost")
    db_session.flush()
    client.post("/login", data={"email": "admin@localhost", "password": pw}, follow_redirects=False)
    r = client.post(
        "/account/password",
        data={"new_password": "long-enough-1", "confirm": "different-2"},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_password_submit_rejects_short(client, db_session):
    _, pw = bootstrap_admin(db_session, "admin@localhost")
    db_session.flush()
    client.post("/login", data={"email": "admin@localhost", "password": pw}, follow_redirects=False)
    r = client.post(
        "/account/password",
        data={"new_password": "short", "confirm": "short"},
        follow_redirects=False,
    )
    assert r.status_code == 400
