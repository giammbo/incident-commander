"""On-call UX package 2: coverage warning, route urgency control, page form, sub-nav."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import AlertRoute, Escalation, Role, Urgency
from app.services import oncall_catalog as cat
from app.services.oncall_schedule import rotation_coverage
from app.services.users import bootstrap_admin, create_user
from tests.test_oncall_schedule import _daily, _rome, _rot, _sched, _version


@pytest.fixture
def client(db_session, get_db_override):
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override
    return TestClient(app)


def _login(client, db_session, *, role="ic"):
    bootstrap_admin(db_session, "admin@localhost")
    r = {"ic": Role.incident_commander, "ro": Role.read_only}[role]
    create_user(db_session, email="u@x.io", name="Una", role=r, password="pw-123456")
    db_session.commit()
    client.post("/login", data={"email": "u@x.io", "password": "pw-123456"})


def _user(db_session):
    from app.models import User

    return db_session.scalar(select(User).where(User.email == "u@x.io"))


# --- item 6: coverage ------------------------------------------------------


def test_rotation_coverage_flags_masked_rotation():
    always = _rot(1, 1, _version([1, 2], _rome(2026, 11, 2, 9)))
    office = _rot(
        2,
        2,
        _version(
            [3],
            _rome(2026, 11, 2, 9),
            windows=[{"days": [0, 1, 2, 3, 4], "start": "09:00", "end": "18:00"}],
        ),
    )
    s = _sched(always, office)
    cov = rotation_coverage(s, _rome(2026, 11, 3, 0), _rome(2026, 11, 17, 0))
    assert cov[2] == timedelta(0)
    assert cov[1] == timedelta(days=14)


def test_rotation_coverage_windowed_rotation_on_top_shares_time():
    office = _rot(
        1,
        1,
        _version(
            [3],
            _rome(2026, 11, 2, 9),
            windows=[{"days": [0, 1, 2, 3, 4], "start": "09:00", "end": "18:00"}],
        ),
    )
    always = _rot(2, 2, _version([1], _rome(2026, 11, 2, 9)))
    s = _sched(office, always)
    cov = rotation_coverage(s, _rome(2026, 11, 2, 0), _rome(2026, 11, 3, 0))  # a Monday
    assert cov[1] == timedelta(hours=9)
    assert cov[2] == timedelta(hours=15)


def test_rotation_coverage_ignores_overrides():
    s = _daily()
    cov = rotation_coverage(s, _rome(2026, 11, 3, 0), _rome(2026, 11, 4, 0))
    assert cov[1] == timedelta(days=1)


def test_schedule_detail_warns_about_never_on_call_rotation(client, db_session):
    _login(client, db_session)
    u = _user(db_session)
    s = cat.create_schedule(db_session, name="Primary", timezone="Europe/Rome")
    start = (datetime.now(UTC) - timedelta(days=3)).date()
    cat.add_rotation(
        db_session, s, name="Weekly", user_ids=[u.id], shift_hours=24,
        handoff_local="09:00", start_date=start,
    )  # fmt: skip
    cat.add_rotation(
        db_session, s, name="Office hours", user_ids=[u.id], shift_hours=24,
        handoff_local="09:00", start_date=start,
        windows=[{"days": [0, 1, 2, 3, 4], "start": "09:00", "end": "18:00"}],
    )  # fmt: skip
    db_session.commit()
    t = client.get(f"/oncall/schedules/{s.id}").text
    assert (
        "Never on call in the next 14 days: Weekly always covers. "
        "Move it up or add a time window to the rotation above." in t
    )
    assert t.count("Never on call") == 1


def test_schedule_detail_no_warning_when_every_rotation_is_used(client, db_session):
    _login(client, db_session)
    u = _user(db_session)
    s = cat.create_schedule(db_session, name="Primary", timezone="Europe/Rome")
    start = (datetime.now(UTC) - timedelta(days=3)).date()
    cat.add_rotation(
        db_session, s, name="Weekly", user_ids=[u.id], shift_hours=24,
        handoff_local="09:00", start_date=start,
    )  # fmt: skip
    db_session.commit()
    assert "Never on call" not in client.get(f"/oncall/schedules/{s.id}").text


# --- items 8 + 9: route form -----------------------------------------------


def _path_with_level(db_session, name="Payments path"):
    u = _user(db_session)
    p = cat.create_path(db_session, name=name)
    cat.add_level(db_session, p, schedule_ids=[], user_ids=[u.id], ack_timeout_minutes=5)
    db_session.commit()
    return p


def _post_route(client, path, **extra):
    data = {"name": "R", "escalation_path_id": str(path.id), **extra}
    return client.post("/oncall/routes", data=data, follow_redirects=False)


def test_route_urgency_modes_store_urgency_and_high_if(client, db_session):
    _login(client, db_session)
    p = _path_with_level(db_session)
    hi = {"hi-0-field": "severity_raw", "hi-0-op": "equals", "hi-0-value": "critical"}
    assert _post_route(client, p, urgency_mode="always_high", **hi).status_code == 303
    assert _post_route(client, p, urgency_mode="always_low", **hi).status_code == 303
    assert _post_route(client, p, urgency_mode="high_when", **hi).status_code == 303
    rows = list(db_session.scalars(select(AlertRoute).order_by(AlertRoute.id)))
    assert [(r.urgency, bool(r.high_if)) for r in rows] == [
        (Urgency.high, False),
        (Urgency.low, False),
        (Urgency.high, True),
    ]


def test_route_high_when_requires_a_condition_and_bad_mode_rejected(client, db_session):
    _login(client, db_session)
    p = _path_with_level(db_session)
    for mode in ("high_when", "bogus"):
        r = client.post(
            "/oncall/routes",
            data={"name": "R", "escalation_path_id": str(p.id), "urgency_mode": mode},
            follow_redirects=True,
        )
        assert r.status_code == 422 and "Check the form" in r.text
    assert db_session.scalar(select(AlertRoute)) is None


def test_route_edit_form_preselects_mode_and_clears_high_if(client, db_session):
    _login(client, db_session)
    p = _path_with_level(db_session)
    r = AlertRoute(
        name="R",
        escalation_path_id=p.id,
        conditions=[],
        urgency=Urgency.high,
        high_if=[{"field": "severity_raw", "op": "equals", "value": "critical"}],
    )
    db_session.add(r)
    db_session.commit()
    page = client.get(f"/oncall/routes/{r.id}").text
    assert 'value="high_when" checked' in page
    client.post(
        f"/oncall/routes/{r.id}/edit",
        data={"name": "R", "escalation_path_id": str(p.id), "urgency_mode": "always_low"},
    )
    db_session.expire_all()
    r = db_session.get(AlertRoute, r.id)
    assert r.urgency == Urgency.low and r.high_if == []
    assert 'value="always_low" checked' in client.get(f"/oncall/routes/{r.id}").text


def test_route_form_path_default_empty_and_levelless_disabled(client, db_session):
    _login(client, db_session)
    _path_with_level(db_session, "Has levels")
    cat.create_path(db_session, name="Empty draft path")
    db_session.commit()
    t = client.get("/oncall/routes").text
    assert '<option value="">— choose —</option>' in t
    assert "Empty draft path (no levels)" in t
    assert "disabled>Empty draft path (no levels)" in t
    assert "Has levels</option>" in t and "selected>Has levels" not in t


# --- item 10: page someone -------------------------------------------------


def test_page_form_has_checkboxes_toggle_and_preview_hook(client, db_session):
    _login(client, db_session)
    t = client.get("/oncall/page").text
    assert 'type="checkbox" name="user_ids"' in t
    assert '<select name="user_ids"' not in t
    assert 'id="pf-path"' in t and 'id="pf-users"' in t
    assert 'hx-get="/oncall/page/preview"' in t


def test_page_preview_path_users_and_edge_cases(client, db_session):
    _login(client, db_session)
    p = _path_with_level(db_session)
    empty = cat.create_path(db_session, name="Empty")
    db_session.commit()
    u = _user(db_session)
    r = client.get("/oncall/page/preview", params={"target_kind": "path", "path_id": p.id})
    assert "Will page now:" in r.text and "Una" in r.text
    r = client.get("/oncall/page/preview", params={"target_kind": "users", "user_ids": [u.id]})
    assert "Will page now:" in r.text and "Una" in r.text
    r = client.get("/oncall/page/preview", params={"target_kind": "path", "path_id": empty.id})
    assert "no levels" in r.text and "Will page now" not in r.text
    r = client.get("/oncall/page/preview", params={"target_kind": "path"})
    assert "Choose an escalation path" in r.text
    r = client.get("/oncall/page/preview", params={"target_kind": "users"})
    assert "Choose at least one person" in r.text


def test_page_preview_is_ic_only(client, db_session):
    _login(client, db_session, role="ro")
    assert client.get("/oncall/page/preview").status_code == 403


def test_page_submit_validation_unchanged(client, db_session):
    _login(client, db_session)
    r = client.post(
        "/oncall/page", data={"target_kind": "users", "urgency": "high"}, follow_redirects=True
    )
    assert "Choose at least one person" in r.text
    assert db_session.scalar(select(Escalation)) is None


# --- item 12: sub-navigation -----------------------------------------------


def test_subnav_on_every_oncall_page_with_active_tab(client, db_session):
    _login(client, db_session)
    p = _path_with_level(db_session)
    s = cat.create_schedule(db_session, name="Primary", timezone="UTC")
    r = AlertRoute(name="R", escalation_path_id=p.id, conditions=[])
    db_session.add(r)
    db_session.commit()
    pages = [
        ("/oncall", "Now"),
        ("/oncall/schedules", "Schedules"),
        (f"/oncall/schedules/{s.id}", "Schedules"),
        ("/oncall/escalation-paths", "Escalation paths"),
        (f"/oncall/escalation-paths/{p.id}", "Escalation paths"),
        ("/oncall/routes", "Alert routes"),
        (f"/oncall/routes/{r.id}", "Alert routes"),
        ("/oncall/escalations", "Escalations"),
        ("/oncall/page", None),
    ]
    for url, active in pages:
        t = client.get(url).text
        assert 'class="oc-tabs"' in t, url
        assert t.count(" active") >= 0
        assert t.count('class="oc-tab active"') == (1 if active else 0), url
        if active:
            assert f'aria-current="page">{active}</a>' in t, url
        assert "Page someone" in t, url


def test_subnav_page_action_hidden_for_readonly(client, db_session):
    _login(client, db_session, role="ro")
    t = client.get("/oncall").text
    assert 'class="oc-tabs"' in t and 'class="btn btn-sm btn-primary oc-tab-action"' not in t
