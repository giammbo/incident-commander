"""On-call UX package 3: polish (queued sentences, disabled move buttons, test button, admin name)."""

import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import NotificationJob, Role, Urgency, User
from app.security.passwords import hash_password
from app.services import oncall_catalog as cat
from app.services import oncall_engine as eng
from app.services import oncall_notify
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session, get_db_override):
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override
    return TestClient(app)


def _login(client, db_session):
    if db_session.scalar(select(User).where(User.email == "admin@localhost")) is None:
        bootstrap_admin(db_session, "admin@localhost")
    create_user(
        db_session, email="u@x.io", name="Una", role=Role.incident_commander, password="pw-123456"
    )
    db_session.commit()
    client.post("/login", data={"email": "u@x.io", "password": "pw-123456"})
    return db_session.scalar(select(User).where(User.email == "u@x.io"))


def _buttons(html, label):
    return [m for m in re.findall(r"<button[^>]*>[^<]*</button>", html) if label in m]


def test_escalation_detail_queued_jobs_as_sentences(client, db_session):
    u = _login(client, db_session)
    now = datetime.now(UTC)
    esc = eng.start_escalation(
        db_session, urgency=Urgency.high, title="DB down", user_ids=[u.id], source="manual", now=now
    )
    db_session.add_all(
        [
            NotificationJob(escalation_id=esc.id, kind="level", level=1, run_at=now),
            NotificationJob(
                escalation_id=esc.id,
                kind="notify",
                level=0,
                run_at=now,
                user_id=u.id,
                channel="email",
            ),
            NotificationJob(escalation_id=esc.id, kind="exhaust", run_at=now),
        ]
    )
    db_session.commit()
    html = client.get(f"/oncall/escalations/{esc.id}").text
    assert "Next: Level 2 at" in html
    assert "if nobody acknowledges" in html
    assert "to Una at" in html
    assert "Stop paging (exhausted) at" in html
    assert "Queued" not in html


def test_path_level_move_buttons_disabled_at_ends(client, db_session):
    u = _login(client, db_session)
    p = cat.create_path(db_session, name="P")
    for _ in range(3):
        cat.add_level(db_session, p, schedule_ids=[], user_ids=[u.id], ack_timeout_minutes=5)
    db_session.commit()
    html = client.get(f"/oncall/escalation-paths/{p.id}").text
    ups, downs = _buttons(html, "Up"), _buttons(html, "Down")
    assert ["disabled" in b for b in ups] == [True, False, False]
    assert ["disabled" in b for b in downs] == [False, False, True]


def test_send_test_disabled_with_reason_for_unavailable_channel(client, db_session, monkeypatch):
    _login(client, db_session)

    class Off:
        key = "sms"
        label = "SMS"

        def available(self, db):
            return False

        def reachable(self, db, user):
            return False

    monkeypatch.setitem(oncall_notify.CHANNELS, "sms", Off())
    html = client.get("/account/notifications").text
    btn = _buttons(html, "Send me a test")
    assert any("disabled" in b and 'title="' in b and 'title=""' not in b for b in btn)


def _admin_must_change(client, db_session):
    admin, _ = bootstrap_admin(db_session, "admin@localhost")
    admin.password_hash = hash_password("old-pass-123")
    db_session.commit()
    client.post("/login", data={"email": "admin@localhost", "password": "old-pass-123"})
    return admin


def test_bootstrap_admin_first_password_change_sets_name(client, db_session):
    admin = _admin_must_change(client, db_session)
    assert 'name="name"' in client.get("/account/password").text
    r = client.post(
        "/account/password", data={"new_password": "new-pass-123", "confirm": "new-pass-123"}
    )
    assert r.status_code == 400 and "name" in r.text.lower()
    r = client.post(
        "/account/password",
        data={"new_password": "new-pass-123", "confirm": "new-pass-123", "name": "  Gianmarco C  "},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db_session.refresh(admin)
    assert admin.name == "Gianmarco C" and not admin.must_change_password
    # later changes do not ask for a name
    assert 'name="name"' not in client.get("/account/password").text


def test_name_field_absent_for_non_bootstrap_user(client, db_session):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(
        db_session,
        email="n@x.io",
        name="Nina",
        role=Role.read_only,
        password="pw-123456",
        must_change_password=True,
    )
    db_session.commit()
    client.post("/login", data={"email": "n@x.io", "password": "pw-123456"})
    assert 'name="name"' not in client.get("/account/password").text
    r = client.post(
        "/account/password",
        data={"new_password": "new-pass-123", "confirm": "new-pass-123", "name": "Hacker"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert db_session.scalar(select(User).where(User.email == "n@x.io")).name == "Nina"
