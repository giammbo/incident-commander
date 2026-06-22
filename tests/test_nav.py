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


def test_readonly_sees_catalog_not_admin_links(client, db_session):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(db_session, email="ro@x.io", name="RO", role=Role.read_only, password="pw-123456")
    db_session.flush()
    client.post("/login", data={"email": "ro@x.io", "password": "pw-123456"})
    html = client.get("/").text
    assert 'href="/systems"' in html and 'href="/components"' in html
    assert 'href="/users"' not in html and 'href="/connections"' not in html
    assert 'href="/account/password"' in html  # personal settings entry


def test_ic_does_not_see_connections(client, db_session):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(
        db_session, email="ic@x.io", name="IC", role=Role.incident_commander, password="pw-123456"
    )
    db_session.flush()
    client.post("/login", data={"email": "ic@x.io", "password": "pw-123456"})
    html = client.get("/").text
    assert 'href="/connections"' not in html and 'href="/users"' not in html
