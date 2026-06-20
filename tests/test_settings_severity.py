import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import SeverityLevel
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


def test_admin_creates_severity_level(client, db_session):
    _admin(client, db_session)
    client.post(
        "/settings/severity",
        data={"label": "P1", "color": "#ff0000", "rank": "1", "is_default": "true"},
    )
    lvls = list(db_session.scalars(select(SeverityLevel).where(SeverityLevel.label == "P1")))
    assert len(lvls) == 1 and lvls[0].color == "#ff0000" and lvls[0].is_default is True


def test_admin_sets_default_on_existing_level(client, db_session):
    _admin(client, db_session)
    a = SeverityLevel(label="P1", color="#FF0000", rank=1, is_default=True)
    b = SeverityLevel(label="P2", color="#FFAA00", rank=2, is_default=False)
    db_session.add_all([a, b])
    db_session.flush()
    r = client.post(f"/settings/severity/{b.id}/default", follow_redirects=False)
    assert r.status_code == 303
    db_session.refresh(a)
    db_session.refresh(b)
    assert b.is_default is True and a.is_default is False


def test_non_admin_forbidden(client, db_session):
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
            "/settings/severity", data={"label": "P1", "color": "#f00", "rank": "1"}
        ).status_code
        == 403
    )
