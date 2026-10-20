import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.services.webhooks as wh
from app.db import get_db
from app.main import create_app
from app.models import (
    Alert,
    InboundIntegration,
    Incident,
    IncidentEvent,
    SeverityLevel,
    Webhook,
    WorkflowRule,
)
from app.services import statuses


@pytest.fixture
def client(db_session, get_db_override):
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override
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


def _automation_bodies(db_session, incident_id):
    return list(
        db_session.scalars(
            select(IncidentEvent.body).where(
                IncidentEvent.incident_id == incident_id,
                IncidentEvent.entry_type == "automation",
            )
        )
    )


def test_auto_created_incident_timeline_names_the_rule(client, db_session):
    _seed(db_session)
    client.post(
        "/ingest/auto-tok",
        json={"id": "a4", "name": "Disk full", "sev": "critical", "state": "alerting"},
    )
    alert = db_session.scalar(select(Alert).where(Alert.dedup_key == "a4"))
    bodies = _automation_bodies(db_session, alert.incident_id)
    assert "Created by automation 'critical->incident' from alert: Disk full" in bodies


def test_auto_created_incident_runs_opened_rules(client, db_session):
    _seed(db_session)
    db_session.add(
        WorkflowRule(name="on-open", trigger="incident.opened", conditions=[], actions=[])
    )
    db_session.commit()
    client.post(
        "/ingest/auto-tok",
        json={"id": "a5", "name": "API 5xx", "sev": "critical", "state": "alerting"},
    )
    alert = db_session.scalar(select(Alert).where(Alert.dedup_key == "a5"))
    assert "Automation 'on-open' ran" in _automation_bodies(db_session, alert.incident_id)


def test_ingest_fires_opened_webhook_for_auto_created_incident(client, db_session, webhook_posts):
    _seed(db_session)
    db_session.add(Webhook(name="W", url="https://hooks/x", format="slack", enabled=True))
    db_session.commit()
    posts = webhook_posts
    r = client.post(
        "/ingest/auto-tok",
        json={"id": "a6", "name": "Queue stuck", "sev": "critical", "state": "alerting"},
    )
    assert r.status_code == 200
    opened = [j for _, j in posts if "incident opened: Queue stuck" in (j.get("text") or "")]
    assert len(opened) == 1
    # a non-matching alert creates no incident, so no further webhook
    client.post(
        "/ingest/auto-tok",
        json={"id": "a7", "name": "noise", "sev": "warning", "state": "alerting"},
    )
    assert len(posts) == 1


def test_ingest_never_500s_when_webhook_post_raises(client, db_session, monkeypatch, webhook_posts):
    _seed(db_session)
    db_session.add(Webhook(name="W", url="https://hooks/x", format="slack", enabled=True))
    db_session.commit()

    def boom(url, **kwargs):
        raise RuntimeError("endpoint down")

    monkeypatch.setattr(wh, "_http_post", boom)
    r = client.post(
        "/ingest/auto-tok",
        json={"id": "a8", "name": "Cache down", "sev": "critical", "state": "alerting"},
    )
    assert r.status_code == 200
    alert = db_session.scalar(select(Alert).where(Alert.dedup_key == "a8"))
    assert alert.incident_id is not None


def test_refired_linked_alert_logs_on_the_linked_incident(client, db_session):
    # A resolved alert that fires again stays linked to its incident: the matching
    # rule doesn't create a second incident, but the linked incident's timeline
    # records that the automation matched again.
    _seed(db_session)
    payload = {"id": "a9", "name": "Flappy", "sev": "critical", "state": "alerting"}
    client.post("/ingest/auto-tok", json=payload)
    client.post("/ingest/auto-tok", json={**payload, "state": "ok"})
    client.post("/ingest/auto-tok", json=payload)
    alert = db_session.scalar(select(Alert).where(Alert.dedup_key == "a9"))
    assert db_session.scalar(select(Incident.id).where(Incident.id != alert.incident_id)) is None
    bodies = _automation_bodies(db_session, alert.incident_id)
    assert (
        "Automation 'critical->incident' matched alert: Flappy (already linked, no new incident)"
    ) in bodies
