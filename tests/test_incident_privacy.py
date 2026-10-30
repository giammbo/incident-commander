"""End-to-end privacy boundaries: an IC role alone never grants private access."""

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import get_current_user
from app.db import get_db
from app.main import create_app
from app.models import (
    Alert,
    IncidentEvent,
    IncidentRoleAssignment,
    Postmortem,
    Role,
    SeverityLevel,
    StatusCategory,
    StatusLevel,
    System,
)
from app.services import followups, postmortems, roles, statuses
from app.services.incidents import create_incident
from app.services.users import create_user


@pytest.fixture
def privacy(db_session, get_db_override):
    users = {
        key: create_user(db_session, email=f"{key}@privacy.test", name=key, role=role)
        for key, role in {
            "admin": Role.admin,
            "creator": Role.incident_commander,
            "assigned_ro": Role.read_only,
            "unassigned_ro": Role.read_only,
            "unassigned_ic": Role.incident_commander,
        }.items()
    }
    statuses.seed_status_levels(db_session)
    roles.seed_incident_role_types(db_session)
    level = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1)
    system = System(name="Database cluster")
    db_session.add_all([level, system])
    db_session.flush()
    incidents = {}
    for key, private in (("public", False), ("secret_open", True), ("secret_closed", True)):
        incidents[key] = create_incident(
            db_session,
            title=f"{key} confidential incident",
            severity_level_id=level.id,
            is_private=private,
            created_by=users["creator"].id,
            system_id=system.id if private else None,
        )
        if private:
            db_session.add(
                IncidentRoleAssignment(
                    incident_id=incidents[key].id,
                    role_type_id=roles.list_incident_role_types(db_session)[0].id,
                    user_id=users["assigned_ro"].id,
                )
            )
    incidents["secret_closed"].status_id = db_session.scalar(
        select(StatusLevel.id).where(StatusLevel.category == StatusCategory.closed)
    )
    secret = incidents["secret_open"]
    fu = followups.create_followup(
        db_session, secret, title="Secret remediation", created_by=users["creator"].id
    )
    postmortems.generate(db_session, secret, by_user=users["creator"].id)
    alert = Alert(
        source="generic", dedup_key="private", title="Secret alert", incident_id=secret.id
    )
    public_alert = Alert(source="generic", dedup_key="public", title="Public alert")
    db_session.add_all([alert, public_alert])
    db_session.commit()
    event = db_session.scalar(select(IncidentEvent).where(IncidentEvent.incident_id == secret.id))
    state = {"user": users["unassigned_ic"]}
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override

    def current(request: Request):
        request.state.current_user = state["user"]
        return state["user"]

    app.dependency_overrides[get_current_user] = current
    return {
        "client": TestClient(app),
        "users": users,
        "state": state,
        "incidents": incidents,
        "followup": fu,
        "event": event,
        "alert": alert,
        "public_alert": public_alert,
        "system": system,
        "severity": level,
    }


@pytest.mark.parametrize("actor", ["unassigned_ic", "unassigned_ro"])
@pytest.mark.parametrize(
    "path", ["/incidents/{id}", "/incidents/{id}/postmortem", "/incidents/{id}/postmortem.md"]
)
def test_private_incident_hidden_from_unassigned_user(privacy, actor, path):
    privacy["state"]["user"] = privacy["users"][actor]
    response = privacy["client"].get(path.format(id=privacy["incidents"]["secret_open"].id))
    assert response.status_code == 404
    assert "secret_open" not in response.text


@pytest.mark.parametrize("actor", ["admin", "creator", "assigned_ro"])
def test_assignee_can_view_private_incident(privacy, actor):
    privacy["state"]["user"] = privacy["users"][actor]
    iid = privacy["incidents"]["secret_open"].id
    assert privacy["client"].get(f"/incidents/{iid}").status_code == 200
    response = privacy["client"].get(f"/incidents/{iid}/postmortem.md")
    assert response.status_code == 200 and "secret_open" in response.text


@pytest.mark.parametrize("page", ["/", "/alerts", "/follow-ups", "/postmortems"])
@pytest.mark.parametrize("actor", ["unassigned_ic", "unassigned_ro"])
def test_secondary_lists_and_incident_picker_do_not_leak(privacy, page, actor):
    privacy["state"]["user"] = privacy["users"][actor]
    response = privacy["client"].get(page)
    assert response.status_code == 200
    for hidden in ("secret_open", "secret_closed", "Secret alert", "Secret remediation"):
        assert hidden not in response.text


@pytest.mark.parametrize(
    "actor,total,graph_status",
    [
        ("unassigned_ic", 1, "ok"),
        ("unassigned_ro", 1, "ok"),
        ("admin", 3, "down"),
        ("creator", 3, "down"),
        ("assigned_ro", 3, "down"),
    ],
)
def test_private_incident_absent_from_aggregate_and_export(
    privacy, db_session, actor, total, graph_status
):
    from app.services.insights import compute_insights
    from app.services.service_map import build_graph

    user = privacy["users"][actor]
    privacy["state"]["user"] = user
    insights = compute_insights(db_session, since=None, user=user)
    assert insights["total"] == total
    assert insights["open_followups"] == (0 if total == 1 else 1)
    graph = build_graph(db_session, user=user)
    assert (
        next(n for n in graph["nodes"] if n["id"] == f"sys-{privacy['system'].id}")["status"]
        == graph_status
    )
    route_graph = privacy["client"].get("/maps/graph.json").json()
    # Snapshot freshness is generated on every read; authorization and graph data
    # must remain identical across the service and HTTP entry points.
    assert {key: value for key, value in route_graph.items() if key != "updated_at"} == {
        key: value for key, value in graph.items() if key != "updated_at"
    }
    response = privacy["client"].get("/insights?days=0")
    assert response.context["data"]["total"] == total


