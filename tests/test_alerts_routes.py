import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import Alert, InboundIntegration, Incident, Role, SeverityLevel
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


def _alert(db_session):
    integ = InboundIntegration(name="i", kind="generic", token="tk")
    db_session.add(integ)
    db_session.flush()
    a = Alert(
        integration_id=integ.id, source="generic", dedup_key="dk", title="Boom", status="firing"
    )
    db_session.add(a)
    db_session.add(SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True))
    db_session.commit()
    return a


def test_inbox_requires_login(client, db_session):
    assert client.get("/alerts", follow_redirects=False).status_code in (302, 303, 307)


def test_readonly_cannot_declare(client, db_session):
    a = _alert(db_session)
    _login(client, db_session, "ro@x.io", Role.read_only)
    assert client.post(f"/alerts/{a.id}/declare", follow_redirects=False).status_code == 403


def test_declare_creates_and_links_incident(client, db_session):
    a = _alert(db_session)
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    r = client.post(f"/alerts/{a.id}/declare", follow_redirects=False)
    assert r.status_code == 303
    db_session.refresh(a)
    inc = db_session.scalar(select(Incident).where(Incident.id == a.incident_id))
    assert inc is not None and inc.title == "Boom"


def test_attach_to_existing(client, db_session):
    a = _alert(db_session)
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    client.post(f"/alerts/{a.id}/declare", follow_redirects=False)  # creates incident 1
    db_session.refresh(a)
    inc_id = a.incident_id
    a.incident_id = None
    db_session.commit()
    r = client.post(
        f"/alerts/{a.id}/attach", data={"incident_id": str(inc_id)}, follow_redirects=False
    )
    assert r.status_code == 303
    db_session.refresh(a)
    assert a.incident_id == inc_id


def test_resolve(client, db_session):
    a = _alert(db_session)
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    client.post(f"/alerts/{a.id}/resolve", follow_redirects=False)
    db_session.refresh(a)
    assert a.status == "resolved"
