from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.models import (
    Alert,
    Escalation,
    EscalationStatus,
    Incident,
    NotificationJob,
    Role,
    SeverityLevel,
    Urgency,
    Webhook,
)
from app.services import oncall_engine as eng
from app.services import statuses
from app.services.incidents import close_incident
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session, get_db_override):
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override
    return TestClient(app)


def _login(client, email):
    r = client.post("/login", data={"email": email, "password": "pw-123456"})
    assert r.status_code in (200, 303)


def _users(db_session):
    bootstrap_admin(db_session, "admin@localhost")
    statuses.seed_status_levels(db_session)
    db_session.add(SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True))
    ic = create_user(
        db_session, email="ic@x.io", name="Ic", role=Role.incident_commander, password="pw-123456"
    )
    ro = create_user(
        db_session, email="ro@x.io", name="Ro", role=Role.read_only, password="pw-123456"
    )
    other = create_user(
        db_session, email="other@x.io", name="Other", role=Role.read_only, password="pw-123456"
    )
    db_session.commit()
    return ic, ro, other


def _esc(db_session, ro, **kw):
    now = datetime.now(UTC)
    esc = eng.start_escalation(
        db_session,
        urgency=Urgency.high,
        title="DB down",
        user_ids=[ro.id],
        source=kw.pop("source", "manual"),
        now=now,
        **kw,
    )
    db_session.add(
        NotificationJob(
            escalation_id=esc.id,
            kind="notify",
            run_at=now,
            status="sent",
            user_id=ro.id,
            channel="email",
            level=0,
        )
    )
    db_session.commit()
    return esc


