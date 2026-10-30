import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import Role, Schedule, ScheduleOverride, User
from app.services import oncall_catalog as cat
from app.services.oncall_schedule import oncall_user_ids
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session, get_db_override):
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override
    return TestClient(app)


def _login(client, db_session, *, role):
    _, pw = bootstrap_admin(db_session, "admin@localhost")
    db_session.flush()
    if role == "admin":
        client.post("/login", data={"email": "admin@localhost", "password": pw})
        client.post(
            "/account/password",
            data={"new_password": "Admin-123", "confirm": "Admin-123", "name": "Admin"},
        )
        return
    email = {"ic": "ic@x.io", "ro": "ro@x.io"}[role]
    r = {"ic": Role.incident_commander, "ro": Role.read_only}[role]
    create_user(db_session, email=email, name=email, role=r, password="pw-123456")
    db_session.flush()
    client.post("/login", data={"email": email, "password": "pw-123456"})


def _user(db_session, email):
    return db_session.scalar(select(User).where(User.email == email))


def _schedule_with_rotation(db_session, user, tz="Europe/Rome"):
    s = cat.create_schedule(db_session, name="Primary", timezone=tz)
    start = (datetime.now(UTC) - timedelta(days=3)).date()
    cat.add_rotation(
        db_session,
        s,
        name="Weekly",
        user_ids=[user.id],
        shift_hours=24,
        handoff_local="09:00",
        start_date=start,
    )
    db_session.flush()
    return s


def test_readonly_sees_oncall_pages_but_cannot_create_schedule(client, db_session):
    _login(client, db_session, role="ro")
    assert client.get("/oncall").status_code == 200
    assert client.get("/oncall/schedules").status_code == 200
    r = client.post("/oncall/schedules", data={"name": "X", "timezone": "UTC"})
    assert r.status_code == 403


