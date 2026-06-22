import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import FollowUp, Role, SeverityLevel
from app.services import statuses
from app.services.incidents import create_incident
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


def _incident(db_session):
    statuses.seed_status_levels(db_session)
    db_session.flush()
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db_session.add(lvl)
    db_session.flush()
    inc = create_incident(
        db_session, title="X", severity_level_id=lvl.id, is_private=False, created_by=1
    )
    db_session.flush()
    return inc


def _items(db_session, inc):
    return list(db_session.scalars(select(FollowUp).where(FollowUp.incident_id == inc.id)))


def test_readonly_cannot_create(client, db_session):
    _login(client, db_session, "ro@x.io", Role.read_only)
    assert (
        client.post(
            "/incidents/1/followups", data={"title": "x"}, follow_redirects=False
        ).status_code
        == 403
    )


def test_ic_create_complete_reopen_delete(client, db_session):
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    inc = _incident(db_session)
    assert (
        client.post(
            f"/incidents/{inc.id}/followups",
            data={"title": "Patch", "due_on": "2026-07-01"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    fu = _items(db_session, inc)[0]
    assert fu.title == "Patch" and fu.status == "open"
    client.post(f"/followups/{fu.id}/status", data={"status": "completed"}, follow_redirects=False)
    db_session.refresh(fu)
    assert fu.status == "completed" and fu.resolved_at is not None
    client.post(f"/followups/{fu.id}/status", data={"status": "open"}, follow_redirects=False)
    db_session.refresh(fu)
    assert fu.status == "open" and fu.resolved_at is None
    assert client.post(f"/followups/{fu.id}/delete", follow_redirects=False).status_code == 303
    assert not _items(db_session, inc)


def test_empty_title_flashes_not_500(client, db_session):
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    inc = _incident(db_session)
    r = client.post(f"/incidents/{inc.id}/followups", data={"title": "  "}, follow_redirects=False)
    assert r.status_code == 303
    assert not _items(db_session, inc)


def test_invalid_status_flashes(client, db_session):
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    inc = _incident(db_session)
    client.post(f"/incidents/{inc.id}/followups", data={"title": "T"})
    db_session.commit()
    fu = _items(db_session, inc)[0]
    r = client.post(f"/followups/{fu.id}/status", data={"status": "bogus"}, follow_redirects=False)
    assert r.status_code == 303
    db_session.refresh(fu)
    assert fu.status == "open"