def test_shared_visibility_helpers_use_the_same_policy(privacy, db_session):
    from fastapi import HTTPException

    from app.services.incident_access import (
        can_view_incident,
        get_visible_incident,
        visible_incident_ids,
    )

    for actor in privacy["users"]:
        user = privacy["users"][actor]
        permitted = actor in ("admin", "creator", "assigned_ro")
        secret = privacy["incidents"]["secret_open"]
        assert can_view_incident(user, secret) is permitted
        assert (secret.id in visible_incident_ids(db_session, user)) is permitted
        if permitted:
            assert get_visible_incident(db_session, secret.id, user).id == secret.id
        else:
            with pytest.raises(HTTPException) as exc:
                get_visible_incident(db_session, secret.id, user)
            assert exc.value.status_code == 404
        assert can_view_incident(user, privacy["incidents"]["public"])


@pytest.mark.parametrize(
    "action",
    [
        "custom-fields",
        "edit",
        "add-meet",
        "open-slack",
        "close",
        "roles",
        "notes",
        "updates",
        "status",
        "pin",
        "delete-event",
        "followup-create",
        "followup-status",
        "followup-edit",
        "followup-delete",
        "postmortem-generate",
        "postmortem-save",
        "postmortem-regenerate",
        "alert-resolve",
        "alert-declare",
        "alert-attach",
        "attach-to-secret",
    ],
)
def test_unassigned_commander_cannot_mutate_private_secondary_records(privacy, db_session, action):
    iid = privacy["incidents"]["secret_open"].id
    path = f"/incidents/{iid}/{action}"
    data = {
        "title": "Changed",
        "body": "Changed",
        "message": "Changed",
        "severity_level_id": privacy["severity"].id,
        "status_id": privacy["incidents"]["secret_closed"].status_id,
        "video": "google",
        "slack_connection_id": 999,
        "role_type_id": 1,
        "status": "completed",
    }
    if action in ("pin", "delete-event"):
        path = f"/incidents/{iid}/events/{privacy['event'].id}/" + (
            "pin" if action == "pin" else "delete"
        )
    elif action.startswith("followup-"):
        suffix = action.removeprefix("followup-")
        path = (
            f"/incidents/{iid}/followups"
            if suffix == "create"
            else f"/followups/{privacy['followup'].id}/{suffix}"
        )
    elif action.startswith("postmortem-"):
        suffix = action.removeprefix("postmortem-")
        path = f"/incidents/{iid}/postmortem" + ("" if suffix == "save" else f"/{suffix}")
    elif action.startswith("alert-") or action == "attach-to-secret":
        aid = privacy["public_alert"].id if action == "attach-to-secret" else privacy["alert"].id
        suffix = "attach" if action == "attach-to-secret" else action.removeprefix("alert-")
        path = f"/alerts/{aid}/{suffix}"
        data["incident_id"] = (
            iid if action == "attach-to-secret" else privacy["incidents"]["public"].id
        )
    response = privacy["client"].post(path, data=data, follow_redirects=False)
    assert response.status_code == 404
    db_session.refresh(privacy["followup"])
    db_session.refresh(privacy["alert"])
    assert privacy["followup"].title == "Secret remediation"
    assert privacy["followup"].status == "open"
    assert privacy["alert"].status == "firing"
    assert privacy["alert"].incident_id == iid
    assert db_session.get(
        Postmortem, privacy["incidents"]["secret_open"].postmortem.id
    ).body.startswith("# Postmortem: secret_open")


@pytest.mark.parametrize(
    "actor,secret_visible",
    [
        ("unassigned_ic", False),
        ("unassigned_ro", False),
        ("admin", True),
        ("creator", True),
        ("assigned_ro", True),
    ],
)
def test_secondary_service_lists_follow_incident_visibility(
    privacy, db_session, actor, secret_visible
):
    from app.services import alerts
    from app.services.incidents import list_incidents

    user = privacy["users"][actor]
    assert len(list_incidents(db_session, user=user)) == (3 if secret_visible else 1)
    assert len(alerts.list_alerts(db_session, user=user)) == (2 if secret_visible else 1)
    assert len(followups.list_open_followups(db_session, user=user)) == (1 if secret_visible else 0)
    assert len(postmortems.list_incidents_with_postmortem(db_session, user=user)) == (
        1 if secret_visible else 0
    )
    assert len(postmortems.list_closed_incidents_without_postmortem(db_session, user=user)) == (
        1 if secret_visible else 0
    )


def test_board_and_alert_counts_do_not_include_hidden_incidents(privacy):
    response = privacy["client"].get("/alerts")
    assert response.context["firing_count"] == 1
    response = privacy["client"].post(
        f"/incidents/{privacy['incidents']['public'].id}/close",
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert response.context["total_count"] == 1
    assert response.context["open_count"] == 0


def test_inbox_does_not_expose_paging_for_private_incident(privacy, db_session):
    from app.models import Escalation, Urgency

    paging = Escalation(
        source="manual",
        urgency=Urgency.high,
        title="Private page",
        short_code="0123",
        incident_id=privacy["incidents"]["secret_open"].id,
        alert_id=privacy["public_alert"].id,
    )
    db_session.add(paging)
    db_session.commit()
    response = privacy["client"].get("/alerts")
    assert privacy["public_alert"].id not in response.context["paging"]
    privacy["state"]["user"] = privacy["users"]["assigned_ro"]
    response = privacy["client"].get("/alerts")
    assert response.context["paging"][privacy["public_alert"].id].id == paging.id
