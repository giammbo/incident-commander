import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import Role, SeverityLevel
from app.services import postmortems as pm_svc
from app.services import statuses
from app.services.incidents import create_incident
from app.services.users import bootstrap_admin, create_user


@pytest.fixture(autouse=True)
def _seed_user(db_session):
    db_session.execute(
        sa.text(
            "INSERT INTO users (id, email, name, is_active, must_change_password, is_protected_admin) "
            "VALUES (1, 'seed@test.local', 'Seed', true, false, false) ON CONFLICT (id) DO NOTHING"
        )
    )
    db_session.execute(sa.text("SELECT setval('users_id_seq', (SELECT MAX(id) FROM users))"))
    db_session.flush()


@pytest.fixture
def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _login(client, db_session):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(
        db_session, email="ic@x.io", name="IC", role=Role.incident_commander, password="pw-123456"
    )
    db_session.flush()
    client.post("/login", data={"email": "ic@x.io", "password": "pw-123456"})


def _incident(db, title):
    statuses.seed_status_levels(db)
    db.flush()
    sev = db.scalar(select(SeverityLevel).order_by(SeverityLevel.rank))
    if sev is None:
        sev = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
        db.add(sev)
        db.flush()
    inc = create_incident(db, title=title, severity_level_id=sev.id, is_private=False, created_by=1)
    db.flush()
    return inc


def test_requires_login(client, db_session):
    assert client.get("/postmortems", follow_redirects=False).status_code in (302, 303, 307)


def test_service_lists_only_with_postmortem(db_session):
    a = _incident(db_session, "A")
    pm_svc.generate(db_session, a, by_user=1)
    _incident(db_session, "B")  # no postmortem
    db_session.flush()
    titles = [i.title for i in pm_svc.list_incidents_with_postmortem(db_session)]
    assert titles == ["A"]


def test_page_lists_only_incidents_with_postmortem(client, db_session):
    with_pm = _incident(db_session, "Has PM")
    pm_svc.generate(db_session, with_pm, by_user=1)
    _incident(db_session, "No PM here")  # no postmortem
    db_session.commit()
    _login(client, db_session)
    body = client.get("/postmortems").text
    assert "Has PM" in body and "No PM here" not in body


def test_service_lists_closed_without_postmortem(db_session):
    from app.services.incidents import close_incident

    closed_no_pm = _incident(db_session, "Closed no PM")
    close_incident(db_session, closed_no_pm, closed_by=1)
    closed_with_pm = _incident(db_session, "Closed with PM")
    close_incident(db_session, closed_with_pm, closed_by=1)
    pm_svc.generate(db_session, closed_with_pm, by_user=1)
    _incident(db_session, "Open no PM")  # open → not a candidate
    db_session.flush()
    titles = [i.title for i in pm_svc.list_closed_incidents_without_postmortem(db_session)]
    assert titles == ["Closed no PM"]  # only the closed incident lacking a postmortem


def test_page_shows_generate_button_for_closed_without_pm(client, db_session):
    from app.services.incidents import close_incident

    inc = _incident(db_session, "Needs a PM")
    close_incident(db_session, inc, closed_by=1)
    db_session.commit()
    _login(client, db_session)
    body = client.get("/postmortems").text
    assert "Needs a PM" in body
    assert f"/incidents/{inc.id}/postmortem/generate" in body  # the Generate form/button
