from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import (
    Alert,
    AlertRoute,
    Escalation,
    EscalationStatus,
    InboundIntegration,
    Incident,
    Role,
    SeverityLevel,
    Urgency,
    WorkflowRule,
)
from app.services import oncall_catalog as cat
from app.services import oncall_engine as eng
from app.services import oncall_routes as routes
from app.services import statuses
from app.services.users import bootstrap_admin, create_user

TOK = "route-tok"


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
    db_session.commit()
    client.post("/login", data={"email": email, "password": "pw-123456"})


def _setup(db_session):
    statuses.seed_status_levels(db_session)
    integ = InboundIntegration(
        name="prod alertmanager",
        kind="generic",
        token=TOK,
        settings={
            "dedup_key": "id",
            "title": "name",
            "severity": "sev",
            "status": "state",
            "resolved_value": "ok",
        },
    )
    ann = create_user(
        db_session, email="ann@x.io", name="Ann", role=Role.incident_commander, password="pw-123456"
    )
    db_session.add(integ)
    db_session.flush()
    path = cat.create_path(db_session, name="Payments path")
    cat.add_level(db_session, path, schedule_ids=[], user_ids=[ann.id], ack_timeout_minutes=5)
    db_session.commit()
    return integ, path


def _route(db_session, path, **kw):
    kw.setdefault("name", "r")
    kw.setdefault("conditions", [])
    r = AlertRoute(escalation_path_id=path.id, **kw)
    db_session.add(r)
    db_session.commit()
    return r


def _fire(client, key="a1", sev="critical", state="alerting", name="DB down"):
    r = client.post(f"/ingest/{TOK}", json={"id": key, "name": name, "sev": sev, "state": state})
    assert r.status_code == 200
    return r


def _escs(db_session):
    db_session.expire_all()
    return list(db_session.scalars(select(Escalation).order_by(Escalation.id)))


def _alert(db_session, key="a1"):
    db_session.expire_all()
    return db_session.scalar(select(Alert).where(Alert.dedup_key == key))


def _mk_alert(**kw):
    kw.setdefault("source", "alertmanager")
    kw.setdefault("title", "t")
    return Alert(dedup_key="k", **kw)


# --- pure matching ---------------------------------------------------------


def test_route_matching_and_rank_order(db_session):
    integ, path = _setup(db_session)
    alert = _mk_alert(integration_id=integ.id, severity_raw="critical")
    db_session.add(alert)
    _route(db_session, path, name="late", rank=5, conditions=[])
    _route(
        db_session,
        path,
        name="crit",
        rank=2,
        conditions=[
            {"field": "integration", "op": "equals", "value": integ.id},
            {"field": "severity_raw", "op": "in", "value": ["critical", "high"]},
        ],
    )
    _route(db_session, path, name="off", rank=1, enabled=False)
    _route(
        db_session,
        path,
        name="nomatch",
        rank=1,
        conditions=[{"field": "source", "op": "equals", "value": "sns"}],
    )
    assert routes.match_route(db_session, alert).name == "crit"
    alert.severity_raw = "warning"
    assert routes.match_route(db_session, alert).name == "late"
    assert routes.route_matches([], alert) is True
    assert (
        routes.route_matches([{"field": "integration", "op": "equals", "value": 9999}], alert)
        is False
    )


def test_title_contains_is_case_insensitive():
    a = _mk_alert(title="Database Connection LOST")
    cond = [{"field": "title", "op": "contains", "value": "connection lost"}]
    assert routes.route_matches(cond, a)
    assert not routes.route_matches([{"field": "title", "op": "contains", "value": "nope"}], a)
    assert routes.build_route_condition("title", "contains", ["  Lost "]) == {
        "field": "title",
        "op": "contains",
        "value": "Lost",
    }
    assert routes.build_route_condition("source", "contains", ["x"]) == {
        "field": "source",
        "op": "equals",
        "value": "x",
    }
    assert routes.build_route_condition("integration", "equals", ["3"])["value"] == 3
    assert routes.build_route_condition("", "equals", ["x"]) is None


def test_high_if_sets_urgency_low_otherwise():
    hi = [{"field": "severity_raw", "op": "equals", "value": "critical"}]
    r = AlertRoute(urgency=Urgency.high, high_if=hi)
    assert routes.route_urgency(r, _mk_alert(severity_raw="critical")) == Urgency.high
    assert routes.route_urgency(r, _mk_alert(severity_raw="warning")) == Urgency.low
    r2 = AlertRoute(urgency=Urgency.low, high_if=[])
    assert routes.route_urgency(r2, _mk_alert(severity_raw="critical")) == Urgency.low


# --- ingest ----------------------------------------------------------------


def test_ingest_firing_alert_starts_escalation_once_even_when_repeated(client, db_session):
    _, path = _setup(db_session)
    _route(
        db_session,
        path,
        conditions=[{"field": "severity_raw", "op": "equals", "value": "critical"}],
    )
    _fire(client)
    _fire(client)
    _fire(client)
    escs = _escs(db_session)
    assert len(escs) == 1
    e = escs[0]
    assert e.source == "alert_route" and e.path_id == path.id and e.urgency == Urgency.high
    assert e.alert_id == _alert(db_session).id and e.title == "DB down"


