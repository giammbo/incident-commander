import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import IncidentType, SeverityLevel
from app.services.users import bootstrap_admin


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


def test_admin_creates_type(client, db_session):
    _admin(client, db_session)
    client.post(
        "/settings/incident-types",
        data={"label": "Latency", "rank": "5"},
    )
    types = list(db_session.scalars(select(IncidentType).where(IncidentType.label == "Latency")))
    assert len(types) == 1 and types[0].rank == 5


def test_admin_sets_default(client, db_session):
    _admin(client, db_session)
    a = IncidentType(label="Outage", rank=1, is_default=True)
    b = IncidentType(label="Degraded", rank=2, is_default=False)
    db_session.add_all([a, b])
    db_session.flush()
    r = client.post(f"/settings/incident-types/{b.id}/default", follow_redirects=False)
    assert r.status_code == 303
    db_session.refresh(a)
    db_session.refresh(b)
    assert b.is_default is True and a.is_default is False


def test_admin_deletes_unused_type(client, db_session):
    _admin(client, db_session)
    t = IncidentType(label="Temporary", rank=99, is_default=False)
    db_session.add(t)
    db_session.flush()
    r = client.post(f"/settings/incident-types/{t.id}/delete", follow_redirects=False)
    assert r.status_code == 303
    remaining = list(
        db_session.scalars(select(IncidentType).where(IncidentType.label == "Temporary"))
    )
    assert remaining == []


def test_delete_in_use_flashes(client, db_session):
    from app.services import incident_types, statuses
    from app.services.incidents import create_incident

    _admin(client, db_session)
    statuses.seed_status_levels(db_session)
    db_session.flush()

    lvl = SeverityLevel(label="High", color="#ff0000", rank=1, is_default=True)
    db_session.add(lvl)
    db_session.flush()

    t = incident_types.create_incident_type(db_session, label="Outage", rank=1)
    db_session.flush()

    create_incident(
        db_session,
        title="X",
        severity_level_id=lvl.id,
        is_private=False,
        created_by=1,
        incident_type_id=t.id,
    )
    db_session.commit()

    r = client.post(f"/settings/incident-types/{t.id}/delete", follow_redirects=False)
    assert r.status_code == 303

    still_there = db_session.get(IncidentType, t.id)
    assert still_there is not None


def test_non_admin_forbidden(client, db_session):
    from app.models import Role
    from app.services.users import create_user

    bootstrap_admin(db_session, "admin@localhost")
    create_user(
        db_session,
        email="ic@x.io",
        name="IC",
        role=Role.incident_commander,
        password="pw-123456",
    )
    db_session.flush()
    client.post("/login", data={"email": "ic@x.io", "password": "pw-123456"})
    assert (
        client.post(
            "/settings/incident-types",
            data={"label": "Latency", "rank": "1"},
        ).status_code
        == 403
    )
