import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.services.users import bootstrap_admin


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


def test_google_connect_route_gone(client, db_session):
    _admin(client, db_session)
    assert client.get("/connections/google/connect", follow_redirects=False).status_code == 404


def test_google_callback_route_gone(client, db_session):
    _admin(client, db_session)
    assert client.get("/connections/google/callback", follow_redirects=False).status_code == 404
