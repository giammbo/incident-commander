import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.models import Role, SeverityLevel
from app.services import statuses
from app.services.incidents import create_incident
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _login_ic(client, db_session):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(
        db_session, email="ic@x.io", name="IC", role=Role.incident_commander, password="pw-123456"
    )
    db_session.flush()
    client.post("/login", data={"email": "ic@x.io", "password": "pw-123456"})


def _incident(db_session):
    statuses.seed_status_levels(db_session)
    db_session.flush()
    sev = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db_session.add(sev)
    db_session.flush()
    inc = create_incident(
        db_session, title="X", severity_level_id=sev.id, is_private=False, created_by=1
    )
    db_session.flush()
    return inc


def test_view_invokes_pull(client, db_session, monkeypatch):
    _login_ic(client, db_session)
    inc = _incident(db_session)
    db_session.commit()
    calls = []
    monkeypatch.setattr(
        "app.routers.postmortems.pm_svc.maybe_pull_gemini_notes",
        lambda db, i: calls.append(i.id) or False,
    )
    r = client.get(f"/incidents/{inc.id}/postmortem")
    assert r.status_code == 200 and calls == [inc.id]


def test_close_invokes_pull(client, db_session, monkeypatch):
    _login_ic(client, db_session)
    inc = _incident(db_session)
    db_session.commit()
    calls = []
    monkeypatch.setattr(
        "app.routers.incidents.pm_svc.maybe_pull_gemini_notes",
        lambda db, i: calls.append(i.id) or False,
    )
    r = client.post(
        f"/incidents/{inc.id}/close",
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code in (200, 303) and calls == [inc.id]


def test_view_pull_error_does_not_break_page(client, db_session, monkeypatch):
    _login_ic(client, db_session)
    inc = _incident(db_session)
    db_session.commit()

    def boom(db, i):
        raise RuntimeError("x")

    monkeypatch.setattr("app.routers.postmortems.pm_svc.maybe_pull_gemini_notes", boom)
    # the hook is wrapped, so the page still renders
    assert client.get(f"/incidents/{inc.id}/postmortem").status_code == 200
