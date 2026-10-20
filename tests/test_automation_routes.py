import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import IncidentType, Role, SeverityLevel, WorkflowRule
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session, get_db_override):
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override
    return TestClient(app)


def _login(client, db_session, role):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(db_session, email="u@x.io", name="U", role=role, password="pw-123456")
    db_session.flush()
    client.post("/login", data={"email": "u@x.io", "password": "pw-123456"})


def test_non_admin_cannot_list(client, db_session):
    _login(client, db_session, Role.incident_commander)
    assert client.get("/automations", follow_redirects=False).status_code == 403


def test_admin_creates_rule(client, db_session):
    _login(client, db_session, Role.admin)
    r = client.post(
        "/automations",
        headers={"origin": "http://testserver"},
        follow_redirects=False,
        data={
            "name": "r1",
            "trigger": "incident.opened",
            "cond_field": ["severity"],
            "cond_op": ["equals"],
            "cond_value": ["1"],
            "action_type": "create_followup",
            "ap_title": "x",
        },
    )
    assert r.status_code == 303
    rule = db_session.scalar(select(WorkflowRule).where(WorkflowRule.name == "r1"))
    assert rule is not None and rule.trigger == "incident.opened"
    assert rule.conditions == [{"field": "severity", "op": "equals", "value": 1}]
    assert rule.actions == [
        {"type": "create_followup", "params": {"title": "x", "assignee_id": None}}
    ]


def test_invalid_action_for_trigger_rejected(client, db_session):
    _login(client, db_session, Role.admin)
    # create_incident is an alert-only action; on an incident trigger it must be rejected
    # (flash + redirect), not silently stored as a dead rule.
    r = client.post(
        "/automations",
        headers={"origin": "http://testserver"},
        follow_redirects=False,
        data={
            "name": "bad",
            "trigger": "incident.opened",
            "action_type": "create_incident",
            "ap_private": "false",
        },
    )
    assert r.status_code == 303
    assert db_session.scalar(select(WorkflowRule).where(WorkflowRule.name == "bad")) is None


def test_toggle_and_delete(client, db_session):
    _login(client, db_session, Role.admin)
    rule = WorkflowRule(
        name="r", trigger="incident.opened", conditions=[], actions=[], enabled=True
    )
    db_session.add(rule)
    db_session.commit()
    client.post(
        f"/automations/{rule.id}/toggle",
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    db_session.refresh(rule)
    assert rule.enabled is False
    client.post(
        f"/automations/{rule.id}/delete",
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    assert db_session.scalar(select(WorkflowRule).where(WorkflowRule.id == rule.id)) is None


def test_admin_creates_rule_from_picker_rows(client, db_session):
    _login(client, db_session, Role.admin)
    s1 = SeverityLevel(label="SEV1", color="#FF0000", rank=1, is_default=True)
    s2 = SeverityLevel(label="SEV2", color="#FF8800", rank=2)
    t = IncidentType(label="Outage", rank=1)
    db_session.add_all([s1, s2, t])
    db_session.flush()
    r = client.post(
        "/automations",
        headers={"origin": "http://testserver"},
        follow_redirects=False,
        data={
            "name": "picker",
            "trigger": "incident.opened",
            # multi-select "is one of", an unchecked is_private checkbox, a blank row
            "cond-0-field": "severity",
            "cond-0-op": "in",
            "cond-0-value": [str(s1.id), str(s2.id)],
            "cond-1-field": "is_private",
            "cond-1-op": "equals",
            "cond-2-field": "",
            "action_type": "post_update",
            "ap_message": "hi",
        },
    )
    assert r.status_code == 303
    rule = db_session.scalar(select(WorkflowRule).where(WorkflowRule.name == "picker"))
    assert rule.conditions == [
        {"field": "severity", "op": "in", "value": [s1.id, s2.id]},
        {"field": "is_private", "op": "equals", "value": False},
    ]
    assert rule.actions == [{"type": "post_update", "params": {"message": "hi"}}]


def test_picker_rows_for_alert_trigger(client, db_session):
    _login(client, db_session, Role.admin)
    client.post(
        "/automations",
        headers={"origin": "http://testserver"},
        follow_redirects=False,
        data={
            "name": "alerts",
            "trigger": "alert.received",
            "cond-0-field": "severity_raw",
            "cond-0-op": "in",
            "cond-0-value": "critical, high",
            "cond-1-field": "status",
            "cond-1-op": "equals",
            "cond-1-value": "firing",
            "cond-2-field": "source",
            "cond-2-op": "equals",
            "cond-2-value": "  ",  # blank text value → row skipped
            "action_type": "create_incident",
        },
    )
    rule = db_session.scalar(select(WorkflowRule).where(WorkflowRule.name == "alerts"))
    assert rule.conditions == [
        {"field": "severity_raw", "op": "in", "value": ["critical", "high"]},
        {"field": "status", "op": "equals", "value": "firing"},
    ]


def test_picker_field_not_allowed_for_trigger_rejected(client, db_session):
    _login(client, db_session, Role.admin)
    client.post(
        "/automations",
        headers={"origin": "http://testserver"},
        follow_redirects=False,
        data={
            "name": "wrong",
            "trigger": "alert.received",
            "cond-0-field": "is_private",
            "cond-0-value": "true",
            "action_type": "create_incident",
        },
    )
    assert db_session.scalar(select(WorkflowRule).where(WorkflowRule.name == "wrong")) is None


def test_builder_and_value_partials_require_admin(client, db_session):
    _login(client, db_session, Role.incident_commander)
    for url in (
        "/automations/builder?trigger=alert.received",
        "/automations/condition-value?i=0&cond-0-field=severity",
    ):
        assert client.get(url, follow_redirects=False).status_code == 403
