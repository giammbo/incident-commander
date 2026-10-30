"""On-call UX fix wave: preview robustness, paging wording, labels, coverage, local times."""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.models import EscalationStatus, Incident, NotificationJob, Urgency
from app.services import oncall_catalog as cat
from app.services import oncall_engine as eng
from tests.test_oncall_ux_p2 import _login, _user


@pytest.fixture
def client(db_session, get_db_override):
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override
    return TestClient(app)


def _esc(db_session, u, status=None):
    now = datetime.now(UTC)
    esc = eng.start_escalation(
        db_session, urgency=Urgency.high, title="DB down", user_ids=[u.id], source="manual", now=now
    )
    db_session.add(
        NotificationJob(
            escalation_id=esc.id, kind="notify", level=0, run_at=now, user_id=u.id, channel="sms"
        )
    )
    if status:
        esc.status = status
    db_session.commit()
    return esc


def test_preview_malformed_input_returns_note(client, db_session):
    _login(client, db_session)
    r = client.get("/oncall/page/preview", params={"target_kind": "users", "user_ids": "abc"})
    assert r.status_code == 200 and "Choose at least one person." in r.text
    r = client.get(
        "/oncall/page/preview", params={"target_kind": "users", "user_ids": "99999999999"}
    )
    assert r.status_code == 200 and "Choose at least one person." in r.text
    for pid in ("99999999999", "abc", "-1"):
        r = client.get("/oncall/page/preview", params={"target_kind": "path", "path_id": pid})
        assert r.status_code == 200 and "Choose an escalation path." in r.text


def test_page_form_preview_includes_only_target_fields(client, db_session):
    _login(client, db_session)
    t = client.get("/oncall/page").text
    m = re.search(r'hx-include="([^"]*)"', t)
    assert m and "page-form" not in m.group(1) and "message" not in m.group(1)
    assert "[name=user_ids]" in m.group(1)


def test_paging_wording_by_status(client, db_session):
    _login(client, db_session)
    u = _user(db_session)
    esc = _esc(db_session, u)
    assert "Paging: Una" in client.get("/oncall").text
    assert "Paging: Una" in client.get(f"/oncall/escalations/{esc.id}").text
    esc.status = EscalationStatus.acknowledged
    db_session.commit()
    t = client.get("/oncall").text
    assert "Paged: Una" in t and "Paging: Una" not in t
    assert "Paged: Una" in client.get(f"/oncall/escalations/{esc.id}").text
    esc.status = EscalationStatus.exhausted
    esc.direct_user_ids = []
    db_session.execute(NotificationJob.__table__.delete())
    db_session.commit()
    d = client.get(f"/oncall/escalations/{esc.id}").text
    assert "Stopped paging" in d and "Nobody paged yet" not in d
    assert "Nobody paged yet" not in client.get("/oncall").text


def test_detail_notified_table_uses_channel_labels(client, db_session):
    _login(client, db_session)
    esc = _esc(db_session, _user(db_session))
    t = client.get(f"/oncall/escalations/{esc.id}").text
    assert "<td>SMS</td>" in t or "<td>Sms</td>" in t or "<td>Text message" in t
    assert "<td>sms</td>" not in t


def test_empty_rotation_says_no_people_not_never_on_call(client, db_session):
    _login(client, db_session)
    u = _user(db_session)
    s = cat.create_schedule(db_session, name="Primary", timezone="Europe/Rome")
    start = (datetime.now(UTC) - timedelta(days=3)).date()
    cat.add_rotation(
        db_session, s, name="Weekly", user_ids=[u.id], shift_hours=24,
        handoff_local="09:00", start_date=start,
    )  # fmt: skip
    empty = cat.add_rotation(
        db_session, s, name="Empty", user_ids=[u.id], shift_hours=24,
        handoff_local="09:00", start_date=start,
    )  # fmt: skip
    for v in empty.versions:
        v.user_ids = []
    db_session.commit()
    t = client.get(f"/oncall/schedules/{s.id}").text
    assert "No people in this rotation." in t and "Never on call" not in t


def test_incident_page_uses_time_tags(client, db_session):
    _login(client, db_session)
    u = _user(db_session)
    inc = Incident(title="X", created_by=u.id, closed_at=datetime.now(UTC))
    db_session.add(inc)
    db_session.commit()
    t = client.get(f"/incidents/{inc.id}").text
    dd = re.search(r"<dt>Opened</dt><dd>(.*?)</dd>", t, re.S).group(1)
    assert "<time datetime=" in dd
    assert re.search(r"<dt>Closed</dt><dd><time datetime=", t)


def test_mobile_css_caps_main_width():
    css = Path("app/static/app.css").read_text()
    block = css[css.index("@media (max-width: 720px)") :]
    assert ".main { width: 100%; }" in block.split("}\n}")[0] + "}"
