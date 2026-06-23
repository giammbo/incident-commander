import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import Role, WorkflowRule
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
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
