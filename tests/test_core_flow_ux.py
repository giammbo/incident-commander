import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import CustomFieldDef, Incident, Role, SeverityLevel
from tests.test_alerts_routes import _alert
from tests.test_incidents_routes import _login, _sev


@pytest.fixture
def client(get_db_override):
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override
    return TestClient(app)


def setup(client, db):
    _login(client, db, "core@x.io", Role.incident_commander)
    return _sev(db)


def test_declare_opens_detail_even_htmx(client, db_session):
    sid = setup(client, db_session)
    r = client.post(
        "/incidents",
        data={"title": "Checkout down", "severity_level_id": sid},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    inc = db_session.scalar(select(Incident))
    assert r.headers.get("HX-Redirect") == f"/incidents/{inc.id}"


def test_whitespace_title_preserves_draft_without_creating(client, db_session):
    sid = setup(client, db_session)
    r = client.post(
        "/incidents",
        data={"title": "   ", "description": "Draft context", "severity_level_id": sid},
        follow_redirects=False,
    )
    assert r.status_code == 422
    assert "Draft context" in r.text and "Title is required" in r.text
    assert db_session.scalar(select(Incident)) is None


def test_required_custom_field_blocks_creation(client, db_session):
    sid = setup(client, db_session)
    db_session.add(CustomFieldDef(label="Customer impact", field_type="text", required=True))
    db_session.commit()
    r = client.post(
        "/incidents",
        data={"title": "Draft incident", "severity_level_id": sid},
        follow_redirects=False,
    )
    assert r.status_code == 422 and "Customer impact" in r.text
    assert db_session.scalar(select(Incident)) is None


def test_default_alerts_excludes_resolved_and_requires_explicit_severity(client, db_session):
    _login(client, db_session, "core@x.io", Role.incident_commander)
    a = _alert(db_session)
    a.title = "Resolved unique alert"
    a.status = "resolved"
    db_session.commit()
    assert "Resolved unique alert" not in client.get("/alerts").text
    r = client.post(f"/alerts/{a.id}/declare", follow_redirects=False)
    assert r.status_code == 422
    assert "Choose an incident severity" in r.text
    severity = db_session.scalar(select(SeverityLevel))
    r = client.post(
        f"/alerts/{a.id}/declare", data={"severity_level_id": severity.id}, follow_redirects=False
    )
    assert r.status_code == 303
    db_session.refresh(a)
    assert a.incident.severity_level_id == severity.id


def test_board_search_filters_rows(client, db_session):
    sid = setup(client, db_session)
    for title in ("Find this incident", "Unrelated incident"):
        client.post("/incidents", data={"title": title, "severity_level_id": sid})
    html = client.get("/?q=Find").text
    assert "Find this incident" in html and "Unrelated incident" not in html


def test_empty_attach_is_corrective_html(client, db_session):
    _login(client, db_session, "core@x.io", Role.incident_commander)
    a = _alert(db_session)
    r = client.post(f"/alerts/{a.id}/attach", data={"incident_id": ""})
    assert r.status_code == 422 and "Choose an open incident" in r.text
    assert r.headers["content-type"].startswith("text/html")


def test_edit_whitespace_and_update_error_keep_bound_drafts(client, db_session):
    sid = setup(client, db_session)
    client.post("/incidents", data={"title": "Original title", "severity_level_id": sid})
    inc = db_session.scalar(select(Incident))
    r = client.post(
        f"/incidents/{inc.id}/edit",
        data={"title": "   ", "description": "Edited draft", "severity_level_id": sid},
    )
    assert r.status_code == 422 and "Edited draft" in r.text
    db_session.refresh(inc)
    assert inc.title == "Original title"
    r = client.post(
        f"/incidents/{inc.id}/updates",
        data={"message": "Keep this update", "status_id": 99999, "return_tab": "timeline"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 422 and "Keep this update" in r.text
    assert r.headers.get("HX-Retarget") == "main"
    assert "panel-timeline" in r.text


def test_global_followup_edit_status_and_invalid_draft(client, db_session):
    from app.models import FollowUp, User

    sid = setup(client, db_session)
    client.post("/incidents", data={"title": "Follow-up incident", "severity_level_id": sid})
    inc = db_session.scalar(select(Incident))
    client.post(f"/incidents/{inc.id}/followups", data={"title": "Action"})
    fu = db_session.scalar(select(FollowUp))
    owner = db_session.scalar(select(User))
    r = client.post(
        f"/followups/{fu.id}/edit",
        data={
            "title": "Edited action",
            "description": "Context",
            "assignee_id": owner.id,
            "due_on": "2026-11-05",
            "return_tab": "global",
            "return_filter": "all",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/follow-ups?status=all"
    db_session.refresh(fu)
    assert (
        fu.title == "Edited action"
        and fu.assignee_id == owner.id
        and str(fu.due_on) == "2026-11-05"
    )
    r = client.post(
        f"/followups/{fu.id}/edit",
        data={
            "title": "  ",
            "description": "Unsent draft",
            "return_tab": "global",
            "return_filter": "all",
        },
        follow_redirects=False,
    )
    assert r.status_code == 422 and "Unsent draft" in r.text
    for state in ("completed", "cancelled", "open"):
        r = client.post(
            f"/followups/{fu.id}/status",
            data={"status": state, "return_tab": "global", "return_filter": "all"},
            follow_redirects=False,
        )
        assert r.headers["location"] == "/follow-ups?status=all"
        db_session.refresh(fu)
        assert fu.status == state


def test_board_filtered_close_reconciles_membership(client, db_session):
    from app.models import StatusLevel

    sid = setup(client, db_session)
    client.post("/incidents", data={"title": "Find incident", "severity_level_id": sid})
    inc = db_session.scalar(select(Incident))
    status = db_session.get(StatusLevel, inc.status_id)
    path = f"/?q=Find&status={status.id}&severity={sid}"
    r = client.post(
        f"/incidents/{inc.id}/close",
        headers={"HX-Request": "true", "HX-Current-URL": f"http://testserver{path}"},
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Redirect") == path
    assert "Find incident" not in client.get(path).text


def test_postmortem_invalid_body_preserves_editor(client, db_session):
    sid = setup(client, db_session)
    client.post("/incidents", data={"title": "Incident", "severity_level_id": sid})
    inc = db_session.scalar(select(Incident))
    client.post(f"/incidents/{inc.id}/postmortem/generate")
    r = client.post(f"/incidents/{inc.id}/postmortem", data={"body": "   "}, follow_redirects=False)
    assert r.status_code == 422 and "Postmortem cannot be empty" in r.text
    assert "core-draft" in r.text


def test_edit_requires_and_can_save_custom_fields(client, db_session):
    from app.models import IncidentCustomFieldValue

    sid = setup(client, db_session)
    client.post("/incidents", data={"title": "Original", "severity_level_id": sid})
    inc = db_session.scalar(select(Incident))
    field = CustomFieldDef(label="Required new context", field_type="text", required=True)
    db_session.add(field)
    db_session.commit()
    r = client.post(
        f"/incidents/{inc.id}/edit",
        data={"title": "Changed", "severity_level_id": sid},
        follow_redirects=False,
    )
    assert r.status_code == 422 and "Required new context is required" in r.text
    r = client.post(
        f"/incidents/{inc.id}/edit",
        data={"title": "Changed", "severity_level_id": sid, f"cf_{field.id}": "Customers affected"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db_session.refresh(inc)
    assert inc.title == "Changed"
    assert (
        db_session.scalar(
            select(IncidentCustomFieldValue).where(IncidentCustomFieldValue.incident_id == inc.id)
        ).value
        == "Customers affected"
    )


def test_alert_declaration_requires_custom_context_and_preserves_choice(client, db_session):
    _login(client, db_session, "core@x.io", Role.incident_commander)
    alert = _alert(db_session)
    field = CustomFieldDef(label="Incident impact", field_type="text", required=True)
    db_session.add(field)
    db_session.commit()
    severity = db_session.scalar(select(SeverityLevel))
    response = client.post(
        f"/alerts/{alert.id}/declare",
        data={"severity_level_id": severity.id},
        follow_redirects=False,
    )
    assert response.status_code == 422 and "Incident impact is required" in response.text
    assert f'name="cf_{field.id}"' in response.text
    assert db_session.scalar(select(Incident)) is None
    response = client.post(
        f"/alerts/{alert.id}/declare",
        data={"severity_level_id": severity.id, f"cf_{field.id}": "Checkout unavailable"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    db_session.refresh(alert)
    assert alert.incident_id is not None


def test_core_controls_match_readonly_role_and_closed_phase(client, db_session):
    from app.models import StatusCategory, StatusLevel

    sid = setup(client, db_session)
    client.post("/incidents", data={"title": "Responding", "severity_level_id": sid})
    incident = db_session.scalar(select(Incident))
    timeline = client.get(f"/incidents/{incident.id}?tab=timeline").text
    assert timeline.index('class="respond"') < timeline.index('id="panel-timeline"')
    closed = db_session.scalar(
        select(StatusLevel).where(StatusLevel.category == StatusCategory.closed)
    )
    client.post(f"/incidents/{incident.id}/status", data={"status_id": closed.id})
    body = client.get(f"/incidents/{incident.id}").text
    assert (
        "After the incident" in body and "Reopen incident" in body and 'class="respond"' not in body
    )
    _login(client, db_session, "viewer@x.io", Role.read_only)
    assert 'class="respond"' not in client.get(f"/incidents/{incident.id}").text
    assert "Reopen incident" not in client.get(f"/incidents/{incident.id}").text
    assert ">Close</button>" not in client.get("/").text
    assert 'type="submit">Declare incident' not in client.get("/alerts").text


def test_board_combines_system_severity_and_status_filters(client, db_session):
    from app.services.catalog import create_system

    sid = setup(client, db_session)
    system = create_system(db_session, name="Checkout")
    db_session.commit()
    client.post(
        "/incidents",
        data={"title": "Scoped incident", "severity_level_id": sid, "system_id": system.id},
    )
    client.post("/incidents", data={"title": "Other incident", "severity_level_id": sid})
    incident = db_session.scalar(select(Incident).where(Incident.title == "Scoped incident"))
    response = client.get(
        f"/?q=incident&system={system.id}&severity={sid}&status={incident.status_id}"
    )
    assert "Scoped incident" in response.text and "Other incident" not in response.text


def test_stakeholder_update_accepts_500_characters_in_browser_form(client, db_session):
    from html.parser import HTMLParser

    from app.models import IncidentEvent

    class UpdateForm(HTMLParser):
        maximum = None

        def handle_starttag(self, tag, attrs):
            fields = dict(attrs)
            if tag == "textarea" and fields.get("name") == "message":
                self.maximum = fields.get("maxlength")

    sid = setup(client, db_session)
    client.post("/incidents", data={"title": "Long update", "severity_level_id": sid})
    incident = db_session.scalar(select(Incident))
    form = UpdateForm()
    form.feed(client.get(f"/incidents/{incident.id}").text)
    assert form.maximum is None or int(form.maximum) >= 500
    message = "Operational investigation and customer impact. " * 10
    message = message.ljust(500, "x")[:500]
    response = client.post(
        f"/incidents/{incident.id}/updates", data={"message": message}, follow_redirects=False
    )
    assert response.status_code == 303
    event = db_session.scalar(select(IncidentEvent).where(IncidentEvent.entry_type == "update"))
    assert event.body == message and len(event.body) == 500
