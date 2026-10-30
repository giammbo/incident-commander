from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import AlertRoute, EscalationLevel, EscalationPath, Role, Schedule, User
from app.services import oncall_catalog as cat
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session, get_db_override):
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override
    return TestClient(app)


def _login(client, db_session, *, role):
    _, pw = bootstrap_admin(db_session, "admin@localhost")
    db_session.flush()
    email = {"ic": "ic@x.io", "ro": "ro@x.io"}[role]
    r = {"ic": Role.incident_commander, "ro": Role.read_only}[role]
    create_user(
        db_session, email=email, name=email.split("@")[0].upper(), role=r, password="pw-123456"
    )
    db_session.flush()
    client.post("/login", data={"email": email, "password": "pw-123456"})


def _user(db_session, email):
    return db_session.scalar(select(User).where(User.email == email))


def _schedule(db_session, user, name="Payments primary"):
    s = cat.create_schedule(db_session, name=name, timezone="Europe/Rome")
    start = (datetime.now(UTC) - timedelta(days=3)).date()
    cat.add_rotation(
        db_session,
        s,
        name="Daily",
        user_ids=[user.id],
        shift_hours=24,
        handoff_local="09:00",
        start_date=start,
    )
    db_session.flush()
    return s


def test_ic_builds_two_level_path_and_reorders(client, db_session):
    _login(client, db_session, role="ic")
    ic = _user(db_session, "ic@x.io")
    s = _schedule(db_session, ic)
    r = client.post(
        "/oncall/escalation-paths",
        data={"name": "Payments", "repeat_times": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    path = db_session.scalar(select(EscalationPath).where(EscalationPath.name == "Payments"))
    assert path.repeat_times == 1
    assert r.headers["location"] == f"/oncall/escalation-paths/{path.id}"
    client.post(
        f"/oncall/escalation-paths/{path.id}/levels",
        data={"schedule_ids": [str(s.id)], "ack_timeout_minutes": "5"},
    )
    client.post(
        f"/oncall/escalation-paths/{path.id}/levels",
        data={"user_ids": [str(ic.id)], "ack_timeout_minutes": "10"},
    )
    db_session.expire_all()
    levels = list(path.levels)
    assert [lv.rank for lv in levels] == [1, 2]
    assert levels[0].schedule_ids == [s.id] and levels[1].user_ids == [ic.id]
    second = levels[1]
    client.post(f"/oncall/levels/{second.id}/move", data={"direction": "up"})
    db_session.expire_all()
    assert [lv.id for lv in path.levels][0] == second.id
    page = client.get(f"/oncall/escalation-paths/{path.id}").text
    assert "Notify whoever is on call on Payments primary" in page
    assert "repeat once" in page
    client.post(f"/oncall/levels/{second.id}/delete")
    db_session.expire_all()
    assert [lv.rank for lv in path.levels] == [1]


def test_level_without_targets_rejected(client, db_session):
    _login(client, db_session, role="ic")
    path = cat.create_path(db_session, name="P")
    db_session.flush()
    with pytest.raises(ValueError):
        cat.add_level(db_session, path, schedule_ids=[], user_ids=[], ack_timeout_minutes=5)
    with pytest.raises(ValueError):
        cat.add_level(
            db_session,
            path,
            schedule_ids=[],
            user_ids=[_user(db_session, "ic@x.io").id],
            ack_timeout_minutes=0,
        )
    db_session.commit()
    r = client.post(
        f"/oncall/escalation-paths/{path.id}/levels",
        data={"ack_timeout_minutes": "5"},
        follow_redirects=False,
    )
    assert r.status_code == 422 and "Check the form" in r.text
    db_session.expire_all()
    assert db_session.scalars(select(EscalationLevel)).all() == []


def test_path_used_by_route_cannot_be_deleted(client, db_session):
    _login(client, db_session, role="ic")
    path = cat.create_path(db_session, name="Used")
    db_session.add(AlertRoute(name="r", escalation_path_id=path.id))
    db_session.flush()
    with pytest.raises(ValueError):
        cat.delete_path(db_session, path)
    db_session.commit()  # the route rolls back on error; persist the setup first
    client.post(f"/oncall/escalation-paths/{path.id}/delete")
    db_session.expire_all()
    assert db_session.get(EscalationPath, path.id) is not None


def test_detail_shows_current_oncall_for_schedule_targets(client, db_session):
    _login(client, db_session, role="ic")
    ic = _user(db_session, "ic@x.io")
    s = _schedule(db_session, ic)
    path = cat.create_path(db_session, name="Pg")
    cat.add_level(db_session, path, schedule_ids=[s.id], user_ids=[], ack_timeout_minutes=5)
    db_session.flush()
    page = client.get(f"/oncall/escalation-paths/{path.id}").text
    assert "Now: Payments primary: IC" in page


def test_readonly_views_but_cannot_edit(client, db_session):
    _login(client, db_session, role="ro")
    path = cat.create_path(db_session, name="V")
    db_session.flush()
    assert client.get("/oncall/escalation-paths").status_code == 200
    assert client.get(f"/oncall/escalation-paths/{path.id}").status_code == 200
    assert client.post("/oncall/escalation-paths", data={"name": "X"}).status_code == 403
    assert client.post(f"/oncall/escalation-paths/{path.id}/delete").status_code == 403


def test_validation_bounds(db_session):
    with pytest.raises(ValueError):
        cat.create_path(db_session, name=" ")
    with pytest.raises(ValueError):
        cat.create_path(db_session, name="B", repeat_times=11)
    cat.create_path(db_session, name="Dup")
    db_session.flush()
    with pytest.raises(ValueError):
        cat.create_path(db_session, name="Dup")
    p = cat.create_path(db_session, name="Ok")
    with pytest.raises(ValueError):
        cat.add_level(db_session, p, schedule_ids=[999999], user_ids=[], ack_timeout_minutes=5)


def test_schedule_used_by_level_cannot_be_deleted(client, db_session):
    _login(client, db_session, role="ic")
    ic = _user(db_session, "ic@x.io")
    s = _schedule(db_session, ic)
    path = cat.create_path(db_session, name="Pg")
    lv = cat.add_level(db_session, path, schedule_ids=[s.id], user_ids=[], ack_timeout_minutes=5)
    db_session.commit()
    sid, lid = s.id, lv.id
    r = client.post(f"/oncall/schedules/{sid}/delete", follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    assert db_session.get(Schedule, sid) is not None
    assert "used by escalation path" in client.get("/oncall/schedules").text
    client.post(f"/oncall/levels/{lid}/edit", data={"user_ids": [str(ic.id)]})
    client.post(f"/oncall/schedules/{sid}/delete")
    db_session.expire_all()
    assert db_session.get(Schedule, sid) is None


def test_only_level_of_routed_path_cannot_be_deleted(client, db_session):
    _login(client, db_session, role="ic")
    ic = _user(db_session, "ic@x.io")
    routed = cat.create_path(db_session, name="Routed")
    lv = cat.add_level(db_session, routed, schedule_ids=[], user_ids=[ic.id], ack_timeout_minutes=5)
    free = cat.create_path(db_session, name="Free")
    lv2 = cat.add_level(db_session, free, schedule_ids=[], user_ids=[ic.id], ack_timeout_minutes=5)
    db_session.add(AlertRoute(name="r", escalation_path_id=routed.id))
    db_session.commit()
    lid, lid2 = lv.id, lv2.id
    client.post(f"/oncall/levels/{lid}/delete")
    db_session.expire_all()
    assert db_session.get(EscalationLevel, lid) is not None
    client.post(f"/oncall/levels/{lid2}/delete")
    db_session.expire_all()
    assert db_session.get(EscalationLevel, lid2) is None