def test_resolved_from_source_closes_escalation(client, db_session):
    _, path = _setup(db_session)
    _route(db_session, path)
    _fire(client)
    _fire(client, state="ok")
    (e,) = _escs(db_session)
    assert e.status == EscalationStatus.resolved
    assert _alert(db_session).status == "resolved"


def test_inbox_resolve_closes_escalation(client, db_session):
    _, path = _setup(db_session)
    _route(db_session, path)
    _fire(client)
    _login(client, db_session, role="ic")
    aid = _alert(db_session).id
    r = client.post(f"/alerts/{aid}/resolve", follow_redirects=False)
    assert r.status_code == 303
    (e,) = _escs(db_session)
    assert e.status == EscalationStatus.resolved
    assert _alert(db_session).status == "resolved"


def test_escalation_resolve_does_not_resolve_alert_and_repeat_does_not_repage(client, db_session):
    _, path = _setup(db_session)
    _route(db_session, path)
    _fire(client)
    (e,) = _escs(db_session)
    eng.resolve(db_session, e, user_id=None, via="web", now=datetime.now(UTC))
    db_session.commit()
    assert _alert(db_session).status == "firing"
    _fire(client)  # alertmanager repeat_interval
    escs = _escs(db_session)
    assert len(escs) == 1 and escs[0].status == EscalationStatus.resolved
    assert _alert(db_session).status == "firing"


def test_exhausted_escalation_blocks_another(client, db_session):
    _, path = _setup(db_session)
    _route(db_session, path)
    _fire(client)
    (e,) = _escs(db_session)
    e.status = EscalationStatus.exhausted
    db_session.commit()
    _fire(client)
    assert len(_escs(db_session)) == 1


def test_refire_after_source_resolved_pages_again(client, db_session):
    _, path = _setup(db_session)
    _route(db_session, path)
    _fire(client)
    _fire(client, state="ok")
    _fire(client)
    escs = _escs(db_session)
    assert [e.status for e in escs] == [EscalationStatus.resolved, EscalationStatus.triggered]


def test_no_route_no_escalation_inbox_unchanged(client, db_session):
    _setup(db_session)
    _fire(client)
    assert _escs(db_session) == []
    a = _alert(db_session)
    assert a.status == "firing" and a.title == "DB down"


def test_non_matching_route_does_not_page(client, db_session):
    _, path = _setup(db_session)
    _route(db_session, path, conditions=[{"field": "severity_raw", "op": "equals", "value": "x"}])
    _fire(client)
    assert _escs(db_session) == []


def test_existing_alert_automation_still_runs_alongside_paging(client, db_session):
    _, path = _setup(db_session)
    db_session.add(SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True))
    db_session.add(
        WorkflowRule(
            name="critical->incident",
            trigger="alert.received",
            conditions=[{"field": "severity_raw", "op": "equals", "value": "critical"}],
            actions=[{"type": "create_incident", "params": {"is_private": False}}],
        )
    )
    _route(db_session, path)
    db_session.commit()
    _fire(client)
    a = _alert(db_session)
    assert a.incident_id is not None
    assert db_session.get(Incident, a.incident_id) is not None
    (e,) = _escs(db_session)
    assert e.alert_id == a.id


def test_paging_failure_does_not_break_ingest(client, db_session, monkeypatch):
    _, path = _setup(db_session)
    _route(db_session, path)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(routes, "page_for_alert", boom)
    _fire(client)
    assert _alert(db_session).status == "firing"


def test_paging_failure_sets_paging_error_shown_in_inbox_then_cleared(
    client, db_session, monkeypatch
):
    _, path = _setup(db_session)
    _route(db_session, path)
    real = routes.page_for_alert

    def boom(*a, **k):
        raise RuntimeError("secret+39333")

    monkeypatch.setattr(routes, "page_for_alert", boom)
    _fire(client)
    assert _alert(db_session).paging_error == "Paging failed: RuntimeError"
    _login(client, db_session, role="ro")
    page = client.get("/alerts").text
    assert "paging failed" in page and "secret" not in page
    monkeypatch.setattr(routes, "page_for_alert", real)
    _fire(client, state="ok")
    _fire(client)  # resolved -> firing: pages successfully
    assert _alert(db_session).paging_error is None
    assert len(_escs(db_session)) == 1


def test_failing_resolve_hook_still_resolves_alert_from_source(client, db_session, monkeypatch):
    _, path = _setup(db_session)
    _route(db_session, path)
    _fire(client)

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(routes.oncall_engine, "resolve_for_alert", boom)
    r = client.post(
        f"/ingest/{TOK}", json={"id": "a1", "name": "DB down", "sev": "critical", "state": "ok"}
    )
    assert r.status_code == 200
    a = _alert(db_session)
    assert a.status == "resolved"
    assert a.paging_error == "Could not close the paging escalation: RuntimeError"
    (e,) = _escs(db_session)
    assert e.status == EscalationStatus.triggered