def test_ic_creates_schedule_and_rotation_and_now_page_shows_current_user(client, db_session):
    _login(client, db_session, role="ic")
    ic = _user(db_session, "ic@x.io")
    r = client.post(
        "/oncall/schedules",
        data={"name": "Primary", "timezone": "Europe/Rome"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    s = db_session.scalar(select(Schedule).where(Schedule.name == "Primary"))
    start = (datetime.now(UTC) - timedelta(days=2)).date().isoformat()
    r = client.post(
        f"/oncall/schedules/{s.id}/rotations",
        data={
            "name": "Daily",
            "user_ids": [str(ic.id)],
            "shift_hours": "24",
            "handoff_local": "09:00",
            "start_date": start,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    db_session.refresh(s)
    assert len(s.rotations) == 1
    page = client.get("/oncall")
    assert page.status_code == 200
    assert "Primary" in page.text and "ic@x.io" in page.text
    detail = client.get(f"/oncall/schedules/{s.id}")
    assert detail.status_code == 200 and "Europe/Rome" in detail.text


def test_invalid_timezone_flashes_error(client, db_session):
    _login(client, db_session, role="ic")
    r = client.post(
        "/oncall/schedules", data={"name": "Bad", "timezone": "Mars/Base"}, follow_redirects=True
    )
    assert "Invalid timezone" in r.text
    assert db_session.scalar(select(Schedule).where(Schedule.name == "Bad")) is None


def test_editing_rotation_creates_version_and_keeps_current_oncall(client, db_session):
    _login(client, db_session, role="ic")
    ic = _user(db_session, "ic@x.io")
    other = create_user(db_session, email="o@x.io", name="Other", role=Role.read_only)
    db_session.flush()
    s = _schedule_with_rotation(db_session, ic)
    rot = s.rotations[0]
    now = datetime.now(UTC)
    assert oncall_user_ids(s, now) == [ic.id]
    tomorrow = (now + timedelta(days=1)).astimezone().strftime("%Y-%m-%dT%H:%M")
    r = client.post(
        f"/oncall/rotations/{rot.id}/edit",
        data={
            "user_ids": [str(other.id)],
            "shift_hours": "24",
            "handoff_local": "09:00",
            "effective_from": tomorrow,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    db_session.refresh(rot)
    assert len(rot.versions) == 2
    assert rot.versions[0].user_ids == [ic.id]
    db_session.refresh(s)
    assert oncall_user_ids(s, now) == [ic.id]
    assert oncall_user_ids(s, now + timedelta(days=3)) == [other.id]


def test_readonly_can_take_shift_for_self_but_not_create_override_for_others(client, db_session):
    _login(client, db_session, role="ro")
    ro = _user(db_session, "ro@x.io")
    ic = create_user(db_session, email="ic2@x.io", name="IC2", role=Role.incident_commander)
    db_session.flush()
    s = _schedule_with_rotation(db_session, ic)
    a = datetime.now(UTC) + timedelta(hours=1)
    b = a + timedelta(hours=4)
    r = client.post(
        f"/oncall/schedules/{s.id}/take",
        data={"starts_at": a.isoformat(), "ends_at": b.isoformat(), "user_id": str(ic.id)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    ov = db_session.scalars(select(ScheduleOverride)).all()
    assert len(ov) == 1 and ov[0].user_id == ro.id and ov[0].created_by == ro.id
    r = client.post(
        f"/oncall/schedules/{s.id}/overrides",
        data={"user_id": str(ic.id), "starts_at": a.isoformat(), "ends_at": b.isoformat()},
    )
    assert r.status_code == 403


def test_override_end_before_start_rejected(client, db_session):
    _login(client, db_session, role="ic")
    ic = _user(db_session, "ic@x.io")
    s = _schedule_with_rotation(db_session, ic)
    db_session.commit()  # the route rolls back on error; keep the fixture rows
    r = client.post(
        f"/oncall/schedules/{s.id}/overrides",
        data={
            "user_id": str(ic.id),
            "starts_at": "2030-01-02T10:00",
            "ends_at": "2030-01-02T09:00",
        },
        follow_redirects=True,
    )
    assert "must end after" in r.text
    assert db_session.scalars(select(ScheduleOverride)).all() == []


def test_sidebar_has_oncall_link(client, db_session):
    _login(client, db_session, role="ro")
    html = client.get("/oncall").text
    response_nav = re.search(r'<nav\b[^>]*aria-label="Response"[^>]*>(.*?)</nav>', html, re.S)
    assert response_nav is not None
    for destination in ("/", "/alerts", "/oncall"):
        assert f'href="{destination}"' in response_nav.group(1)


def test_catalog_validation(db_session):
    u = create_user(db_session, email="v@x.io", name="V", role=Role.read_only)
    db_session.flush()
    s = cat.create_schedule(db_session, name="S", timezone="UTC")
    with pytest.raises(ValueError):
        cat.create_schedule(db_session, name="S", timezone="UTC")
    from datetime import date

    base = dict(
        name="r",
        user_ids=[u.id],
        shift_hours=24,
        handoff_local="09:00",
        start_date=date(2030, 1, 1),
    )
    for bad in (
        {"shift_hours": 0},
        {"shift_hours": 24 * 29},
        {"user_ids": []},
        {"user_ids": [999999]},
        {"handoff_local": "9am"},
        {"windows": [{"days": [7], "start": "09:00", "end": "17:00"}]},
        {"windows": [{"days": [1], "start": "17:00", "end": "09:00"}]},
        {"windows": [{"days": [1], "start": "09:00"}]},
        {"windows": [{"days": [], "start": "09:00", "end": "10:00"}]},
    ):
        with pytest.raises(ValueError):
            cat.add_rotation(db_session, s, **{**base, **bad})
    rot = cat.add_rotation(db_session, s, **base)
    with pytest.raises(ValueError):
        cat.update_rotation(
            db_session,
            rot,
            user_ids=[u.id],
            shift_hours=0,
            handoff_local="09:00",
            windows=None,
            effective_from=datetime.now(UTC),
        )
    u.is_active = False
    db_session.flush()
    with pytest.raises(ValueError):
        cat.add_rotation(db_session, s, **base)


def test_move_and_delete_rotation_keep_ranks_dense(db_session):
    from datetime import date

    u = create_user(db_session, email="m@x.io", name="M", role=Role.read_only)
    db_session.flush()
    s = cat.create_schedule(db_session, name="S", timezone="UTC")
    kw = dict(user_ids=[u.id], shift_hours=24, handoff_local="09:00", start_date=date(2030, 1, 1))
    a = cat.add_rotation(db_session, s, name="a", **kw)
    b = cat.add_rotation(db_session, s, name="b", **kw)
    cat.move_rotation(db_session, b, "up")
    assert (b.rank, a.rank) == (1, 2)
    cat.delete_rotation(db_session, b)
    assert a.rank == 1


def _weekly(db_session, user, other):
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("UTC")
    s = cat.create_schedule(db_session, name="Weekly", timezone="UTC")
    now = datetime.now(UTC)
    monday = (now - timedelta(days=now.weekday() + 14)).date()  # a past Monday
    rot = cat.add_rotation(
        db_session,
        s,
        name="w",
        user_ids=[user.id, other.id],
        shift_hours=168,
        handoff_local="09:00",
        start_date=monday,
    )
    assert datetime.combine(monday, datetime.min.time(), tz).weekday() == 0
    db_session.flush()
    return s, rot


def test_weekly_rotation_people_only_edit_keeps_monday_handoff(db_session):
    a = create_user(db_session, email="a@x.io", name="A", role=Role.read_only)
    b = create_user(db_session, email="b@x.io", name="B", role=Role.read_only)
    db_session.flush()
    s, rot = _weekly(db_session, a, b)
    now = datetime.now(UTC)
    # a Wednesday-ish edit time, regardless of today's weekday
    wed = (now - timedelta(days=now.weekday())) + timedelta(days=2, hours=12)
    wed = wed.replace(minute=0, second=0, microsecond=0)
    before = oncall_user_ids(s, wed)
    cat.update_rotation(
        db_session,
        rot,
        user_ids=[a.id, b.id],
        shift_hours=168,
        handoff_local="09:00",
        windows=None,
        effective_from=wed,
        now=wed - timedelta(hours=1),
    )
    db_session.refresh(s)
    assert oncall_user_ids(s, wed) == before
    # still the same user until Monday 09:00, then it changes exactly there
    next_monday = (wed - timedelta(days=2, hours=12)).replace(hour=9) + timedelta(days=7)
    assert oncall_user_ids(s, next_monday - timedelta(minutes=1)) == before
    assert oncall_user_ids(s, next_monday) != before
    assert rot.versions[-1].anchor == rot.versions[0].anchor


def test_changing_handoff_time_reanchors_on_effective_date(db_session):
    a = create_user(db_session, email="a2@x.io", name="A", role=Role.read_only)
    b = create_user(db_session, email="b2@x.io", name="B", role=Role.read_only)
    db_session.flush()
    s, rot = _weekly(db_session, a, b)
    eff = datetime.now(UTC) + timedelta(days=1)
    v = cat.update_rotation(
        db_session,
        rot,
        user_ids=[a.id, b.id],
        shift_hours=168,
        handoff_local="10:30",
        windows=None,
        effective_from=eff,
    )
    assert v.anchor.date() == eff.date() and (v.anchor.hour, v.anchor.minute) == (10, 30)


def test_past_effective_from_is_clamped_to_now_and_old_version_untouched(db_session):
    a = create_user(db_session, email="a3@x.io", name="A", role=Role.read_only)
    b = create_user(db_session, email="b3@x.io", name="B", role=Role.read_only)
    db_session.flush()
    s, rot = _weekly(db_session, a, b)
    first = rot.versions[0]
    snap = (first.effective_from, first.anchor, first.shift_hours, list(first.user_ids))
    now = datetime(2030, 1, 1, 12, tzinfo=UTC)
    v = cat.update_rotation(
        db_session,
        rot,
        user_ids=[b.id],
        shift_hours=168,
        handoff_local="09:00",
        windows=None,
        effective_from=now - timedelta(days=10),
        now=now,
    )
    assert v.effective_from == now
    assert (first.effective_from, first.anchor, first.shift_hours, list(first.user_ids)) == snap
