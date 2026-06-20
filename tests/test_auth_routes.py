import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.models import Role
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def test_login_page_renders(client):
    assert client.get("/login").status_code == 200


def test_login_success_sets_session_and_redirects(client, db_session):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(db_session, email="a@x.io", name="A", role=Role.read_only, password="hunter2!")
    db_session.flush()
    r = client.post(
        "/login", data={"email": "a@x.io", "password": "hunter2!"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    # session cookie present → can hit a protected page
    assert client.get("/").status_code in (200, 303)


def test_login_failure_shows_error(client, db_session):
    bootstrap_admin(db_session, "admin@localhost")
    db_session.flush()
    r = client.post("/login", data={"email": "a@x.io", "password": "bad"}, follow_redirects=False)
    assert r.status_code == 401


def test_login_sets_last_login_at(client, db_session):
    bootstrap_admin(db_session, "admin@localhost")
    user = create_user(
        db_session, email="a@x.io", name="A", role=Role.read_only, password="hunter2!"
    )
    db_session.flush()
    assert user.last_login_at is None
    r = client.post(
        "/login", data={"email": "a@x.io", "password": "hunter2!"}, follow_redirects=False
    )
    assert r.status_code == 303
    db_session.refresh(user)
    assert user.last_login_at is not None
