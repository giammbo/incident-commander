import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.models import Alert, InboundIntegration, Role, SeverityLevel
from app.services import statuses
from app.services.incidents import create_incident
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session, get_db_override):
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override
    return TestClient(app)


def _login_ic(client, db_session):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(
        db_session, email="ic@x.io", name="IC", role=Role.incident_commander, password="pw-123456"
    )
    db_session.flush()
    client.post("/login", data={"email": "ic@x.io", "password": "pw-123456"})


def _incident(db_session):
    statuses.seed_status_levels(db_session)
    db_session.flush()
    sev = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db_session.add(sev)
    db_session.flush()
    inc = create_incident(
        db_session, title="Tabby", severity_level_id=sev.id, is_private=False, created_by=1
    )
    db_session.flush()
    return inc


def test_detail_renders_tab_scaffold(client, db_session):
    _login_ic(client, db_session)
    inc = _incident(db_session)
    db_session.commit()
    body = client.get(f"/incidents/{inc.id}").text
    # tab radios + labels
    assert (
        'id="tab-overview"' in body and 'id="tab-timeline"' in body and 'id="tab-followups"' in body
    )
    assert ">Overview<" in body and ">Timeline<" in body and "Follow-ups" in body
    # Roles is always its own tab (panel + label), not buried in Overview
    assert 'id="tab-roles"' in body and 'id="panel-roles"' in body and ">Roles<" in body
    # content relocated into the page (timeline add-note form + respond box still present)
    assert f'action="/incidents/{inc.id}/notes"' in body
    assert f'action="/incidents/{inc.id}/updates"' in body  # Respond box always present
    # Alerts tab absent when there are no related alerts
    assert 'id="tab-alerts"' not in body


def test_alerts_tab_appears_with_related_alert(client, db_session):
    _login_ic(client, db_session)
    inc = _incident(db_session)
    integ = InboundIntegration(name="i", kind="generic", token="tk")
    db_session.add(integ)
    db_session.flush()
    db_session.add(
        Alert(
            integration_id=integ.id,
            source="generic",
            dedup_key="dk",
            title="Boom",
            status="firing",
            incident_id=inc.id,
        )
    )
    db_session.commit()
    body = client.get(f"/incidents/{inc.id}").text
    assert 'id="tab-alerts"' in body and ">Alerts<" in body


def _checked_tab(body: str) -> str | None:
    """Return the id of the tab radio rendered with `checked`."""
    import re

    m = re.search(
        r'<input class="tab-radio" type="radio" name="inc-tab" id="(tab-[a-z]+)" checked', body
    )
    return m.group(1) if m else None


def test_detail_defaults_to_overview_tab(client, db_session):
    _login_ic(client, db_session)
    inc = _incident(db_session)
    db_session.commit()
    assert _checked_tab(client.get(f"/incidents/{inc.id}").text) == "tab-overview"


@pytest.mark.parametrize("tab", ["overview", "roles", "timeline", "followups"])
def test_detail_tab_query_selects_radio(client, db_session, tab):
    _login_ic(client, db_session)
    inc = _incident(db_session)
    db_session.commit()
    assert _checked_tab(client.get(f"/incidents/{inc.id}?tab={tab}").text) == f"tab-{tab}"


def test_detail_unknown_tab_falls_back_to_overview(client, db_session):
    _login_ic(client, db_session)
    inc = _incident(db_session)
    db_session.commit()
    body = client.get(f"/incidents/{inc.id}?tab=%22%3E%3Cscript%3E").text
    assert _checked_tab(body) == "tab-overview"


def test_detail_alerts_tab_without_alerts_falls_back_to_overview(client, db_session):
    _login_ic(client, db_session)
    inc = _incident(db_session)
    db_session.commit()
    assert _checked_tab(client.get(f"/incidents/{inc.id}?tab=alerts").text) == "tab-overview"


def test_detail_alerts_tab_selected_when_alerts_exist(client, db_session):
    _login_ic(client, db_session)
    inc = _incident(db_session)
    integ = InboundIntegration(name="i", kind="generic", token="tk2")
    db_session.add(integ)
    db_session.flush()
    db_session.add(
        Alert(
            integration_id=integ.id,
            source="generic",
            dedup_key="dk2",
            title="Boom",
            status="firing",
            incident_id=inc.id,
        )
    )
    db_session.commit()
    assert _checked_tab(client.get(f"/incidents/{inc.id}?tab=alerts").text) == "tab-alerts"