def test_failing_resolve_hook_still_resolves_alert_from_inbox(client, db_session, monkeypatch):
    _, path = _setup(db_session)
    _route(db_session, path)
    _fire(client)
    _login(client, db_session, role="ic")

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(routes.oncall_engine, "resolve_for_alert", boom)
    aid = _alert(db_session).id
    r = client.post(f"/alerts/{aid}/resolve", follow_redirects=False)
    assert r.status_code == 303
    a = _alert(db_session)
    assert a.status == "resolved" and a.paging_error.startswith("Could not close")
    assert "paging failed" in client.get("/alerts?status=resolved").text


# --- inbox chip ------------------------------------------------------------


def test_inbox_shows_paging_chip(client, db_session):
    _, path = _setup(db_session)
    _route(db_session, path)
    _fire(client)
    _login(client, db_session, role="ro")
    r = client.get("/alerts")
    assert "paging: triggered" in r.text


# --- routes UI -------------------------------------------------------------


def test_route_pages_crud_and_sentence(client, db_session):
    integ, path = _setup(db_session)
    _login(client, db_session, role="ic")
    r = client.post(
        "/oncall/routes",
        data={
            "name": "Prod critical",
            "escalation_path_id": str(path.id),
            "urgency_mode": "high_when",
            "cond-0-field": "integration",
            "cond-0-op": "equals",
            "cond-0-value": str(integ.id),
            "cond-1-field": "severity_raw",
            "cond-1-op": "equals",
            "cond-1-value": "critical",
            "cond-2-field": "title",
            "cond-2-op": "contains",
            "cond-2-value": "db",
            "hi-0-field": "severity_raw",
            "hi-0-op": "in",
            "hi-0-value": "critical, high",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    route = db_session.scalar(select(AlertRoute))
    assert route.conditions == [
        {"field": "integration", "op": "equals", "value": integ.id},
        {"field": "severity_raw", "op": "equals", "value": "critical"},
        {"field": "title", "op": "contains", "value": "db"},
    ]
    assert route.high_if == [{"field": "severity_raw", "op": "in", "value": ["critical", "high"]}]
    lst = client.get("/oncall/routes").text
    assert "Alerts from prod alertmanager where severity is critical" in lst
    assert "Payments path (high" in lst
    d = client.get(f"/oncall/routes/{route.id}")
    assert d.status_code == 200 and "Prod critical" in d.text

    r = client.post(
        f"/oncall/routes/{route.id}/edit",
        data={
            "name": "Renamed",
            "escalation_path_id": str(path.id),
            "urgency_mode": "always_low",
            "cond-0-field": "source",
            "cond-0-op": "equals",
            "cond-0-value": "sns",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    db_session.expire_all()
    route = db_session.get(AlertRoute, route.id)
    assert route.name == "Renamed" and route.urgency == Urgency.low
    assert route.conditions == [{"field": "source", "op": "equals", "value": "sns"}]
    assert route.high_if == []

    client.post(f"/oncall/routes/{route.id}/toggle")
    db_session.expire_all()
    assert db_session.get(AlertRoute, route.id).enabled is False
    client.post(f"/oncall/routes/{route.id}/delete")
    db_session.expire_all()
    assert db_session.get(AlertRoute, route.id) is None


def test_route_move_swaps_rank(client, db_session):
    _, path = _setup(db_session)
    a = _route(db_session, path, name="a", rank=1)
    b = _route(db_session, path, name="b", rank=2)
    _login(client, db_session, role="ic")
    client.post(f"/oncall/routes/{b.id}/move", data={"direction": "up"})
    db_session.expire_all()
    assert db_session.get(AlertRoute, b.id).rank < db_session.get(AlertRoute, a.id).rank


def test_read_only_sees_routes_but_cannot_write(client, db_session):
    _, path = _setup(db_session)
    r = _route(db_session, path, name="visible")
    _login(client, db_session, role="ro")
    assert "visible" in client.get("/oncall/routes").text
    assert client.get(f"/oncall/routes/{r.id}").status_code == 200
    assert (
        client.post(
            "/oncall/routes", data={"name": "x", "escalation_path_id": str(path.id)}
        ).status_code
        == 403
    )
    assert client.post(f"/oncall/routes/{r.id}/delete").status_code == 403


def test_condition_value_widget(client, db_session):
    integ, _ = _setup(db_session)
    _login(client, db_session, role="ic")
    r = client.get(
        "/oncall/routes/condition-value",
        params={"prefix": "cond", "i": 1, "cond-1-field": "integration"},
    )
    assert "prod alertmanager" in r.text
    r = client.get(
        "/oncall/routes/condition-value", params={"prefix": "cond", "i": 0, "cond-0-field": "title"}
    )
    assert "contains" in r.text


def test_route_rejected_for_path_without_levels(client, db_session):
    _setup(db_session)
    empty = cat.create_path(db_session, name="Empty path")
    db_session.commit()
    _login(client, db_session, role="ic")
    r = client.post(
        "/oncall/routes",
        data={"name": "x", "escalation_path_id": str(empty.id), "urgency": "high"},
        follow_redirects=True,
    )
    assert "That escalation path has no levels yet." in r.text
    assert db_session.scalar(select(AlertRoute)) is None
