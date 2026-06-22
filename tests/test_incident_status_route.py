import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import Role, SeverityLevel, StatusCategory, StatusLevel
from app.services import statuses
from app.services.incidents import list_incidents
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


def _sev(db_session):
    statuses.seed_status_levels(db_session)
    db_session.flush()
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=False)
    db_session.add(lvl)
    db_session.flush()
    return lvl.id


def test_readonly_cannot_change_status(client, db_session):
    _login(client, db_session, "ro@x.io", Role.read_only)
    r = client.post(
        "/incidents/1/status",
        data={"status_id": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_ic_change_to_active_fires_updated(client, db_session, monkeypatch):
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    sev_id = _sev(db_session)
    client.post(
        "/incidents",
        data={"title": "Active test incident", "severity_level_id": str(sev_id)},
        headers={"HX-Request": "true"},
    )
    inc = list_incidents(db_session)[0]

    events = []
    monkeypatch.setattr(
        "app.routers.incidents.webhooks.notify", lambda *a, **k: events.append(a[2])
    )

    investigating = db_session.scalar(
        select(StatusLevel)
        .where(StatusLevel.category == StatusCategory.active)
        .order_by(StatusLevel.rank)
    )

    r = client.post(
        f"/incidents/{inc.id}/status",
        data={"status_id": str(investigating.id)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db_session.refresh(inc)
    assert not inc.is_closed
    assert events == ["updated"]


def test_ic_close_via_status_sets_closed_and_fires_closed(client, db_session, monkeypatch):
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    sev_id = _sev(db_session)
    client.post(
        "/incidents",
        data={"title": "Close test incident", "severity_level_id": str(sev_id)},
        headers={"HX-Request": "true"},
    )
    inc = list_incidents(db_session)[0]

    events = []
    monkeypatch.setattr(
        "app.routers.incidents.webhooks.notify", lambda *a, **k: events.append(a[2])
    )

    closed_status = db_session.scalar(
        select(StatusLevel)
        .where(StatusLevel.category == StatusCategory.closed)
        .order_by(StatusLevel.rank)
    )

    r = client.post(
        f"/incidents/{inc.id}/status",
        data={"status_id": str(closed_status.id)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db_session.refresh(inc)
    assert inc.is_closed
    assert inc.closed_at is not None
    assert events == ["closed"]


def test_unknown_status_flashes_not_500(client, db_session):
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    sev_id = _sev(db_session)
    client.post(
        "/incidents",
        data={"title": "Unknown status test", "severity_level_id": str(sev_id)},
        headers={"HX-Request": "true"},
    )
    inc = list_incidents(db_session)[0]
    original_status_id = inc.status_id

    r = client.post(
        f"/incidents/{inc.id}/status",
        data={"status_id": "999999"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db_session.refresh(inc)
    assert inc.status_id == original_status_id
    assert not inc.is_closed
