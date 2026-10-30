"""Settings/automation acceptance: real Postgres, no external delivery."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import (
    CustomFieldDef,
    Group,
    Incident,
    IncidentCustomFieldValue,
    IncidentType,
    Role,
    SeverityLevel,
    StatusLevel,
    Team,
    Webhook,
    WorkflowRule,
)
from app.services import automation
from app.services.users import bootstrap_admin
from app.settings_store import app_settings, google_settings, sso_settings


@pytest.fixture
def admin(db_session, get_db_override):
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override
    client = TestClient(app)
    _, password = bootstrap_admin(db_session, "admin@localhost")
    db_session.commit()
    client.post("/login", data={"email": "admin@localhost", "password": password})
    client.post(
        "/account/password",
        data={"new_password": "Admin-123", "confirm": "Admin-123", "name": "Admin"},
    )
    return client


@pytest.mark.parametrize(
    "kind,model,data,changed",
    [
        (
            "severity",
            SeverityLevel,
            {"label": "P1", "color": "#ff0000", "rank": "1"},
            {"label": "Critical", "color": "#112233", "rank": "2"},
        ),
        (
            "statuses",
            StatusLevel,
            {"label": "Working", "category": "active", "rank": "1"},
            {"label": "Responding", "category": "active", "rank": "3"},
        ),
        (
            "incident-types",
            IncidentType,
            {"label": "Outage", "rank": "1"},
            {"label": "Service outage", "description": "Edited", "rank": "4"},
        ),
        (
            "roles",
            __import__("app.models", fromlist=["IncidentRoleType"]).IncidentRoleType,
            {"label": "Lead", "rank": "1"},
            {"label": "Commander", "rank": "4"},
        ),
        ("teams", Team, {"name": "Ops"}, {"name": "Platform", "description": "Edited"}),
        (
            "custom-fields",
            CustomFieldDef,
            {"label": "Region", "field_type": "text"},
            {"label": "Affected region", "field_type": "text", "rank": "2"},
        ),
    ],
)
def test_edit_catalog_preserves_identity(admin, db_session, kind, model, data, changed):
    admin.post(f"/settings/{kind}", data=data)
    row = db_session.scalar(select(model))
    row_id = row.id
    response = admin.post(f"/settings/{kind}/{row_id}/edit", data=changed, follow_redirects=False)
    assert response.status_code == 303
    db_session.refresh(row)
    assert row.id == row_id
    assert getattr(row, "name" if kind == "teams" else "label") == changed.get(
        "name", changed.get("label")
    )
    assert response.headers["location"].endswith(f"#{kind}")


def test_edit_reference_and_fixed_allowlist(admin, db_session):
    sev = SeverityLevel(label="P1", color="#ff0000", rank=1)
    db_session.add(sev)
    db_session.flush()
    typ = IncidentType(label="Outage", default_severity_level_id=sev.id)
    db_session.add(typ)
    db_session.commit()
    response = admin.post(
        f"/settings/severity/{sev.id}/edit",
        data={
            "label": "Critical",
            "color": "#123456",
            "rank": "1",
            "id": "777",
            "created_by": "777",
        },
    )
    assert response.status_code == 200
    db_session.refresh(sev)
    db_session.refresh(typ)
    assert typ.default_severity_level_id == sev.id and sev.id != 777
    assert sev.label == "Critical"


@pytest.mark.parametrize(
    "path,data",
    [
        ("severity", {"label": " ", "color": "#fff", "rank": "1"}),
        ("severity", {"label": "X", "color": "red;display:none", "rank": "1"}),
        ("statuses", {"label": "x" * 41, "category": "active"}),
        ("incident-types", {"label": "X", "default_severity_level_id": "99999"}),
        ("teams", {"name": "x" * 81}),
        ("roles", {"label": "Lead", "rank": "oops"}),
        ("custom-fields", {"label": "Region", "field_type": "select", "options": ""}),
    ],
)
def test_invalid_catalog_does_not_mutate_and_retains_form(admin, db_session, path, data):
    response = admin.post(f"/settings/{path}", data=data)
    assert 'role="alert"' in response.text
    assert "settings-draft" in response.text
    assert f'id="{path}"' in response.text


def test_group_protection_and_edit(admin, db_session):
    protected = db_session.scalar(select(Group).where(Group.name == "Admins"))
    response = admin.post(
        f"/groups/{protected.id}/edit", data={"name": "Renamed", "role": "read_only"}
    )
    assert "protected" in response.text.lower()
    db_session.refresh(protected)
    assert protected.name == "Admins" and protected.role == Role.admin
    admin.post("/groups", data={"name": "Responders", "role": "read_only"})
    group = db_session.scalar(select(Group).where(Group.name == "Responders"))
    assert (
        admin.post(
            f"/groups/{group.id}/edit",
            data={"name": "Response", "role": "incident_commander"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    db_session.refresh(group)
    assert group.name == "Response" and group.role == Role.incident_commander


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        '{"type":"user"}',
        '{"type":"service_account","client_email":"bot@example.com","private_key":"bad"}',
    ],
)
def test_google_invalid_structural_config_is_rejected(admin, db_session, payload):
    response = admin.post(
        "/settings/google",
        data={
            "service_account_json": payload,
            "impersonate_email": "ops@example.com",
            "enabled": "true",
        },
    )
    assert 'role="alert"' in response.text
    assert not google_settings(db_session).enabled
    assert not google_settings(db_session).service_account_json
    assert payload not in response.text if payload != "[]" else True
    assert "ops@example.com" in response.text


def test_incomplete_sso_rejected_preserves_secret(admin, db_session):
    s = sso_settings(db_session)
    s.client_secret = "private-client-secret"
    db_session.commit()
    response = admin.post(
        "/settings/sso",
        data={
            "sso_enabled": "true",
            "issuer": "javascript:alert(1)",
            "client_id": "client",
            "display_name": "Company",
        },
    )
    assert 'role="alert"' in response.text
    assert not sso_settings(db_session).sso_enabled
    assert sso_settings(db_session).client_secret == "private-client-secret"
    assert "private-client-secret" not in response.text
    assert "Company" in response.text


@pytest.mark.parametrize(
    "template", ["inc-{unknown}", "inc-{slug!r}", "inc-{date", "inc-{slug.__class__}"]
)
def test_channel_template_invalid_rejected(admin, db_session, template):
    before = app_settings(db_session).slack_channel_name_template
    response = admin.post("/settings/app", data={"slack_channel_name_template": template})
    assert response.status_code == 422
    assert 'role="alert"' in response.text
    assert app_settings(db_session).slack_channel_name_template == before


def test_webhook_edit_toggle_preview_preserves_and_masks_secrets(admin, db_session, monkeypatch):
    import app.services.webhooks as hooks

    monkeypatch.setattr(
        hooks, "_http_post", lambda *a, **k: pytest.fail("No outbound requests in Settings")
    )
    url = "https://username:password@example.com/hooks/private-token?token=query-secret"
    admin.post(
        "/settings/webhooks", data={"name": "Receiver", "url": url, "secret": "signing-secret"}
    )
    hook = db_session.scalar(select(Webhook))
    assert hook is not None
    html = admin.get("/settings").text
    for secret in [
        "username:password",
        "password@example",
        "private-token",
        "query-secret",
        "signing-secret",
    ]:
        assert secret not in html
    response = admin.post(
        f"/settings/webhooks/{hook.id}/edit",
        data={"name": "Edited", "url": "", "secret": "", "format": "teams"},
    )
    assert response.status_code == 200
    db_session.refresh(hook)
    assert hook.name == "Edited" and hook.url == url and hook.secret == "signing-secret"
    admin.post(f"/settings/webhooks/{hook.id}/toggle")
    db_session.refresh(hook)
    assert not hook.enabled
    preview = admin.post(f"/settings/webhooks/{hook.id}/preview")
    assert "Local preview" in preview.text and "AdaptiveCard" in preview.text
    assert "query-secret" not in preview.text and "signing-secret" not in preview.text


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///tmp/key",
        "http://",
        "https://example.com/\nheader",
        "https://example.com:bad",
    ],
)
def test_invalid_webhook_url_rejected(admin, db_session, url):
    response = admin.post("/settings/webhooks", data={"name": "Invalid", "url": url})
    assert db_session.scalar(select(Webhook)) is None
    assert 'role="alert"' in response.text


def test_default_and_in_use_deletion_are_coherent(admin, db_session):
    sev = SeverityLevel(label="Default", color="#123456", is_default=True)
    db_session.add(sev)
    db_session.commit()
    response = admin.post(f"/settings/severity/{sev.id}/delete")
    assert "default" in response.text.lower()
    assert db_session.get(SeverityLevel, sev.id) is not None
    fd = CustomFieldDef(label="Region", field_type="text")
    inc = Incident(title="Sample")
    db_session.add_all([fd, inc])
    db_session.flush()
    db_session.add(IncidentCustomFieldValue(field_id=fd.id, incident_id=inc.id, value="EU"))
    db_session.commit()
    response = admin.post(
        f"/settings/custom-fields/{fd.id}/edit", data={"label": "Region", "field_type": "number"}
    )
    db_session.refresh(fd)
    assert fd.field_type == "text" and "in use" in response.text.lower()


def test_rule_requires_action(admin, db_session):
    response = admin.post(
        "/automations", data={"name": "Empty action", "trigger": "incident.opened"}
    )
    assert db_session.scalar(select(WorkflowRule)) is None
    assert "action" in response.text.lower()
    rule = WorkflowRule(
        name="Legacy", trigger="incident.opened", conditions=[], actions=[], enabled=False
    )
    db_session.add(rule)
    db_session.commit()
    admin.post(f"/automations/{rule.id}/toggle")
    db_session.refresh(rule)
    assert not rule.enabled


def test_automation_edit_dry_run_and_safe_actual_outcome(admin, db_session, monkeypatch):
    admin.post(
        "/automations",
        data={
            "name": "Rule",
            "trigger": "alert.received",
            "cond-0-field": "source",
            "cond-0-op": "equals",
            "cond-0-value": "monitor",
            "action_type": "create_incident",
        },
    )
    rule = db_session.scalar(select(WorkflowRule))
    response = admin.post(
        f"/automations/{rule.id}/edit",
        data={
            "name": "Edited",
            "trigger": "alert.received",
            "cond-0-field": "source",
            "cond-0-value": "monitor",
            "action_type": "create_incident",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    db_session.refresh(rule)
    assert rule.name == "Edited"
    monkeypatch.setattr(
        automation, "_execute_action", lambda *a, **k: pytest.fail("Dry-run executed an action")
    )
    preview = admin.post(
        f"/automations/{rule.id}/preview",
        data={"sample_source": "different", "sample_status": "firing"},
    )
    assert "does not match" in preview.text.lower() and "monitor" in preview.text
    assert rule.last_run_at is None

    def broken(*args, **kwargs):
        raise ValueError("secret-provider-token-private-body")

    monkeypatch.setattr(automation, "_execute_action", broken)
    automation.run_rules(
        db_session,
        trigger="alert.received",
        alert=SimpleNamespace(source="monitor", incident_id=None, title="Private subject"),
    )
    db_session.commit()
    db_session.refresh(rule)
    assert rule.last_run_at is not None
    assert rule.last_run_outcome == "failed"
    assert "secret-provider" not in rule.last_run_summary
    html = admin.get("/automations").text
    assert "failed" in html and "secret-provider" not in html and "Private subject" not in html


def test_large_settings_draft_stays_in_response_not_session(admin, db_session):
    import json
    import re

    options = "\n".join(f"region-{i}-" + "x" * 60 for i in range(100))
    response = admin.post(
        "/settings/custom-fields",
        data={"label": "Large draft", "field_type": "select", "options": options, "rank": "oops"},
    )
    assert response.status_code == 422
    draft = json.loads(
        re.search(
            r'<script id="settings-draft" type="application/json">(.*?)</script>',
            response.text,
            re.S,
        ).group(1)
    )
    assert draft["values"]["options"] == [options]
    assert len(response.headers.get("set-cookie", "")) < 1500
    assert admin.get("/settings").status_code == 200
    assert db_session.scalar(select(CustomFieldDef)) is None


def test_webhook_failure_logs_do_not_disclose_destination(caplog):
    import httpx

    from app.services.webhooks import Delivery, deliver_one

    url = "https://private-user:private-pass@example.com/private-token?token=private-query"

    def unavailable(*args, **kwargs):
        raise httpx.ConnectError("Cannot connect to " + url)

    assert not deliver_one(
        Delivery(name="Receiver", url=url, body=b"{}"), post=unavailable, sleep=lambda _: None
    )
    assert "private-user" not in caplog.text
    assert "private-token" not in caplog.text


def test_partial_database_action_failure_is_recorded_without_poisoning(db_session, monkeypatch):
    from sqlalchemy import text

    rule = WorkflowRule(
        name="Partial",
        trigger="alert.received",
        conditions=[],
        actions=[{"type": "create_incident"}, {"type": "create_incident"}],
    )
    db_session.add(rule)
    db_session.flush()
    count = 0

    def execute(db, action, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            db.execute(text("SELECT 1 / 0"))

    monkeypatch.setattr(automation, "_execute_action", execute)
    automation.run_rules(
        db_session, trigger="alert.received", alert=SimpleNamespace(incident_id=None)
    )
    db_session.commit()
    db_session.refresh(rule)
    assert rule.last_run_outcome == "partial"
    assert "1 action(s) completed; 1 failed" in rule.last_run_summary


def test_edit_multiple_actions_preserves_other_action(admin, db_session):
    rule = WorkflowRule(
        name="Legacy",
        trigger="incident.opened",
        conditions=[],
        actions=[
            {"type": "post_update", "params": {"message": "original"}},
            {"type": "create_followup", "params": {"title": "Kept"}},
        ],
    )
    db_session.add(rule)
    db_session.commit()
    page = admin.get(f"/automations/{rule.id}/edit")
    assert page.status_code == 200 and "original" in page.text
    admin.post(
        f"/automations/{rule.id}/edit",
        data={
            "name": "Edited",
            "trigger": "incident.opened",
            "action_index": "0",
            "action_type": "post_update",
            "ap_message": "Updated",
        },
    )
    db_session.refresh(rule)
    assert rule.actions[0]["params"]["message"] == "Updated"
    assert rule.actions[1]["params"]["title"] == "Kept"


def test_slack_automation_without_workspace_records_failure(db_session):
    incident = Incident(title="Synthetic")
    rule = WorkflowRule(
        name="Slack",
        trigger="incident.opened",
        conditions=[],
        actions=[{"type": "open_slack_channel", "params": {}}],
    )
    db_session.add_all([incident, rule])
    db_session.flush()
    automation.run_rules(db_session, trigger="incident.opened", incident=incident)
    db_session.commit()
    db_session.refresh(rule)
    assert rule.last_run_outcome == "failed"


@pytest.mark.parametrize(
    "path,data",
    [
        (
            "smtp",
            {
                "host": "smtp.example.com",
                "port": "invalid",
                "username": "kept-user",
                "password": "never-echo-this",
                "from_address": "bot@example.com",
            },
        ),
        ("slack", {"enabled": "true", "client_id": "kept-client"}),
        (
            "oncall",
            {
                "twilio_account_sid": "x" * 65,
                "twilio_from_number": "+15551234567",
                "twilio_auth_token": "never-echo-this",
            },
        ),
    ],
)
def test_other_settings_invalid_saves_recover_without_secrets(admin, path, data):
    response = admin.post(f"/settings/{path}", data=data)
    assert response.status_code == 422
    assert 'role="alert"' in response.text
    assert "never-echo-this" not in response.text
    assert "settings-draft" in response.text


def test_deletion_blocks_legacy_string_rule_reference(admin, db_session):
    sev = SeverityLevel(label="Used", color="#123456")
    db_session.add(sev)
    db_session.flush()
    db_session.add(
        WorkflowRule(
            name="Legacy",
            trigger="incident.opened",
            conditions=[{"field": "severity", "op": "equals", "value": str(sev.id)}],
            actions=[],
        )
    )
    db_session.commit()
    response = admin.post(f"/settings/severity/{sev.id}/delete")
    assert response.status_code == 422
    assert db_session.get(SeverityLevel, sev.id) is not None


def test_inbound_mapping_failure_keeps_form_and_reports_error(admin, db_session):
    from app.models import InboundIntegration

    integration = InboundIntegration(
        name="Synthetic", kind="generic", token="synthetic-token", settings={"dedup_key": "kept.id"}
    )
    db_session.add(integration)
    db_session.commit()
    response = admin.post(
        f"/settings/integrations/{integration.id}/settings",
        data={"dedup_key": " ", "title": "kept.title"},
    )
    assert response.status_code == 422
    assert "settings-draft" in response.text and "kept.title" in response.text
    db_session.refresh(integration)
    assert integration.settings == {"dedup_key": "kept.id"}


def test_invalid_action_reference_keeps_visible_action_fields(admin, db_session):
    response = admin.post(
        "/automations",
        data={
            "name": "Kept rule",
            "trigger": "incident.opened",
            "action_type": "create_followup",
            "ap_title": "Kept follow-up",
            "ap_assignee": "oops",
            "cond-0-field": "is_private",
            "cond-0-value": "true",
        },
    )
    assert response.status_code == 422
    assert 'name="ap_title" value="Kept follow-up"' in response.text
    assert 'value="create_followup" selected' in response.text
    assert db_session.scalar(select(WorkflowRule)) is None


def _rendered_rule_form(html):
    """Extract server-rendered controls; the separate Chromium artifact executes JavaScript."""
    from html.parser import HTMLParser

    class Controls(HTMLParser):
        def __init__(self):
            super().__init__()
            self.in_form = False
            self.fields = {}
            self.select = None
            self.options = []
            self.choices = {}
            self.textarea = None
            self.text = ""

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "form" and "rule-editor" in a.get("class", ""):
                self.in_form = True
            if not self.in_form:
                return
            name = a.get("name")
            if tag == "input" and name:
                self.fields[name] = (
                    []
                    if a.get("type") == "checkbox" and "checked" not in a
                    else [a.get("value", "")]
                )
            if tag == "select" and name:
                self.select = (name, "multiple" in a)
                self.options = []
            if tag == "option" and self.select:
                self.options.append((a.get("value", ""), "selected" in a))
            if tag == "textarea" and name:
                self.textarea, self.text = name, ""

        def handle_data(self, data):
            if self.textarea:
                self.text += data

        def handle_endtag(self, tag):
            if tag == "form":
                self.in_form = False
            if tag == "select" and self.select:
                name, multiple = self.select
                selected = [value for value, chosen in self.options if chosen]
                self.fields[name] = (
                    selected
                    if multiple or selected
                    else [self.options[0][0]]
                    if self.options
                    else []
                )
                self.choices[name] = {value for value, _ in self.options}
                self.select = None
            if tag == "textarea" and self.textarea:
                self.fields[self.textarea] = [self.text]
                self.textarea = None

    parsed = Controls()
    parsed.feed(html)
    return parsed.fields


@pytest.mark.parametrize("mode", ["text", "multi_boolean", "multi_alias", "malformed"])
def test_failed_rule_save_corrected_browser_resubmit_retains_sparse_conditions(
    admin, db_session, mode
):
    if mode == "text":
        form = {
            "name": " ",
            "trigger": "alert.received",
            "cond-0-field": "",
            "cond-1-field": "source",
            "cond-1-op": "equals",
            "cond-1-value": "prod",
            "action_type": "create_incident",
        }
        expected = [{"field": "source", "op": "equals", "value": "prod"}]
    else:
        first = SeverityLevel(label="P1", color="#123456")
        second = SeverityLevel(label="P2", color="#234567")
        db_session.add_all([first, second])
        db_session.commit()
        form = {
            "name": " ",
            "trigger": "incident.opened",
            "cond-0-field": "",
            "cond-1-field": "severity",
            "cond-1-op": "in",
            "cond-1-value": [str(first.id), str(second.id)],
            "cond-2-field": "is_private",
            "cond-2-op": "equals",
            "cond-2-value": "true",
            "action_type": "post_update",
            "ap_message": "kept message",
        }
        expected = [
            {"field": "severity", "op": "in", "value": [first.id, second.id]},
            {"field": "is_private", "op": "equals", "value": True},
        ]
        if mode == "multi_alias":
            form["cond-2-value"] = "YES"
        if mode == "malformed":
            form["cond-1-value"] = [str(first.id), "not-an-id"]
            expected[0]["value"] = [first.id, second.id]
    rejected = admin.post("/automations", data=form)
    assert rejected.status_code == 422
    browser = _rendered_rule_form(rejected.text)
    assert browser["cond-1-field"] == [form["cond-1-field"]]
    submitted_values = (
        form["cond-1-value"] if isinstance(form["cond-1-value"], list) else [form["cond-1-value"]]
    )
    assert set(browser["cond-1-value"]) == set(submitted_values)
    browser["name"] = ["Corrected"]
    if mode == "malformed":
        browser["cond-1-value"] = [str(first.id), str(second.id)]
    saved = admin.post("/automations", data=browser, follow_redirects=False)
    assert saved.status_code == 303
    rule = db_session.scalar(select(WorkflowRule))
    assert rule.conditions == expected


@pytest.mark.parametrize("field", ["severity", "type", "system"])
@pytest.mark.parametrize("op", ["equals", "in"])
def test_rule_rejects_missing_catalog_condition_with_bound_row(admin, db_session, field, op):
    response = admin.post(
        "/automations",
        data={
            "name": "Dangling",
            "trigger": "incident.opened",
            "cond-1-field": field,
            "cond-1-op": op,
            "cond-1-value": "987654321",
            "action_type": "post_update",
            "ap_message": "kept",
        },
    )
    assert response.status_code == 422
    assert db_session.scalar(select(WorkflowRule)) is None
    bound = _rendered_rule_form(response.text)
    assert bound["cond-1-field"] == [field]
    assert bound["cond-1-value"] == ["987654321"]


@pytest.mark.parametrize("field,value", [("status", "unknown"), ("is_private", "not-a-boolean")])
def test_rule_rejects_invalid_fixed_condition_domain(admin, db_session, field, value):
    alert = field == "status"
    response = admin.post(
        "/automations",
        data={
            "name": "Invalid domain",
            "trigger": "alert.received" if alert else "incident.opened",
            "cond-0-field": field,
            "cond-0-op": "equals",
            "cond-0-value": value,
            "action_type": "create_incident" if alert else "post_update",
            "ap_message": "kept",
        },
    )
    assert response.status_code == 422
    assert db_session.scalar(select(WorkflowRule)) is None


def test_legacy_condition_references_validated_on_enable_and_preserved(admin, db_session):
    sev = SeverityLevel(label="P1", color="#123456")
    db_session.add(sev)
    db_session.flush()
    valid = WorkflowRule(
        name="Valid legacy",
        trigger="incident.opened",
        enabled=False,
        conditions=[{"field": "severity", "op": "equals", "value": str(sev.id)}],
        actions=[{"type": "post_update", "params": {"message": "kept"}}],
    )
    stale = WorkflowRule(
        name="Stale legacy",
        trigger="incident.opened",
        enabled=False,
        conditions=[{"field": "severity", "op": "in", "value": [str(sev.id), "987654321"]}],
        actions=valid.actions,
    )
    db_session.add_all([valid, stale])
    db_session.commit()
    original = valid.conditions.copy()
    admin.post(f"/automations/{valid.id}/toggle")
    admin.post(f"/automations/{stale.id}/toggle")
    db_session.refresh(valid)
    db_session.refresh(stale)
    assert valid.enabled and valid.conditions == original
    assert not stale.enabled
    assert automation._matches(valid.conditions, SimpleNamespace(severity_level_id=sev.id))


def test_stale_catalog_edit_recovery_preserves_saved_rule_and_valid_reference(admin, db_session):
    valid = SeverityLevel(label="Current", color="#123456")
    deleted = SeverityLevel(label="Deleted while form was open", color="#234567")
    db_session.add_all([valid, deleted])
    db_session.flush()
    rule = WorkflowRule(
        name="Saved",
        trigger="incident.opened",
        conditions=[{"field": "severity", "op": "equals", "value": valid.id}],
        actions=[{"type": "post_update", "params": {"message": "old"}}],
    )
    db_session.add(rule)
    db_session.commit()
    stale_id = deleted.id
    db_session.delete(deleted)
    db_session.commit()
    rejected = admin.post(
        f"/automations/{rule.id}/edit",
        data={
            "name": "Edited",
            "trigger": "incident.opened",
            "cond-0-field": "",
            "cond-1-field": "severity",
            "cond-1-op": "equals",
            "cond-1-value": str(stale_id),
            "action_type": "post_update",
            "ap_message": "new",
        },
    )
    assert rejected.status_code == 422
    db_session.refresh(rule)
    assert rule.name == "Saved" and rule.conditions[0]["value"] == valid.id
    browser = _rendered_rule_form(rejected.text)
    assert browser["cond-1-value"] == [str(stale_id)]
    browser["cond-1-value"] = [str(valid.id)]
    response = admin.post(f"/automations/{rule.id}/edit", data=browser, follow_redirects=False)
    assert response.status_code == 303
    db_session.refresh(rule)
    assert rule.name == "Edited" and rule.conditions == [
        {"field": "severity", "op": "equals", "value": valid.id}
    ]


@pytest.mark.parametrize("field", ["severity", "type", "system"])
@pytest.mark.parametrize("op", ["equals", "in"])
def test_existing_legacy_catalog_conditions_keep_references_and_match(db_session, field, op):
    from app.models import System

    model, attrs, subject_attr = {
        "severity": (SeverityLevel, {"label": "Existing", "color": "#123456"}, "severity_level_id"),
        "type": (IncidentType, {"label": "Existing"}, "incident_type_id"),
        "system": (System, {"name": "Existing"}, "system_id"),
    }[field]
    record = model(**attrs)
    db_session.add(record)
    db_session.flush()
    original = [
        {"field": field, "op": op, "value": [str(record.id)] if op == "in" else str(record.id)}
    ]
    rule = automation.create_rule(
        db_session,
        name="Valid legacy reference",
        trigger="incident.opened",
        conditions=original,
        actions=[{"type": "post_update", "params": {"message": "kept"}}],
    )
    db_session.commit()
    db_session.refresh(rule)
    assert rule.conditions == original
    assert automation._matches(rule.conditions, SimpleNamespace(**{subject_attr: record.id}))