def test_tab_panel_forms_carry_return_tab(client, db_session):
    _login_ic(client, db_session)
    inc = _incident(db_session)
    from app.services import roles

    roles.seed_incident_role_types(db_session)
    db_session.commit()
    body = client.get(f"/incidents/{inc.id}").text
    for tab in ("overview", "roles", "timeline", "followups"):
        assert f'<input type="hidden" name="return_tab" value="{tab}">' in body


def test_note_post_returns_to_timeline_tab(client, db_session):
    _login_ic(client, db_session)
    inc = _incident(db_session)
    db_session.commit()
    r = client.post(
        f"/incidents/{inc.id}/notes",
        data={"body": "hello", "return_tab": "timeline"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/incidents/{inc.id}?tab=timeline"


def test_followup_post_returns_to_followups_tab(client, db_session):
    _login_ic(client, db_session)
    inc = _incident(db_session)
    db_session.commit()
    r = client.post(
        f"/incidents/{inc.id}/followups",
        data={"title": "Fix it", "return_tab": "followups"},
        follow_redirects=False,
    )
    assert r.headers["location"] == f"/incidents/{inc.id}?tab=followups"
    from app.models import FollowUp

    fu = db_session.query(FollowUp).filter_by(incident_id=inc.id).one()
    r = client.post(
        f"/followups/{fu.id}/status",
        data={"status": "completed", "return_tab": "followups"},
        follow_redirects=False,
    )
    assert r.headers["location"] == f"/incidents/{inc.id}?tab=followups"
    r = client.post(
        f"/followups/{fu.id}/delete", data={"return_tab": "followups"}, follow_redirects=False
    )
    assert r.headers["location"] == f"/incidents/{inc.id}?tab=followups"


def test_roles_post_returns_to_roles_tab(client, db_session):
    _login_ic(client, db_session)
    inc = _incident(db_session)
    from app.services import roles

    roles.seed_incident_role_types(db_session)
    db_session.commit()
    rt = roles.list_incident_role_types(db_session)[0]
    r = client.post(
        f"/incidents/{inc.id}/roles",
        data={"role_type_id": rt.id, "return_tab": "roles"},
        follow_redirects=False,
    )
    assert r.headers["location"] == f"/incidents/{inc.id}?tab=roles"


def test_pin_post_returns_to_timeline_tab(client, db_session):
    _login_ic(client, db_session)
    inc = _incident(db_session)
    from app.services.timeline import add_note

    ev = add_note(db_session, inc, body="n", created_by=1)
    db_session.commit()
    r = client.post(
        f"/incidents/{inc.id}/events/{ev.id}/pin",
        data={"return_tab": "timeline"},
        follow_redirects=False,
    )
    assert r.headers["location"] == f"/incidents/{inc.id}?tab=timeline"


def test_update_post_returns_to_timeline_tab(client, db_session):
    _login_ic(client, db_session)
    inc = _incident(db_session)
    db_session.commit()
    r = client.post(
        f"/incidents/{inc.id}/updates",
        data={"message": "status: investigating", "return_tab": "timeline"},
        follow_redirects=False,
    )
    assert r.headers["location"] == f"/incidents/{inc.id}?tab=timeline"


def test_edit_post_returns_to_overview_tab(client, db_session):
    _login_ic(client, db_session)
    inc = _incident(db_session)
    db_session.commit()
    r = client.post(
        f"/incidents/{inc.id}/edit",
        data={
            "title": "Renamed",
            "severity_level_id": inc.severity_level_id,
            "return_tab": "overview",
        },
        follow_redirects=False,
    )
    assert r.headers["location"] == f"/incidents/{inc.id}?tab=overview"


def test_return_tab_outside_whitelist_is_ignored(client, db_session):
    _login_ic(client, db_session)
    inc = _incident(db_session)
    db_session.commit()
    r = client.post(
        f"/incidents/{inc.id}/notes",
        data={"body": "x", "return_tab": "https://evil.example"},
        follow_redirects=False,
    )
    assert r.headers["location"] == f"/incidents/{inc.id}"


def test_post_without_return_tab_keeps_plain_redirect(client, db_session):
    _login_ic(client, db_session)
    inc = _incident(db_session)
    db_session.commit()
    r = client.post(f"/incidents/{inc.id}/notes", data={"body": "x"}, follow_redirects=False)
    assert r.headers["location"] == f"/incidents/{inc.id}"
