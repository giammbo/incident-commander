import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import Role, System
from app.services import teams
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _login(client, db_session, email, role):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(db_session, email=email, name="U", role=role, password="pw-123456")
    db_session.flush()
    client.post("/login", data={"email": email, "password": "pw-123456"})


def test_ic_creates_system_with_owner(client, db_session):
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    t = teams.create_team(db_session, name="Payments")
    db_session.commit()
    r = client.post(
        "/systems",
        data={"name": "Checkout", "owner_team_id": str(t.id)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    s = db_session.scalar(select(System).where(System.name == "Checkout"))
    assert s.owner_team_id == t.id
