import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.services.incident_actions as actions
from app.db import get_db
from app.main import create_app
from app.models import Incident, Role, SeverityLevel, SlackConnection
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _sev(db_session):
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=False)
    db_session.add(lvl)
    db_session.flush()
    return lvl.id


def _seed(client, db_session):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(
        db_session, email="ic@x.io", name="IC", role=Role.incident_commander, password="pw-123456"
    )
    conn = SlackConnection(team_id="T1", team_name="Acme", bot_token="xoxb-1", created_by=1)
    db_session.add(conn)
    db_session.flush()
    client.post("/login", data={"email": "ic@x.io", "password": "pw-123456"})
    return conn


def test_create_incident_opens_channel(client, db_session, monkeypatch):
    conn = _seed(client, db_session)
    sev_id = _sev(db_session)
    calls = {}
    monkeypatch.setattr(
        actions.slack,
        "create_channel",
        lambda token, **k: (
            (calls.update({"create": (token, k)}) or None) or {"id": "C9", "name": k["name"]}
        ),
    )
    monkeypatch.setattr(
        actions.slack, "set_topic_purpose", lambda token, **k: calls.setdefault("topic", k)
    )
    monkeypatch.setattr(
        actions.slack, "post_message", lambda token, **k: calls.setdefault("post", k)
    )

    r = client.post(
        "/incidents",
        data={
            "title": "Checkout 5xx",
            "severity_level_id": str(sev_id),
            "slack_connection_id": str(conn.id),
        },
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    inc = db_session.scalar(select(Incident).order_by(Incident.id.desc()))
    assert inc.slack_channel_id == "C9"
    assert inc.creation_state["channel"] == "ok"
    assert calls["create"][0] == "xoxb-1"
    assert "post" in calls


def test_close_incident_posts_closing_message(client, db_session, monkeypatch):
    conn = _seed(client, db_session)
    sev_id = _sev(db_session)
    monkeypatch.setattr(
        actions.slack, "create_channel", lambda token, **k: {"id": "C9", "name": k["name"]}
    )
    monkeypatch.setattr(actions.slack, "set_topic_purpose", lambda token, **k: None)
    posts = []
    monkeypatch.setattr(actions.slack, "post_message", lambda token, **k: posts.append(k["text"]))

    client.post(
        "/incidents",
        data={"title": "X", "severity_level_id": str(sev_id), "slack_connection_id": str(conn.id)},
        headers={"HX-Request": "true"},
    )
    inc = db_session.scalar(select(Incident).order_by(Incident.id.desc()))
    posts.clear()
    r = client.post(f"/incidents/{inc.id}/close", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert any("closed" in p.lower() or "resolved" in p.lower() for p in posts)
    db_session.refresh(inc)
    assert inc.creation_state.get("closed_announce") == "ok"


def test_create_incident_channel_failure_is_graceful(client, db_session, monkeypatch):
    conn = _seed(client, db_session)
    sev_id = _sev(db_session)

    def boom(token, **k):
        raise RuntimeError("slack down")

    monkeypatch.setattr(actions.slack, "create_channel", boom)
    r = client.post(
        "/incidents",
        data={"title": "X", "severity_level_id": str(sev_id), "slack_connection_id": str(conn.id)},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200  # incident still created, no 500
    inc = db_session.scalar(select(Incident).order_by(Incident.id.desc()))
    assert inc.slack_channel_id is None
    assert inc.creation_state["channel"] == "failed"
