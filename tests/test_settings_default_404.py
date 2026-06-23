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


def _login_admin(client, db_session):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(db_session, email="a@x.io", name="A", role=Role.admin, password="pw-123456")
    db_session.flush()
    client.post("/login", data={"email": "a@x.io", "password": "pw-123456"})


@pytest.mark.parametrize(
    "path",
    [
        "/settings/severity/99999/default",
        "/settings/statuses/99999/default",
        "/settings/incident-types/99999/default",
    ],
)
def test_default_unknown_id_flashes_not_500(client, db_session, path):
    _login_admin(client, db_session)
    r = client.post(path, follow_redirects=False)
    assert r.status_code == 303  # graceful redirect, NOT 500