def test_notified_readonly_user_can_ack_but_not_declare(client, db_session):
    ic, ro, _ = _users(db_session)
    alert = Alert(source="x", dedup_key="k", title="DB down", status="firing")
    db_session.add(alert)
    db_session.flush()
    esc = _esc(db_session, ro, alert=alert)
    _login(client, "ro@x.io")
    r = client.post(f"/oncall/escalations/{esc.id}/ack", follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    assert db_session.get(Escalation, esc.id).status == EscalationStatus.acknowledged
    r = client.post(
        f"/oncall/escalations/{esc.id}/declare",
        data={"severity_level_id": db_session.query(SeverityLevel).first().id},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert client.get(f"/oncall/escalations/{esc.id}").status_code == 200


def test_unrelated_readonly_user_cannot_ack(client, db_session):
    ic, ro, other = _users(db_session)
    esc = _esc(db_session, ro)
    _login(client, "other@x.io")
    for action in ("ack", "resolve", "escalate"):
        r = client.post(f"/oncall/escalations/{esc.id}/{action}", follow_redirects=False)
        assert r.status_code == 403
    db_session.expire_all()
    assert db_session.get(Escalation, esc.id).status == EscalationStatus.triggered


def test_declare_from_escalation_links_incident_and_runs_side_effects(
    client, db_session, webhook_posts
):
    ic, ro, _ = _users(db_session)
    db_session.add(Webhook(name="W", url="https://hooks/x", format="slack", enabled=True))
    alert = Alert(source="x", dedup_key="k", title="DB down", status="firing")
    db_session.add(alert)
    db_session.flush()
    esc = _esc(db_session, ro, alert=alert)
    _login(client, "ic@x.io")
    r = client.post(
        f"/oncall/escalations/{esc.id}/declare",
        data={"severity_level_id": db_session.query(SeverityLevel).first().id},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db_session.expire_all()
    esc = db_session.get(Escalation, esc.id)
    inc = db_session.get(Incident, esc.incident_id)
    assert inc is not None and inc.title == "DB down"
    assert db_session.get(Alert, alert.id).incident_id == inc.id
    assert r.headers["location"] == f"/incidents/{inc.id}"
    assert webhook_posts
    # second declare is refused
    r = client.post(
        f"/oncall/escalations/{esc.id}/declare",
        data={"severity_level_id": db_session.query(SeverityLevel).first().id},
        follow_redirects=False,
    )
    assert r.status_code in (303, 400)
    assert db_session.query(Incident).count() == 1


def test_closing_incident_resolves_linked_manual_escalations(db_session):
    ic, ro, _ = _users(db_session)
    from app.services.incidents import create_incident

    inc = create_incident(
        db_session,
        title="T",
        severity_level_id=db_session.query(SeverityLevel).first().id,
        is_private=False,
        description=None,
        created_by=ic.id,
    )
    db_session.flush()
    esc = _esc(db_session, ro, incident=inc)
    close_incident(db_session, inc, ic.id)
    db_session.commit()
    db_session.expire_all()
    assert db_session.get(Escalation, esc.id).status == EscalationStatus.resolved


def test_detail_shows_failed_notification_reason(client, db_session):
    ic, ro, _ = _users(db_session)
    esc = _esc(db_session, ro)
    job = db_session.query(NotificationJob).filter_by(kind="notify").one()
    job.status, job.detail = "failed", "SMTP connection refused"
    db_session.commit()
    _login(client, "ic@x.io")
    r = client.get(f"/oncall/escalations/{esc.id}")
    assert r.status_code == 200
    assert "SMTP connection refused" in r.text
    assert "ro@x.io" not in r.text
    lst = client.get("/oncall/escalations")
    assert lst.status_code == 200 and "DB down" in lst.text
    assert "DB down" in client.get("/oncall").text


def test_declare_is_atomic_and_second_declare_is_refused(client, db_session, pg_engine):
    from sqlalchemy.orm import Session

    ic, ro, _ = _users(db_session)
    alert = Alert(source="x", dedup_key="k", title="DB down", status="firing")
    db_session.add(alert)
    db_session.flush()
    esc = _esc(db_session, ro, alert=alert)
    _login(client, "ic@x.io")
    client.post(
        f"/oncall/escalations/{esc.id}/declare",
        data={"severity_level_id": db_session.query(SeverityLevel).first().id},
        follow_redirects=False,
    )
    with Session(pg_engine) as fresh:
        incs = fresh.query(Incident).all()
        assert len(incs) == 1
        assert fresh.get(Escalation, esc.id).incident_id == incs[0].id
        incident_id = incs[0].id
        severity_level_id = incs[0].severity_level_id
    r = client.post(
        f"/alerts/{alert.id}/declare",
        data={"severity_level_id": severity_level_id},
        follow_redirects=True,
    )
    assert r.status_code == 422
    assert "already has an incident" in r.text
    with Session(pg_engine) as fresh:
        assert fresh.query(Incident).count() == 1
        assert fresh.get(Alert, alert.id).incident_id == incident_id
    r = client.post(
        f"/oncall/escalations/{esc.id}/declare",
        data={"severity_level_id": db_session.query(SeverityLevel).first().id},
        follow_redirects=True,
    )
    assert "Declare needs an alert without an incident." in r.text
    with Session(pg_engine) as fresh:
        assert fresh.query(Incident).count() == 1
        assert fresh.get(Escalation, esc.id).incident_id == incident_id


def test_concurrent_declares_create_one_incident(db_session, pg_engine):
    import threading

    from sqlalchemy.orm import Session

    from app.models import User
    from app.routers.alerts import declare_from_alert

    ic, ro, _ = _users(db_session)
    alert = Alert(source="x", dedup_key="k", title="DB down", status="firing")
    db_session.add(alert)
    db_session.commit()
    aid, uid = alert.id, ic.id
    barrier = threading.Barrier(2)
    results = []

    def run():
        with Session(pg_engine) as s:
            a, u = s.get(Alert, aid), s.get(User, uid)
            barrier.wait()
            try:
                declare_from_alert(s, a, user=u)
                results.append("ok")
            except ValueError:
                results.append("refused")

    ts = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(results) == ["ok", "refused"]
    with Session(pg_engine) as fresh:
        assert fresh.query(Incident).count() == 1


def test_list_pages_show_paged_names_and_inline_ack_only_for_allowed(client, db_session):
    ic, ro, other = _users(db_session)
    esc = _esc(db_session, ro)
    action = f'action="/oncall/escalations/{esc.id}/ack"'
    for email, allowed in (("ro@x.io", True), ("other@x.io", False), ("ic@x.io", True)):
        client.get("/logout")
        _login(client, email)
        for path in ("/oncall", "/oncall/escalations"):
            body = client.get(path).text
            assert "Ro" in body and "triggered" in body and "high urgency" in body
            assert (action in body) is allowed, (email, path)


def test_inline_ack_redirects_back_to_list_and_rejects_foreign_targets(client, db_session):
    ic, ro, _ = _users(db_session)
    esc = _esc(db_session, ro)
    _login(client, "ro@x.io")
    r = client.post(
        f"/oncall/escalations/{esc.id}/ack",
        data={"return_to": "/oncall/escalations"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/oncall/escalations"
    db_session.expire_all()
    assert db_session.get(Escalation, esc.id).status == EscalationStatus.acknowledged
    # acknowledged rows no longer offer an inline ack
    assert f'action="/oncall/escalations/{esc.id}/ack"' not in client.get("/oncall").text
    for bad in ("https://evil.example/", "//evil.example", "/oncall/../x"):
        esc2 = _esc(db_session, ro)
        r = client.post(
            f"/oncall/escalations/{esc2.id}/ack", data={"return_to": bad}, follow_redirects=False
        )
        assert r.headers["location"] == f"/oncall/escalations/{esc2.id}"


def test_detail_primary_ack_collapses_skipped_and_uses_time_tags(client, db_session):
    ic, ro, _ = _users(db_session)
    esc = _esc(db_session, ro)
    db_session.add(
        NotificationJob(
            escalation_id=esc.id,
            kind="notify",
            run_at=datetime.now(UTC),
            status="skipped",
            user_id=ro.id,
            channel="slack",
            level=0,
            detail="Ro is not reachable on Slack",
        )
    )
    db_session.commit()
    _login(client, "ro@x.io")
    body = client.get(f"/oncall/escalations/{esc.id}").text
    assert 'class="btn btn-primary btn-lg">Acknowledge' in body
    assert "1 skipped" in body and "<details" in body
    assert body.index("1 skipped") > body.index("sent")
    assert "<time datetime=" in body


@pytest.mark.parametrize("severity", [None, 99999])
def test_declare_requires_explicit_valid_severity_and_keeps_draft(client, db_session, severity):
    from app.models import CustomFieldDef

    _, ro, _ = _users(db_session)
    field = CustomFieldDef(label="Customer impact", field_type="text", required=True)
    alert = Alert(source="x", dedup_key="required-severity", title="DB down", status="firing")
    db_session.add_all([field, alert])
    db_session.flush()
    esc = _esc(db_session, ro, alert=alert)
    _login(client, "ic@x.io")
    payload = {f"cf_{field.id}": "Preserved impact draft"}
    if severity is not None:
        payload["severity_level_id"] = severity
    r = client.post(
        f"/oncall/escalations/{esc.id}/declare",
        data=payload,
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 422
    assert "Choose an incident severity" in r.text and "Preserved impact draft" in r.text
    assert r.headers["HX-Retarget"] == "main"
    assert 'name="severity_level_id" required' in r.text
    assert db_session.query(Incident).count() == 0
    assert db_session.get(Alert, alert.id).incident_id is None
    assert db_session.get(Escalation, esc.id).incident_id is None


def test_declare_collects_global_and_default_type_required_fields(client, db_session):
    from app.models import CustomFieldDef, IncidentCustomFieldValue, IncidentType

    _, ro, _ = _users(db_session)
    kind = IncidentType(label="Outage", is_default=True)
    chosen = SeverityLevel(label="SEV2", color="#123456", rank=2)
    db_session.add_all([kind, chosen])
    db_session.flush()
    global_field = CustomFieldDef(label="Customer impact", field_type="text", required=True)
    typed_field = CustomFieldDef(
        label="Affected region",
        field_type="select",
        options="EU\nUS",
        required=True,
        incident_types=[kind],
    )
    alert = Alert(source="x", dedup_key="required-context", title="DB down", status="firing")
    db_session.add_all([global_field, typed_field, alert])
    db_session.flush()
    esc = _esc(db_session, ro, alert=alert)
    _login(client, "ic@x.io")
    url = f"/oncall/escalations/{esc.id}/declare"
    detail = client.get(f"/oncall/escalations/{esc.id}")
    assert "Incident severity" in detail.text and "Customer impact" in detail.text
    assert "Affected region" in detail.text
    payload = {"severity_level_id": chosen.id}
    r = client.post(url, data=payload, follow_redirects=False)
    assert r.status_code == 422 and "Customer impact is required" in r.text
    payload[f"cf_{global_field.id}"] = "Checkout unavailable"
    r = client.post(url, data=payload, follow_redirects=False)
    assert r.status_code == 422 and "Affected region is required" in r.text
    assert "Checkout unavailable" in r.text
    payload[f"cf_{typed_field.id}"] = "invalid"
    r = client.post(url, data=payload, follow_redirects=False)
    assert r.status_code == 422 and "Choose a valid option" in r.text
    assert db_session.query(Incident).count() == 0
    payload[f"cf_{typed_field.id}"] = "EU"
    r = client.post(url, data=payload, follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    inc = db_session.query(Incident).one()
    assert inc.severity_level_id == chosen.id
    assert db_session.get(Alert, alert.id).incident_id == inc.id
    assert db_session.get(Escalation, esc.id).incident_id == inc.id
    values = {v.field_id: v.value for v in db_session.query(IncidentCustomFieldValue).all()}
    assert values == {global_field.id: "Checkout unavailable", typed_field.id: "EU"}
