import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.models import (
    Escalation,
    EscalationLevel,
    EscalationPath,
    EscalationStatus,
    IncidentEvent,
    Role,
    SeverityLevel,
)
from app.services import statuses
from app.services.incidents import create_incident
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session, get_db_override):
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override
    return TestClient(app)


def _login(client, email):
    r = client.post("/login", data={"email": email, "password": "pw-123456"})
    assert r.status_code in (200, 303)


def _setup(db_session):
    bootstrap_admin(db_session, "admin@localhost")
    statuses.seed_status_levels(db_session)
    sev = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db_session.add(sev)
    ic = create_user(
        db_session, email="ic@x.io", name="Ic", role=Role.incident_commander, password="pw-123456"
    )
    ro = create_user(
        db_session, email="ro@x.io", name="Ro", role=Role.read_only, password="pw-123456"
    )
    db_session.flush()
    path = EscalationPath(name="Payments")
    db_session.add(path)
    db_session.flush()
    db_session.add(EscalationLevel(path_id=path.id, rank=1, user_ids=[ro.id], schedule_ids=[]))
    inc = create_incident(
        db_session,
        title="Checkout down",
        severity_level_id=sev.id,
        is_private=False,
        created_by=ic.id,
    )
    db_session.commit()
    return ic, ro, path, inc


def _events(db_session, inc_id, kind):
    return (
        db_session.query(IncidentEvent)
        .filter(IncidentEvent.incident_id == inc_id, IncidentEvent.entry_type == kind)
        .all()
    )


def test_ic_pages_path_from_incident_logs_timeline(client, db_session):
    ic, ro, path, inc = _setup(db_session)
    _login(client, "ic@x.io")
    assert client.get(f"/oncall/page?incident_id={inc.id}").status_code == 200
    r = client.post(
        "/oncall/page",
        data={
            "target_kind": "path",
            "path_id": path.id,
            "urgency": "high",
            "message": "",
            "incident_id": inc.id,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/incidents/{inc.id}?tab=timeline"
    esc = db_session.query(Escalation).one()
    assert esc.source == "manual" and esc.incident_id == inc.id and esc.path_id == path.id
    assert esc.title == "Checkout down"
    ev = _events(db_session, inc.id, "page")
    assert len(ev) == 1 and "Paged Payments path (high)" in ev[0].body
    page = client.get(f"/incidents/{inc.id}").text
    assert f"/oncall/escalations/{esc.id}" in page and "Page someone" in page


def test_ic_pages_users_directly_without_incident(client, db_session):
    ic, ro, path, inc = _setup(db_session)
    _login(client, "ic@x.io")
    r = client.post(
        "/oncall/page",
        data={"target_kind": "users", "user_ids": [ro.id], "urgency": "low", "message": "  Help  "},
        follow_redirects=False,
    )
    assert r.status_code == 303
    esc = db_session.query(Escalation).one()
    assert r.headers["location"] == f"/oncall/escalations/{esc.id}"
    assert esc.direct_user_ids == [ro.id] and esc.incident_id is None
    assert esc.title == "Page from Ic: Help" and esc.message == "Help"
    assert esc.initiated_by == ic.id


def test_readonly_cannot_page(client, db_session):
    ic, ro, path, inc = _setup(db_session)
    _login(client, "ro@x.io")
    assert client.get("/oncall/page").status_code == 403
    r = client.post(
        "/oncall/page",
        data={"target_kind": "users", "user_ids": [ro.id], "urgency": "high"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert db_session.query(Escalation).count() == 0


def test_page_requires_path_or_users(client, db_session):
    ic, ro, path, inc = _setup(db_session)
    _login(client, "ic@x.io")
    for data in (
        {"target_kind": "users", "urgency": "high"},
        {"target_kind": "path", "urgency": "high"},
        {"target_kind": "path", "path_id": 9999, "urgency": "high"},
        {"target_kind": "users", "user_ids": [9999], "urgency": "high"},
        {"target_kind": "users", "user_ids": [ro.id], "urgency": "bogus"},
    ):
        r = client.post("/oncall/page", data=data, follow_redirects=False)
        # Invalid forms are now rendered directly with bound values.
        assert r.status_code == 422 and "Check the form" in r.text
    empty = EscalationPath(name="Empty")
    db_session.add(empty)
    db_session.commit()
    r = client.post(
        "/oncall/page",
        data={"target_kind": "path", "path_id": empty.id, "urgency": "high"},
        follow_redirects=False,
    )
    assert r.status_code == 422 and "That escalation path has no levels" in r.text
    assert db_session.query(Escalation).count() == 0


def test_ack_of_manual_page_logged_on_incident_timeline(client, db_session):
    ic, ro, path, inc = _setup(db_session)
    _login(client, "ic@x.io")
    client.post(
        "/oncall/page",
        data={"target_kind": "path", "path_id": path.id, "urgency": "high", "incident_id": inc.id},
    )
    esc = db_session.query(Escalation).one()
    r = client.post(f"/oncall/escalations/{esc.id}/ack", follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    assert db_session.get(Escalation, esc.id).status == EscalationStatus.acknowledged
    assert len(_events(db_session, inc.id, "page_acked")) == 1
