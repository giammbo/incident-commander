import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import Role, Team
from app.services import teams
from app.services.catalog import create_system
from app.services.users import bootstrap_admin, create_user


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


def test_admin_creates_team(client, db_session):
    _admin(client, db_session)
    r = client.post(
        "/settings/teams",
        data={"name": "SRE", "description": "x"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    t = db_session.scalar(select(Team).where(Team.name == "SRE"))
    assert t is not None and t.description == "x"


def test_admin_deletes_unused_team(client, db_session):
    _admin(client, db_session)
    t = teams.create_team(db_session, name="Ops")
    db_session.commit()
    r = client.post(f"/settings/teams/{t.id}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert db_session.get(Team, t.id) is None


def test_delete_in_use_flashes(client, db_session):
    _admin(client, db_session)
    t = teams.create_team(db_session, name="Platform")
    db_session.flush()
    create_system(db_session, name="Core", owner_team_id=t.id)
    db_session.commit()
    r = client.post(f"/settings/teams/{t.id}/delete", follow_redirects=False)
    assert r.status_code == 303
    # team still exists
    assert db_session.get(Team, t.id) is not None


def test_non_admin_forbidden(client, db_session):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(
        db_session, email="ic@x.io", name="IC", role=Role.incident_commander, password="pw-123456"
    )
    db_session.flush()
    client.post("/login", data={"email": "ic@x.io", "password": "pw-123456"})
    r = client.post("/settings/teams", data={"name": "SRE"})
    assert r.status_code == 403
