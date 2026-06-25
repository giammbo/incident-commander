import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.services.incident_actions as actions
from app.db import get_db
from app.main import create_app
from app.models import Incident, Role, SeverityLevel
from app.services.users import bootstrap_admin, create_user
from app.settings_store import google_settings


@pytest.fixture
def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _sev(db_session):
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=False)
    db_session.add(lvl)
    db_session.flush()
    return lvl.id


def test_create_incident_makes_meet(client, db_session, monkeypatch):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(
        db_session, email="ic@x.io", name="IC", role=Role.incident_commander, password="pw-123456"
    )
    g = google_settings(db_session)
    g.enabled = True
    g.service_account_json = '{"type": "service_account"}'
    g.impersonate_email = "bot@example.com"
    db_session.flush()
    sev_id = _sev(db_session)
    client.post("/login", data={"email": "ic@x.io", "password": "pw-123456"})

    monkeypatch.setattr(
        actions.google,
        "create_meet_space",
        lambda **k: ("https://meet.google.com/abc-defg-hij", "spaces/abc-defg-hij"),
    )
    r = client.post(
        "/incidents",
        data={
            "title": "DB down",
            "severity_level_id": str(sev_id),
            "video": "meet",
        },
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    inc = db_session.scalar(select(Incident).order_by(Incident.id.desc()))
    assert inc.meet_url == "https://meet.google.com/abc-defg-hij"
    assert inc.meet_space_name == "spaces/abc-defg-hij"
    assert inc.creation_state["meet"] == "ok"
    assert inc.creation_state["smart_notes"] == "ok"


def test_meet_failure_is_graceful(client, db_session, monkeypatch):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(
        db_session, email="ic@x.io", name="IC", role=Role.incident_commander, password="pw-123456"
    )
    g = google_settings(db_session)
    g.enabled = True
    g.service_account_json = '{"type": "service_account"}'
    g.impersonate_email = "bot@example.com"
    db_session.flush()
    sev_id = _sev(db_session)
    client.post("/login", data={"email": "ic@x.io", "password": "pw-123456"})

    def boom(**k):
        raise RuntimeError("google down")

    monkeypatch.setattr(actions.google, "create_meet_space", boom)
    r = client.post(
        "/incidents",
        data={"title": "X", "severity_level_id": str(sev_id), "video": "meet"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    inc = db_session.scalar(select(Incident).order_by(Incident.id.desc()))
    assert inc.meet_url is None and inc.creation_state["meet"] == "failed"
