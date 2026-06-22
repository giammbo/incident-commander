import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.services.incident_actions as actions
from app.db import get_db
from app.main import create_app
from app.models import Component, Incident, Role, SeverityLevel, SlackConnection, System
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _setup(client, db_session):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(
        db_session, email="ic@x.io", name="IC", role=Role.incident_commander, password="pw-123456"
    )
    l1 = SeverityLevel(label="SEV2", color="#F4B740", rank=2, is_default=True)
    l2 = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1)
    sysm = System(name="Core")
    db_session.add_all([l1, l2, sysm])
    db_session.flush()
    svc = Component(name="Checkout", system_id=sysm.id)
    db_session.add(svc)
    db_session.flush()
    client.post("/login", data={"email": "ic@x.io", "password": "pw-123456"})
    return l1, l2, svc


def test_edit_updates_fields(client, db_session):
    l1, l2, svc = _setup(client, db_session)
    client.post(
        "/incidents",
        data={"title": "Old", "severity_level_id": str(l1.id)},
        headers={"HX-Request": "true"},
    )
    inc = db_session.scalar(select(Incident).order_by(Incident.id.desc()))
    r = client.post(
        f"/incidents/{inc.id}/edit",
        data={
            "title": "New title",
            "description": "details",
            "severity_level_id": str(l2.id),
            "system_id": str(svc.system_id),
            "component_ids": [str(svc.id)],
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    db_session.refresh(inc)
    assert inc.title == "New title" and inc.description == "details"
    assert inc.severity_level_id == l2.id
    assert [c.name for c in inc.components] == ["Checkout"]


def test_edit_readonly_forbidden(client, db_session):
    l1, l2, svc = _setup(client, db_session)
    create_user(db_session, email="ro@x.io", name="RO", role=Role.read_only, password="pw-123456")
    db_session.flush()
    client.post(
        "/incidents",
        data={"title": "Old", "severity_level_id": str(l1.id)},
        headers={"HX-Request": "true"},
    )
    inc = db_session.scalar(select(Incident).order_by(Incident.id.desc()))
    client.get("/logout")
    client.post("/login", data={"email": "ro@x.io", "password": "pw-123456"})
    client.post("/account/password", data={"new_password": "Reader-1", "confirm": "Reader-1"})
    r = client.post(
        f"/incidents/{inc.id}/edit",
        data={"title": "x", "severity_level_id": str(l1.id)},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_edit_posts_slack_update(client, db_session, monkeypatch):
    l1, l2, svc = _setup(client, db_session)
    conn = SlackConnection(team_id="T1", team_name="Acme", bot_token="xoxb-1", created_by=1)
    db_session.add(conn)
    db_session.flush()
    posts = []
    monkeypatch.setattr(actions.slack, "post_message", lambda token, **k: posts.append(k["text"]))
    monkeypatch.setattr(actions.slack, "set_topic_purpose", lambda token, **k: None)
    client.post(
        "/incidents",
        data={"title": "Old", "severity_level_id": str(l1.id)},
        headers={"HX-Request": "true"},
    )
    inc = db_session.scalar(select(Incident).order_by(Incident.id.desc()))
    inc.slack_channel_id = "C1"
    inc.slack_connection_id = conn.id
    db_session.flush()
    posts.clear()
    client.post(
        f"/incidents/{inc.id}/edit",
        data={"title": "New", "severity_level_id": str(l2.id)},
        follow_redirects=False,
    )
    assert any("updated" in p.lower() for p in posts)
    db_session.refresh(inc)
    assert inc.creation_state.get("updated_announce") == "ok"


def test_edit_slack_message_includes_scope(client, db_session, monkeypatch):
    from app.services.catalog import create_component, create_system

    l1, l2, comp = _setup(client, db_session)  # _setup now returns a Component in a System
    conn = SlackConnection(team_id="T2", team_name="Acme", bot_token="xoxb-1", created_by=1)
    db_session.add(conn)
    s2 = create_system(db_session, name="Payments")
    db_session.flush()
    billing = create_component(db_session, name="Ledger", system_id=s2.id)
    db_session.flush()
    posts = []
    monkeypatch.setattr(actions.slack, "post_message", lambda token, **k: posts.append(k["text"]))
    monkeypatch.setattr(actions.slack, "set_topic_purpose", lambda token, **k: None)
    client.post(
        "/incidents",
        data={"title": "Old", "severity_level_id": str(l1.id)},
        headers={"HX-Request": "true"},
    )
    inc = db_session.scalar(select(Incident).order_by(Incident.id.desc()))
    inc.slack_channel_id = "C1"
    inc.slack_connection_id = conn.id
    db_session.flush()
    posts.clear()
    client.post(
        f"/incidents/{inc.id}/edit",
        data={
            "title": "New",
            "severity_level_id": str(l2.id),
            "system_id": str(s2.id),
            "component_ids": [str(billing.id)],
        },
        follow_redirects=False,
    )
    assert any("Payments" in p and "Ledger" in p for p in posts)
