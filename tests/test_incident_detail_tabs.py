import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.models import Alert, InboundIntegration, Role, SeverityLevel
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
        db_session, title="Tabby", severity_level_id=sev.id, is_private=False, created_by=1
    )
    db_session.flush()
    return inc


def test_detail_renders_tab_scaffold(client, db_session):
    _login_ic(client, db_session)
    inc = _incident(db_session)
    db_session.commit()
    body = client.get(f"/incidents/{inc.id}").text
    # tab radios + labels
    assert (
        'id="tab-overview"' in body and 'id="tab-timeline"' in body and 'id="tab-followups"' in body
    )
    assert ">Overview<" in body and ">Timeline<" in body and "Follow-ups" in body
    # Roles is always its own tab (panel + label), not buried in Overview
    assert 'id="tab-roles"' in body and 'id="panel-roles"' in body and ">Roles<" in body
    # content relocated into the page (timeline add-note form + respond box still present)
    assert f'action="/incidents/{inc.id}/notes"' in body
    assert f'action="/incidents/{inc.id}/updates"' in body  # Respond box always present
    # Alerts tab absent when there are no related alerts
    assert 'id="tab-alerts"' not in body


def test_alerts_tab_appears_with_related_alert(client, db_session):
    _login_ic(client, db_session)
    inc = _incident(db_session)
    integ = InboundIntegration(name="i", kind="generic", token="tk")
    db_session.add(integ)
    db_session.flush()
    db_session.add(
        Alert(
            integration_id=integ.id,
            source="generic",
            dedup_key="dk",
            title="Boom",
            status="firing",
            incident_id=inc.id,
        )
    )
    db_session.commit()
    body = client.get(f"/incidents/{inc.id}").text
    assert 'id="tab-alerts"' in body and ">Alerts<" in body
