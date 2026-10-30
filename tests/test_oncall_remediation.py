"""Behavior regressions for on-call editing, safe preview and private paging."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.db import get_db
from app.main import create_app
from app.models import (
    Alert,
    AlertRoute,
    Escalation,
    EscalationLevel,
    EscalationPath,
    Group,
    IncidentRoleAssignment,
    IncidentRoleType,
    NotificationJob,
    NotificationRule,
    Role,
    SeverityLevel,
    Urgency,
    User,
    UserGroup,
    effective_role,
)
from app.services import oncall_catalog as cat
from app.services import oncall_engine as engine
from app.services import oncall_notify, statuses
from app.services.incidents import create_incident
from app.services.oncall_schedule import oncall_user_ids
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session, get_db_override):
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override
    return TestClient(app)


def setup(db):
    bootstrap_admin(db, "admin@localhost")
    statuses.seed_status_levels(db)
    sev = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db.add(sev)
    creator = create_user(
        db, email="creator@x.io", name="Creator", role=Role.incident_commander, password="pw-123456"
    )
    other = create_user(
        db,
        email="other@x.io",
        name="Other responder",
        role=Role.incident_commander,
        password="pw-123456",
    )
    member = create_user(
        db, email="member@x.io", name="Allowed responder", role=Role.read_only, password="pw-123456"
    )
    db.flush()
    path = EscalationPath(
        name="Payments",
        levels=[
            EscalationLevel(
                rank=1, user_ids=[other.id, member.id], schedule_ids=[], ack_timeout_minutes=5
            )
        ],
    )
    db.add(path)
    inc = create_incident(
        db,
        title="Private acquisition outage",
        severity_level_id=sev.id,
        is_private=True,
        created_by=creator.id,
    )
    role = IncidentRoleType(label="Responder")
    db.add(role)
    db.flush()
    assignment = IncidentRoleAssignment(incident_id=inc.id, role_type_id=role.id, user_id=member.id)
    db.add(assignment)
    db.commit()
    return creator, other, member, path, inc, assignment


def login(client, email="creator@x.io"):
    client.post("/login", data={"email": email, "password": "pw-123456"})


def test_inactive_high_condition_does_not_block_route(client, db_session):
    _, _, _, path, _, _ = setup(db_session)
    login(client)
    response = client.post(
        "/oncall/routes",
        data={
            "name": "Safe route",
            "escalation_path_id": path.id,
            "urgency_mode": "always_high",
            "hi-0-field": "title",
            "hi-0-value": "",
        },
    )
    assert response.status_code == 200
    assert db_session.scalars(select(AlertRoute)).one().high_if == []
    # Initial HTML disables the entire inactive branch; its change handler keeps it in sync.
    assert re.search(r"<fieldset[^>]*data-high-rows[^>]*disabled", response.text)


def test_future_rotation_keeps_current_summary_and_effective_date(client, db_session):
    creator, other, member, _, _, _ = setup(db_session)
    login(client)
    now = datetime.now(UTC)
    s = cat.create_schedule(db_session, name="Primary", timezone="UTC")
    rot = cat.add_rotation(
        db_session,
        s,
        name="Daily",
        user_ids=[creator.id, other.id],
        shift_hours=24,
        handoff_local="09:00",
        start_date=(now - timedelta(days=3)).date(),
    )
    eff = (now + timedelta(days=7)).replace(second=0, microsecond=0)
    cat.update_rotation(
        db_session,
        rot,
        user_ids=[member.id],
        shift_hours=48,
        handoff_local="09:00",
        windows=[],
        effective_from=eff,
    )
    db_session.commit()
    page = client.get(f"/oncall/schedules/{s.id}")
    assert 'data-version-state="current"' in page.text
    assert 'data-version-state="planned"' in page.text
    current = page.text.split('data-version-state="current"', 1)[1].split(
        'data-version-state="planned"', 1
    )[0]
    assert "24h shifts" in current and "Other responder" in current
    assert f'value="{eff.strftime("%Y-%m-%dT%H:%M")}"' in page.text
    before = oncall_user_ids(s, now)
    edit = client.post(
        f"/oncall/rotations/{rot.id}/edit",
        data={
            "version_id": rot.versions[-1].id,
            "user_ids": [other.id],
            "shift_hours": 48,
            "handoff_local": "09:00",
            "effective_from": eff.isoformat(),
        },
    )
    assert edit.status_code == 200
    db_session.refresh(s)
    assert oncall_user_ids(s, now) == before
    assert oncall_user_ids(s, eff + timedelta(hours=1)) == [other.id]


def test_invalid_override_retains_values_and_opens_form(client, db_session):
    creator, _, _, _, _, _ = setup(db_session)
    login(client)
    s = cat.create_schedule(db_session, name="Primary", timezone="UTC")
    db_session.commit()
    response = client.post(
        f"/oncall/schedules/{s.id}/overrides",
        data={
            "user_id": creator.id,
            "starts_at": "2030-01-02T18:00",
            "ends_at": "2030-01-02T09:00",
        },
    )
    assert response.status_code == 422
    assert 'value="2030-01-02T18:00"' in response.text
    assert 'value="2030-01-02T09:00"' in response.text
    assert re.search(r'<details[^>]*id="add-override"[^>]*open', response.text)
    assert 'role="alert"' in response.text


def test_invalid_page_retains_message_path_and_urgency_without_cookie_draft(client, db_session):
    setup(db_session)
    empty = EscalationPath(name="Draft empty path")
    db_session.add(empty)
    db_session.commit()
    login(client)
    response = client.post(
        "/oncall/page",
        data={
            "target_kind": "path",
            "path_id": empty.id,
            "urgency": "low",
            "message": "Keep this operational message",
        },
    )
    assert response.status_code == 422
    assert "Keep this operational message</textarea>" in response.text
    assert re.search(rf'<option value="{empty.id}" selected', response.text)
    assert '<option value="low" selected' in response.text
    assert not response.headers.get("set-cookie")
    assert db_session.scalars(select(Escalation)).all() == []


def test_invalid_route_retains_raw_conditions(client, db_session):
    _, _, _, path, _, _ = setup(db_session)
    login(client)
    response = client.post(
        "/oncall/routes",
        data={
            "name": "Critical draft",
            "escalation_path_id": path.id,
            "urgency_mode": "high_when",
            "cond-0-field": "source",
            "cond-0-value": "prometheus",
            "hi-0-field": "title",
            "hi-0-op": "contains",
            "hi-0-value": "",
        },
    )
    assert response.status_code == 422
    assert 'value="Critical draft"' in response.text
    assert 'value="prometheus"' in response.text
    assert 'value="high_when" checked' in response.text
    assert re.search(r'name="hi-0-field"[\s\S]*?<option value="title" selected', response.text)


@pytest.mark.parametrize(
    "sample_title,severity,expected_route,expected_urgency",
    [
        ("Checkout down", "critical", "Critical", "High"),
        ("Database down", "critical", "Critical", "Low"),
        ("Minor warning", "warning", "Fallback", "Low"),
    ],
)
def test_route_preview_uses_first_enabled_match_without_creating_jobs(
    client, db_session, sample_title, severity, expected_route, expected_urgency
):
    _, _, _, path, _, _ = setup(db_session)
    login(client)
    db_session.add_all(
        [
            AlertRoute(
                name="Disabled critical",
                enabled=False,
                rank=1,
                conditions=[],
                escalation_path_id=path.id,
                urgency=Urgency.high,
            ),
            AlertRoute(
                name="Critical",
                rank=2,
                conditions=[{"field": "severity_raw", "op": "equals", "value": "critical"}],
                high_if=[{"field": "title", "op": "contains", "value": "checkout"}],
                escalation_path_id=path.id,
                urgency=Urgency.high,
            ),
            AlertRoute(
                name="Fallback",
                rank=3,
                conditions=[],
                escalation_path_id=path.id,
                urgency=Urgency.low,
            ),
        ]
    )
    db_session.commit()
    response = client.post(
        "/oncall/routes/preview",
        data={"title": sample_title, "source": "prometheus", "severity_raw": severity},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert (
        expected_route in response.text
        and "Payments" in response.text
        and expected_urgency in response.text
    )
    assert "Disabled critical" not in response.text
    assert "No notifications were sent" in response.text
    assert db_session.scalars(select(Escalation)).all() == []
    assert db_session.scalars(select(NotificationJob)).all() == []


@pytest.mark.parametrize("indirect", [False, True])
def test_private_escalation_hidden_on_lists_detail_actions_and_page(client, db_session, indirect):
    creator, other, member, _, inc, _ = setup(db_session)
    alert = None
    if indirect:
        alert = Alert(
            title="Private acquisition alert", source="api", dedup_key="private", incident_id=inc.id
        )
        db_session.add(alert)
        db_session.flush()
    esc = engine.start_escalation(
        db_session,
        urgency=Urgency.high,
        title=inc.title,
        incident=None if indirect else inc,
        alert=alert,
        user_ids=[other.id],
        source="manual",
        now=datetime.now(UTC),
    )
    # Having a queued page is explicitly not an access grant.
    db_session.add(
        NotificationJob(
            escalation_id=esc.id,
            kind="notify",
            user_id=other.id,
            level=0,
            channel="email",
            run_at=datetime.now(UTC),
        )
    )
    db_session.commit()
    login(client, "other@x.io")
    for url in ("/oncall", "/oncall/escalations"):
        page = client.get(url)
        assert inc.title not in page.text
        assert f'/oncall/escalations/{esc.id}"' not in page.text
    for url in (
        f"/oncall/escalations/{esc.id}",
        f"/oncall/page?incident_id={inc.id}",
        f"/oncall/page/preview?incident_id={inc.id}",
    ):
        assert client.get(url).status_code == 404
    for action in ("ack", "resolve", "escalate", "declare"):
        assert client.post(f"/oncall/escalations/{esc.id}/{action}").status_code == 404
    assert (
        client.post(
            "/oncall/page",
            data={"incident_id": inc.id, "target_kind": "users", "user_ids": [other.id]},
        ).status_code
        == 404
    )
    login(client, "member@x.io")
    assert client.get(f"/oncall/escalations/{esc.id}").status_code == 200


def test_private_page_rejects_unassigned_recipient_and_preview_explains_policy(client, db_session):
    _, other, member, path, inc, _ = setup(db_session)
    login(client)
    form = client.get(f"/oncall/page?incident_id={inc.id}")
    assert (
        f'value="{other.id}"' not in form.text.split('id="pf-users"')[1].split('id="pf-preview"')[0]
    )
    preview = client.get(
        f"/oncall/page/preview?incident_id={inc.id}&target_kind=path&path_id={path.id}"
    )
    assert "Allowed responder" in preview.text
    assert "Other responder" not in preview.text
    assert "Assign an incident role" in preview.text
    response = client.post(
        "/oncall/page",
        data={
            "incident_id": inc.id,
            "target_kind": "users",
            "user_ids": [other.id],
            "message": "Private details",
        },
    )
    assert response.status_code == 422
    assert "Assign an incident role" in response.text
    assert db_session.scalars(select(Escalation)).all() == []


def test_private_queued_page_rechecks_membership_before_delivery(db_session, monkeypatch):
    _, other, member, path, inc, assignment = setup(db_session)
    sent = []

    class FakeChannel:
        key = "email"
        label = "Email"

        def available(self, db):
            return True

        def reachable(self, db, user):
            return True

        def send(self, db, *, user, escalation, ack_url):
            sent.append((user.id, escalation.title))
            return "fake"

    monkeypatch.setattr(oncall_notify, "CHANNELS", {"email": FakeChannel()})
    for user in (other, member):
        db_session.add(
            NotificationRule(
                user_id=user.id, urgency=Urgency.high, channel="email", delay_minutes=0
            )
        )
    now = datetime.now(UTC)
    engine.start_escalation(
        db_session,
        urgency=Urgency.high,
        title=inc.title,
        path=path,
        incident=inc,
        source="manual",
        now=now,
    )
    db_session.commit()
    engine.tick(db_session, now=now, base_url="http://testserver", limit=1)
    notify = list(
        db_session.scalars(select(NotificationJob).where(NotificationJob.kind == "notify"))
    )
    assert [j.user_id for j in notify] == [member.id]
    db_session.delete(assignment)
    db_session.commit()
    engine.tick(db_session, now=now, base_url="http://testserver")
    assert sent == []
    db_session.refresh(notify[0])
    assert notify[0].status == "skipped" and "access" in notify[0].detail


@pytest.mark.parametrize("channel_key", ["email", "slack", "push", "sms", "voice"])
def test_channel_refuses_private_content_after_membership_revoked(db_session, channel_key):
    _, _, member, _, inc, assignment = setup(db_session)
    esc = engine.start_escalation(
        db_session,
        urgency=Urgency.high,
        title=inc.title,
        incident=inc,
        user_ids=[member.id],
        source="manual",
        now=datetime.now(UTC),
    )
    db_session.commit()
    db_session.delete(assignment)
    db_session.commit()
    with pytest.raises(PermissionError, match="access"):
        oncall_notify.CHANNELS[channel_key].send(
            db_session, user=member, escalation=esc, ack_url="http://testserver/ack"
        )


def test_old_signed_ack_token_cannot_reveal_private_page_after_role_removed(client, db_session):
    _, _, member, _, inc, assignment = setup(db_session)
    esc = engine.start_escalation(
        db_session,
        urgency=Urgency.high,
        title=inc.title,
        incident=inc,
        user_ids=[member.id],
        source="manual",
        now=datetime.now(UTC),
    )
    db_session.add(
        NotificationJob(
            escalation_id=esc.id,
            kind="notify",
            user_id=member.id,
            level=0,
            channel="email",
            run_at=datetime.now(UTC),
        )
    )
    db_session.commit()
    token = oncall_notify.make_ack_token(esc.id, member.id)
    assert inc.title in client.get(f"/oncall/ack/{token}").text
    db_session.delete(assignment)
    db_session.commit()
    for method in (client.get, client.post):
        response = method(f"/oncall/ack/{token}")
        assert response.status_code in (403, 404)
        assert inc.title not in response.text
        assert "Acknowledge</button>" not in response.text


@pytest.mark.parametrize("endpoint,params", [("voice", {}), ("gather", {"Digits": "2"})])
def test_signed_voice_callback_cannot_read_private_page_without_access(
    client, db_session, monkeypatch, endpoint, params
):
    from app.routers import oncall_inbound

    _, other, _, _, inc, _ = setup(db_session)
    esc = engine.start_escalation(
        db_session,
        urgency=Urgency.high,
        title=inc.title,
        incident=inc,
        user_ids=[other.id],
        source="manual",
        now=datetime.now(UTC),
    )
    db_session.commit()
    monkeypatch.setattr(oncall_inbound, "_twilio_ok", lambda *_: True)
    response = client.post(f"/oncall/twilio/{endpoint}/{esc.id}/{other.id}", data=params)
    assert inc.title not in response.text
    assert "can't" in response.text or response.status_code in (403, 404)


def test_divergent_incident_links_require_access_to_both(client, db_session):
    creator, other, member, _, inc, _ = setup(db_session)
    second = create_incident(
        db_session,
        title="Other restricted case",
        severity_level_id=inc.severity_level_id,
        is_private=True,
        created_by=other.id,
    )
    alert = Alert(
        title="Restricted linked alert",
        source="api",
        dedup_key="second-private",
        incident_id=second.id,
    )
    db_session.add(alert)
    db_session.flush()
    esc = engine.start_escalation(
        db_session,
        urgency=Urgency.high,
        title=inc.title,
        incident=inc,
        alert=alert,
        user_ids=[member.id],
        source="manual",
        now=datetime.now(UTC),
    )
    db_session.commit()
    login(client, "member@x.io")
    assert client.get(f"/oncall/escalations/{esc.id}").status_code == 404
    assert inc.title not in client.get("/oncall/escalations").text
    assert not oncall_notify.can_receive_escalation(db_session, member, esc)
    login(client)
    # Even the direct incident's creator cannot view the different alert incident.
    assert client.get(f"/oncall/escalations/{esc.id}").status_code == 404


def test_sms_lookup_excludes_revoked_private_page(db_session):
    from app.models import EscalationStatus
    from app.routers import oncall_inbound

    _, _, member, _, inc, assignment = setup(db_session)
    esc = engine.start_escalation(
        db_session,
        urgency=Urgency.high,
        title=inc.title,
        incident=inc,
        user_ids=[member.id],
        source="manual",
        now=datetime.now(UTC),
    )
    db_session.add(
        NotificationJob(
            escalation_id=esc.id,
            kind="notify",
            user_id=member.id,
            level=0,
            channel="sms",
            run_at=datetime.now(UTC),
        )
    )
    db_session.commit()
    assert oncall_inbound._paged_escalations(
        db_session, [member.id], [EscalationStatus.triggered]
    ) == [esc]
    db_session.delete(assignment)
    db_session.commit()
    assert (
        oncall_inbound._paged_escalations(db_session, [member.id], [EscalationStatus.triggered])
        == []
    )


def test_invalid_rotation_and_level_preserve_selection_and_open_editor(client, db_session):
    creator, other, _, path, _, _ = setup(db_session)
    login(client)
    s = cat.create_schedule(db_session, name="Primary", timezone="UTC")
    db_session.commit()
    response = client.post(
        f"/oncall/schedules/{s.id}/rotations",
        data={
            "name": "Keep rotation name",
            "user_ids": [other.id, creator.id],
            "shift_hours": "0",
            "handoff_local": "11:00",
            "start_date": "2030-01-02",
            "win_days": [1, 3],
            "win_start": "09:00",
            "win_end": "17:00",
        },
    )
    assert response.status_code == 422
    assert 'value="Keep rotation name"' in response.text
    assert 'value="11:00"' in response.text
    assert re.search(rf'<option value="{other.id}" selected', response.text)
    assert re.search(r'<details[^>]*id="add-rotation"[^>]*open', response.text)
    response = client.post(
        f"/oncall/levels/{path.levels[0].id}/edit",
        data={"user_ids": [other.id], "ack_timeout_minutes": "0"},
    )
    assert response.status_code == 422
    assert re.search(rf'type="checkbox" name="user_ids" value="{other.id}" checked', response.text)
    assert "<details open><summary>Edit level" in response.text
    assert '<select name="user_ids" multiple' not in response.text


def test_membership_removed_during_channel_probe_skips_delivery(db_session, monkeypatch):
    _, _, member, _, inc, assignment = setup(db_session)
    channel = oncall_notify.EmailChannel()
    monkeypatch.setattr(channel, "available", lambda db: True)
    monkeypatch.setattr(oncall_notify, "CHANNELS", {"email": channel})
    db_session.add(
        NotificationRule(user_id=member.id, urgency=Urgency.high, channel="email", delay_minutes=0)
    )
    now = datetime.now(UTC)
    engine.start_escalation(
        db_session,
        urgency=Urgency.high,
        title=inc.title,
        incident=inc,
        user_ids=[member.id],
        source="manual",
        now=now,
    )
    db_session.commit()
    engine.tick(db_session, now=now, base_url="http://testserver", limit=1)
    sent = []
    monkeypatch.setattr(oncall_notify, "send_email", lambda *args, **kwargs: sent.append(kwargs))

    def remove_membership(db, user):
        db.delete(assignment)
        db.flush()
        return True

    monkeypatch.setattr(channel, "reachable", remove_membership)
    engine.tick(db_session, now=now, base_url="http://testserver")
    assert sent == []
    job = db_session.scalar(select(NotificationJob).where(NotificationJob.kind == "notify"))
    assert job.status == "skipped" and "access" in job.detail


def _recipient_for_concurrent_revocation(db, revocation):
    _, other, member, _, inc, _ = setup(db)
    if revocation == "deactivate":
        recipient = member  # Read-only incident assignees must remain eligible while active.
    else:
        recipient = other  # No creator/assignment access: eligibility comes only from admin.
        recipient.groups = [Group(name="Temporary paging admins", role=Role.admin)]
    db.commit()
    # Populate both the membership collection and the Group identity before the other commit.
    assert recipient.is_active
    assert effective_role(recipient) == (
        Role.read_only if revocation == "deactivate" else Role.admin
    )
    return recipient, inc


def _revoke_recipient_in_other_session(pg_engine, recipient, revocation):
    with Session(pg_engine) as writer:
        if revocation == "deactivate":
            writer.execute(update(User).where(User.id == recipient.id).values(is_active=False))
        elif revocation == "remove_admin_membership":
            writer.execute(delete(UserGroup).where(UserGroup.user_id == recipient.id))
        else:
            writer.execute(
                update(Group).where(Group.id == recipient.groups[0].id).values(role=Role.read_only)
            )
        writer.commit()


@pytest.mark.parametrize(
    "revocation", ["deactivate", "remove_admin_membership", "demote_admin_group"]
)
def test_concurrent_recipient_revocation_during_probe_skips_private_email(
    db_session, pg_engine, monkeypatch, revocation
):
    recipient, inc = _recipient_for_concurrent_revocation(db_session, revocation)
    channel = oncall_notify.EmailChannel()
    monkeypatch.setattr(channel, "available", lambda db: True)
    monkeypatch.setattr(oncall_notify, "CHANNELS", {"email": channel})
    db_session.add(
        NotificationRule(
            user_id=recipient.id, urgency=Urgency.high, channel="email", delay_minutes=0
        )
    )
    now = datetime.now(UTC)
    engine.start_escalation(
        db_session,
        urgency=Urgency.high,
        title=inc.title,
        incident=inc,
        user_ids=[recipient.id],
        source="manual",
        now=now,
    )
    db_session.commit()
    engine.tick(db_session, now=now, base_url="http://testserver", limit=1)
    job = db_session.scalar(select(NotificationJob).where(NotificationJob.kind == "notify"))
    assert job is not None and job.status == "pending"
    sent, probes = [], []
    monkeypatch.setattr(oncall_notify, "send_email", lambda *args, **kwargs: sent.append(kwargs))

    def revoke_during_probe(db, user):
        assert user.is_active
        assert effective_role(user) == (
            Role.read_only if revocation == "deactivate" else Role.admin
        )
        _revoke_recipient_in_other_session(pg_engine, user, revocation)
        probes.append(user.id)
        return True

    monkeypatch.setattr(channel, "reachable", revoke_during_probe)
    engine.tick(db_session, now=now, base_url="http://testserver")
    assert probes == [recipient.id]
    assert sent == []
    db_session.refresh(job)
    assert job.status == "skipped" and "access" in job.detail


@pytest.mark.parametrize("channel_key", ["email", "slack", "push", "sms", "voice"])
@pytest.mark.parametrize(
    "revocation", ["deactivate", "remove_admin_membership", "demote_admin_group"]
)
def test_direct_channel_refreshes_recipient_after_other_session_revokes_access(
    db_session, pg_engine, monkeypatch, channel_key, revocation
):
    recipient, inc = _recipient_for_concurrent_revocation(db_session, revocation)
    esc = engine.start_escalation(
        db_session,
        urgency=Urgency.high,
        title=inc.title,
        incident=inc,
        user_ids=[recipient.id],
        source="manual",
        now=datetime.now(UTC),
    )
    db_session.commit()
    assert oncall_notify.can_receive_escalation(db_session, recipient, esc)
    _revoke_recipient_in_other_session(pg_engine, recipient, revocation)
    sent = []

    def capture(*args, **kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(oncall_notify, "send_email", capture)
    monkeypatch.setattr(oncall_notify, "WebClient", capture)
    monkeypatch.setattr(oncall_notify.pywebpush, "webpush", capture)
    monkeypatch.setattr(oncall_notify.oncall_twilio, "send_sms", capture)
    monkeypatch.setattr(oncall_notify.oncall_twilio, "place_call", capture)
    with pytest.raises(PermissionError, match="access"):
        oncall_notify.CHANNELS[channel_key].send(
            db_session, user=recipient, escalation=esc, ack_url="http://testserver/ack"
        )
    assert sent == []


def test_read_only_assignee_keeps_both_private_links_after_group_membership_removed(
    db_session, pg_engine, monkeypatch
):
    _, other, member, _, inc, assignment = setup(db_session)
    second = create_incident(
        db_session,
        title="Second private linked incident",
        severity_level_id=inc.severity_level_id,
        is_private=True,
        created_by=other.id,
    )
    second_assignment = IncidentRoleAssignment(
        incident_id=second.id, role_type_id=assignment.role_type_id, user_id=member.id
    )
    alert = Alert(
        title="Linked alert", source="api", dedup_key="both-private", incident_id=second.id
    )
    db_session.add_all([second_assignment, alert])
    db_session.flush()
    esc = engine.start_escalation(
        db_session,
        urgency=Urgency.high,
        title=inc.title,
        incident=inc,
        alert=alert,
        user_ids=[member.id],
        source="manual",
        now=datetime.now(UTC),
    )
    db_session.commit()
    assert effective_role(member) == Role.read_only
    assert oncall_notify.can_receive_escalation(db_session, member, esc)
    _revoke_recipient_in_other_session(pg_engine, member, "remove_admin_membership")
    sent = []
    monkeypatch.setattr(oncall_notify, "send_email", lambda *args, **kwargs: sent.append(kwargs))
    channel = oncall_notify.EmailChannel()
    channel.send(db_session, user=member, escalation=esc, ack_url="http://testserver/ack")
    assert len(sent) == 1 and sent[0]["to"] == "member@x.io"
    assert inc.title in sent[0]["subject"]
    # Keeping access to the direct incident is insufficient after losing the alert incident.
    with Session(pg_engine) as writer:
        writer.execute(
            delete(IncidentRoleAssignment).where(IncidentRoleAssignment.id == second_assignment.id)
        )
        writer.commit()
    with pytest.raises(PermissionError, match="access"):
        channel.send(db_session, user=member, escalation=esc, ack_url="http://testserver/ack")
    assert len(sent) == 1
