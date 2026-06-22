import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import StatusCategory, StatusLevel
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


def test_admin_creates_status(client, db_session):
    _admin(client, db_session)
    client.post(
        "/settings/statuses",
        data={"label": "Investigating", "category": "active", "rank": "2", "is_default": "false"},
    )
    lvls = list(db_session.scalars(select(StatusLevel).where(StatusLevel.label == "Investigating")))
    assert len(lvls) == 1 and lvls[0].category == StatusCategory.active


def test_admin_sets_default_on_existing_status(client, db_session):
    _admin(client, db_session)
    a = StatusLevel(label="Triage", category=StatusCategory.triage, rank=1, is_default=True)
    b = StatusLevel(label="Investigating", category=StatusCategory.active, rank=2, is_default=False)
    db_session.add_all([a, b])
    db_session.flush()
    r = client.post(f"/settings/statuses/{b.id}/default", follow_redirects=False)
    assert r.status_code == 303
    db_session.refresh(a)
    db_session.refresh(b)
    assert b.is_default is True and a.is_default is False


def test_admin_deletes_unused_status(client, db_session):
    _admin(client, db_session)
    s = StatusLevel(label="Obsolete", category=StatusCategory.triage, rank=99, is_default=False)
    db_session.add(s)
    db_session.flush()
    client.post(f"/settings/statuses/{s.id}/delete")
    remaining = list(db_session.scalars(select(StatusLevel).where(StatusLevel.label == "Obsolete")))
    assert remaining == []


def test_non_admin_forbidden_statuses(client, db_session):
    from app.models import Role
    from app.services.users import create_user

    bootstrap_admin(db_session, "admin@localhost")
    create_user(
        db_session, email="ic@x.io", name="IC", role=Role.incident_commander, password="pw-123456"
    )
    db_session.flush()
    client.post("/login", data={"email": "ic@x.io", "password": "pw-123456"})
    assert (
        client.post(
            "/settings/statuses",
            data={"label": "Triage", "category": "triage", "rank": "1"},
        ).status_code
        == 403
    )
