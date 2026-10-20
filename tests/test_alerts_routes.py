import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import (
    Alert,
    InboundIntegration,
    Incident,
    IncidentEvent,
    Role,
    SeverityLevel,
    Webhook,
    WorkflowRule,
)
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session, get_db_override):
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override
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


def test_inbox_renders_sidebar_when_authenticated(client, db_session):
    # Regression: the inbox route must pass current_user so base.html renders the
    # left sidebar nav (it was missing, leaving the page without navigation).
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    html = client.get("/alerts").text
    assert 'class="sidebar"' in html
    assert 'href="/systems"' in html  # a sidebar nav link (only in the authenticated shell)


def test_login_page_renders_without_sidebar_when_unauthenticated(client, db_session):
    # The current_user context processor must default to None for routes without an
    # auth dependency (request.state.current_user unset), so unauthenticated pages
    # use the plain layout — and never leak a previous request's user.
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    client.post("/logout")
    html = client.get("/login").text
    assert 'class="sidebar"' not in html


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


def test_declare_runs_incident_opened_rules(client, db_session):
    # Declaring from the inbox must have the same side-effects as POST /incidents:
    # the incident.opened automation rules run on the new incident.
    a = _alert(db_session)
    db_session.add(
        WorkflowRule(name="on-open", trigger="incident.opened", conditions=[], actions=[])
    )
    db_session.commit()
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    client.post(f"/alerts/{a.id}/declare", follow_redirects=False)
    db_session.refresh(a)
    bodies = list(
        db_session.scalars(
            select(IncidentEvent.body).where(
                IncidentEvent.incident_id == a.incident_id,
                IncidentEvent.entry_type == "automation",
            )
        )
    )
    assert "Automation 'on-open' ran" in bodies


def test_declare_fires_opened_webhook(client, db_session, webhook_posts):
    a = _alert(db_session)
    db_session.add(Webhook(name="W", url="https://hooks/x", format="slack", enabled=True))
    db_session.commit()
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    r = client.post(f"/alerts/{a.id}/declare", follow_redirects=False)
    assert r.status_code == 303
    assert any("incident opened: Boom" in (j.get("text") or "") for _, j in webhook_posts)


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
