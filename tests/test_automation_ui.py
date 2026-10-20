import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.models import (
    IncidentRoleType,
    IncidentType,
    Role,
    SeverityLevel,
    StatusCategory,
    StatusLevel,
    System,
    WorkflowRule,
)
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session, get_db_override):
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override
    return TestClient(app)


def _login_admin(client, db_session):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(db_session, email="a@x.io", name="A", role=Role.admin, password="pw-123456")
    db_session.flush()
    client.post("/login", data={"email": "a@x.io", "password": "pw-123456"})


def test_automations_page_lists_rules(client, db_session):
    _login_admin(client, db_session)
    db_session.add(
        WorkflowRule(name="My Rule", trigger="incident.opened", conditions=[], actions=[])
    )
    db_session.commit()
    body = client.get("/automations").text
    assert "My Rule" in body and "When an incident is opened" in body


def test_action_params_partial(client, db_session):
    _login_admin(client, db_session)
    # the HTMX partial returns the param inputs for a chosen action type
    body = client.get("/automations/action-params?type=create_followup").text
    assert "title" in body  # create_followup exposes a title input


def test_action_params_partial_reads_the_select_name(client, db_session):
    _login_admin(client, db_session)
    # hx-include="this" on <select name="action_type"> sends ?action_type=, not ?type=
    body = client.get("/automations/action-params?action_type=post_update").text
    assert 'name="ap_message"' in body


def test_action_params_partial_requires_admin(client, db_session):
    _login_admin(client, db_session)  # admin ok
    assert client.get("/automations/action-params?type=set_status").status_code == 200


def _catalog(db_session):
    sev1 = SeverityLevel(label="SEV1", color="#FF0000", rank=1, is_default=True)
    crit = SeverityLevel(label="Critical", color="#FF8800", rank=2)
    outage = IncidentType(label="Outage", rank=1)
    api = System(name="Payments API")
    mitigated = StatusLevel(label="Mitigated", category=StatusCategory.active, rank=5)
    lead = IncidentRoleType(label="Incident lead", rank=1)
    db_session.add_all([sev1, crit, outage, api, mitigated, lead])
    db_session.flush()
    return sev1, crit, outage, api, mitigated, lead


def test_trigger_select_shows_readable_labels(client, db_session):
    _login_admin(client, db_session)
    body = client.get("/automations").text
    assert '<option value="alert.received"' in body
    assert "When an alert is received" in body
    assert "When an incident&#39;s status changes" in body
    assert 'name="cond_field"' not in body  # no free-text field box any more
    assert 'name="cond-0-field"' in body


def test_rule_summary_resolves_ids_to_names(client, db_session):
    _login_admin(client, db_session)
    sev1, crit, outage, api, mitigated, lead = _catalog(db_session)
    db_session.add_all(
        [
            WorkflowRule(
                name="Page on critical",
                trigger="alert.received",
                conditions=[{"field": "severity_raw", "op": "equals", "value": "critical"}],
                actions=[
                    {
                        "type": "create_incident",
                        "params": {"severity_level_id": sev1.id, "incident_type_id": outage.id},
                    }
                ],
            ),
            WorkflowRule(
                name="Escalate",
                trigger="incident.opened",
                conditions=[
                    {"field": "severity", "op": "in", "value": [sev1.id, crit.id]},
                    {"field": "system", "op": "equals", "value": api.id},
                    {"field": "is_private", "op": "equals", "value": False},
                ],
                actions=[{"type": "set_status", "params": {"status_id": mitigated.id}}],
            ),
        ]
    )
    db_session.commit()
    body = client.get("/automations").text
    assert (
        "When an alert is received and severity is critical → create incident (SEV1, Outage)"
        in body
    )
    assert (
        "When an incident is opened and severity is one of SEV1, Critical and system is "
        "Payments API and the incident is not private → set status to Mitigated"
    ) in body


def test_summary_tolerates_legacy_and_dangling_values(client, db_session):
    _login_admin(client, db_session)
    sev1, *_ = _catalog(db_session)
    # rules saved before the picker: string IDs, a since-deleted ID, no actions
    db_session.add(
        WorkflowRule(
            name="Old",
            trigger="incident.status_changed",
            conditions=[
                {"field": "severity", "op": "equals", "value": str(sev1.id)},
                {"field": "type", "op": "equals", "value": 99999},
            ],
            actions=[],
        )
    )
    db_session.commit()
    body = client.get("/automations").text
    assert "severity is SEV1 and type is #99999 (deleted) → do nothing" in body


def test_builder_partial_limits_fields_and_actions_to_trigger(client, db_session):
    _login_admin(client, db_session)
    alert = client.get("/automations/builder?trigger=alert.received").text
    assert 'value="severity_raw"' in alert and 'value="status"' in alert
    assert 'value="system"' not in alert and 'value="is_private"' not in alert
    assert "Create an incident" in alert and "Set the status" not in alert
    inc = client.get("/automations/builder?trigger=incident.opened").text
    assert 'value="system"' in inc and 'value="severity_raw"' not in inc
    assert "Set the status" in inc and "Create an incident" not in inc


def test_condition_value_select_for_catalog_fields(client, db_session):
    _login_admin(client, db_session)
    sev1, crit, outage, api, *_ = _catalog(db_session)
    db_session.commit()
    body = client.get("/automations/condition-value?i=1&cond-1-field=severity").text
    assert 'name="cond-1-op"' in body and ">is one of<" in body
    assert f'<option value="{sev1.id}"' in body and ">Critical<" in body
    assert "multiple" not in body
    multi = client.get(
        f"/automations/condition-value?i=1&cond-1-field=system&cond-1-op=in&cond-1-value={api.id}"
    ).text
    assert "multiple" in multi and f'<option value="{api.id}" selected>Payments API' in multi


def test_condition_value_widgets_for_other_fields(client, db_session):
    _login_admin(client, db_session)
    priv = client.get("/automations/condition-value?i=0&cond-0-field=is_private").text
    assert 'type="checkbox" name="cond-0-value"' in priv
    status = client.get("/automations/condition-value?i=0&cond-0-field=status").text
    assert '<option value="firing"' in status and ">Resolved<" in status
    source = client.get("/automations/condition-value?i=2&cond-2-field=source").text
    assert '<input name="cond-2-value"' in source
    empty = client.get("/automations/condition-value?i=0&cond-0-field=system").text
    assert "Nothing to choose from yet" in empty
