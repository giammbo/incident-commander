import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import IncidentEvent, Role, SeverityLevel, StatusCategory, StatusLevel
from app.services import statuses
from app.services.incidents import create_incident
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


def _incident(db_session):
    statuses.seed_status_levels(db_session)
    db_session.flush()
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db_session.add(lvl)
    db_session.flush()
    inc = create_incident(
        db_session, title="X", severity_level_id=lvl.id, is_private=False, created_by=1
    )
    db_session.flush()
    return inc


def _updates(db_session, inc):
    return list(
        db_session.scalars(
            select(IncidentEvent).where(
                IncidentEvent.incident_id == inc.id, IncidentEvent.entry_type == "update"
            )
        )
    )


def test_readonly_cannot_post_update(client, db_session):
    _login(client, db_session, "ro@x.io", Role.read_only)
    assert (
        client.post(
            "/incidents/1/updates", data={"message": "x"}, follow_redirects=False
        ).status_code
        == 403
    )


def test_ic_posts_update_and_fires_webhook(client, db_session, monkeypatch):
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    inc = _incident(db_session)
    db_session.commit()
    events = []
    monkeypatch.setattr(
        "app.routers.incidents.webhooks.notify",
        lambda *a, **k: events.append((a[2], k.get("message"))),
    )
    r = client.post(
        f"/incidents/{inc.id}/updates",
        data={"message": "Mitigation deployed"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert _updates(db_session, inc)[0].body == "Mitigation deployed"
    assert events == [("update", "Mitigation deployed")]


def test_update_with_status_change(client, db_session):
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    inc = _incident(db_session)
    target = next(
        s
        for s in db_session.scalars(select(StatusLevel))
        if s.category == StatusCategory.active and s.id != inc.status_id
    )
    db_session.commit()
    client.post(
        f"/incidents/{inc.id}/updates",
        data={"message": "Now monitoring", "status_id": str(target.id)},
        follow_redirects=False,
    )
    db_session.refresh(inc)
    assert inc.status_id == target.id


def test_close_via_update_fires_both_webhooks(client, db_session, monkeypatch):
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    inc = _incident(db_session)
    closed = next(
        s for s in db_session.scalars(select(StatusLevel)) if s.category == StatusCategory.closed
    )
    db_session.commit()
    fired = []
    monkeypatch.setattr("app.routers.incidents.webhooks.notify", lambda *a, **k: fired.append(a[2]))
    r = client.post(
        f"/incidents/{inc.id}/updates",
        data={"message": "Resolved, closing out", "status_id": str(closed.id)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # the stakeholder "update" AND the lifecycle "closed" event both broadcast
    assert fired == ["update", "closed"]


def test_empty_message_flashes(client, db_session, monkeypatch):
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    inc = _incident(db_session)
    db_session.commit()
    fired = []
    monkeypatch.setattr("app.routers.incidents.webhooks.notify", lambda *a, **k: fired.append(a[2]))
    r = client.post(f"/incidents/{inc.id}/updates", data={"message": "   "}, follow_redirects=False)
    assert r.status_code == 303
    assert not _updates(db_session, inc)  # nothing recorded
    assert fired == []  # the empty-message path must not broadcast a webhook
