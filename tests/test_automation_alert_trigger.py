import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import Alert, InboundIntegration, Incident, SeverityLevel, WorkflowRule
from app.services import statuses


@pytest.fixture
def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _seed(db_session):
    statuses.seed_status_levels(db_session)
    db_session.add(SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True))
    db_session.add(
        InboundIntegration(
            name="i",
            kind="generic",
            token="auto-tok",
            settings={
                "dedup_key": "id",
                "title": "name",
                "severity": "sev",
                "status": "state",
                "resolved_value": "ok",
            },
        )
    )
    db_session.add(
        WorkflowRule(
            name="critical->incident",
            trigger="alert.received",
            conditions=[{"field": "severity_raw", "op": "equals", "value": "critical"}],
            actions=[{"type": "create_incident", "params": {"is_private": False}}],
        )
    )
    db_session.commit()


def test_critical_alert_creates_linked_incident(client, db_session):
    _seed(db_session)
    body = {"id": "a1", "name": "DB down", "sev": "critical", "state": "alerting"}
    r = client.post("/ingest/auto-tok", json=body)
    assert r.status_code == 200
    alert = db_session.scalar(select(Alert).where(Alert.dedup_key == "a1"))
    assert alert.incident_id is not None
    inc = db_session.scalar(select(Incident).where(Incident.id == alert.incident_id))
    assert inc.title == "DB down" and inc.created_by is None  # system-created


def test_non_matching_alert_creates_no_incident(client, db_session):
    _seed(db_session)
    client.post(
        "/ingest/auto-tok",
        json={"id": "a2", "name": "noise", "sev": "warning", "state": "alerting"},
    )
    alert = db_session.scalar(select(Alert).where(Alert.dedup_key == "a2"))
    assert alert.incident_id is None


def test_dedup_bump_does_not_refire(client, db_session):
    _seed(db_session)
    client.post(
        "/ingest/auto-tok",
        json={"id": "a3", "name": "flap", "sev": "critical", "state": "alerting"},
    )
    alert = db_session.scalar(select(Alert).where(Alert.dedup_key == "a3"))
    first_inc = alert.incident_id
    assert first_inc is not None
    # second firing of the SAME alert (dedup bump) must not create a second incident
    client.post(
        "/ingest/auto-tok",
        json={"id": "a3", "name": "flap", "sev": "critical", "state": "alerting"},
    )
    db_session.refresh(alert)
    assert alert.incident_id == first_inc
    assert db_session.scalar(select(Incident.id).where(Incident.id != first_inc)) is None
