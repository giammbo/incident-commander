import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import FollowUp, Incident, Role, SeverityLevel, StatusLevel, WorkflowRule
from app.services import statuses
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


def _seed(db_session):
    statuses.seed_status_levels(db_session)
    db_session.flush()
    sev = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db_session.add(sev)
    db_session.flush()
    return sev


def test_declare_fires_matching_rule(client, db_session):
    _login_ic(client, db_session)
    sev = _seed(db_session)
    db_session.add(
        WorkflowRule(
            name="sev1",
            trigger="incident.opened",
            conditions=[{"field": "severity", "op": "equals", "value": sev.id}],
            actions=[{"type": "create_followup", "params": {"title": "auto postmortem"}}],
        )
    )
    db_session.commit()
    r = client.post(
        "/incidents",
        data={"title": "Boom", "severity_level_id": str(sev.id)},
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code in (200, 303)
    inc = db_session.scalar(select(Incident).where(Incident.title == "Boom"))
    assert any(
        f.title == "auto postmortem"
        for f in db_session.scalars(select(FollowUp).where(FollowUp.incident_id == inc.id))
    )


def test_status_change_fires_rule_without_looping(client, db_session, monkeypatch):
    _login_ic(client, db_session)
    sev = _seed(db_session)
    inc = __import__("app.services.incidents", fromlist=["create_incident"]).create_incident(
        db_session, title="S", severity_level_id=sev.id, is_private=False, created_by=1
    )
    db_session.flush()
    target = next(s for s in db_session.scalars(select(StatusLevel)) if s.id != inc.status_id)
    db_session.add(
        WorkflowRule(
            name="on-status",
            trigger="incident.status_changed",
            conditions=[],
            actions=[{"type": "create_followup", "params": {"title": "status-hook"}}],
        )
    )
    db_session.commit()
    calls = []
    real = __import__("app.services.automation", fromlist=["run_rules"]).run_rules
    monkeypatch.setattr(
        "app.routers.incidents.automation.run_rules",
        lambda *a, **k: (calls.append(k.get("trigger")), real(*a, **k))[1],
    )
    client.post(
        f"/incidents/{inc.id}/status",
        data={"status_id": str(target.id)},
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    # status route dispatched exactly once; the set_status path inside actions did NOT re-dispatch
    assert calls == ["incident.status_changed"]
