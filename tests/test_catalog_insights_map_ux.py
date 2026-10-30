from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import Role, StatusCategory, StatusLevel, System
from app.services import statuses
from app.services.catalog import create_component, create_system, update_component, update_system
from app.services.followups import create_followup
from app.services.incidents import create_incident
from app.services.insights import compute_insights, window_since
from app.services.service_map import build_graph
from app.services.teams import create_team
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def actor(db_session):
    bootstrap_admin(db_session, "admin@localhost")
    return create_user(
        db_session,
        email="catalog@example.test",
        name="Catalog operator",
        role=Role.incident_commander,
        password="pw-123456",
    )


@pytest.fixture
def client(db_session, get_db_override, actor):
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override
    client = TestClient(app)
    client.post("/login", data={"email": actor.email, "password": "pw-123456"})
    return client


@pytest.mark.parametrize("kind", ["system", "component"])
def test_catalog_name_boundary_is_validated_before_database(db_session, kind):
    system = create_system(db_session, name="Parent")
    if kind == "system":
        item = create_system(db_session, name="a" * 120)
        with pytest.raises(ValueError, match="120"):
            update_system(db_session, item, name="b" * 121)
        assert item.name == "a" * 120
        with pytest.raises(ValueError, match="120"):
            create_system(db_session, name="c" * 121)
    else:
        item = create_component(db_session, name="a" * 120, system_id=system.id)
        with pytest.raises(ValueError, match="120"):
            update_component(
                db_session, item, name="b" * 121, description=None, system_id=system.id
            )
        assert item.name == "a" * 120
        with pytest.raises(ValueError, match="120"):
            create_component(db_session, name="c" * 121, system_id=system.id)


@pytest.mark.parametrize("path", ["/systems", "/components"])
def test_catalog_invalid_create_retains_draft(client, db_session, path):
    system = create_system(db_session, name="Parent")
    db_session.commit()
    response = client.post(
        path,
        data={"name": "   ", "description": "Keep this draft", "system_id": system.id},
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert "Keep this draft" in response.text
    assert "name is required" in response.text


def test_catalog_invalid_system_edit_retains_draft(client, db_session):
    system = create_system(db_session, name="Original")
    db_session.commit()
    response = client.post(
        f"/systems/{system.id}/edit",
        data={"name": " ", "description": "Retained edit"},
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert "Retained edit" in response.text
    assert db_session.get(System, system.id).name == "Original"


def test_catalog_search_filters_and_pagination(client, db_session):
    for n in range(23):
        create_system(db_session, name=f"System {n:02}")
    first = client.get("/systems")
    assert "System 00" in first.text and "System 22" not in first.text
    assert "System 22" in client.get("/systems?page=2").text
    page = client.get("/systems?q=System+22")
    assert "System 22" in page.text and "System 00" not in page.text
    parent = db_session.scalar(select(System).where(System.name == "System 00"))
    other = db_session.scalar(select(System).where(System.name == "System 01"))
    component = create_component(db_session, name="Matched component", system_id=parent.id)
    create_component(db_session, name="Different component", system_id=other.id)
    page = client.get(f"/components?system_id={parent.id}")
    assert f'href="/components/{component.id}"' in page.text
    assert "Different component" not in page.text


def test_insights_current_backlog_includes_old_visible_incidents(db_session, actor):
    old = create_incident(
        db_session, title="Old open", severity_level_id=None, is_private=False, created_by=actor.id
    )
    old.created_at = datetime.now(UTC) - timedelta(days=120)
    create_followup(db_session, old, title="Old work", created_by=actor.id)
    recent = create_incident(
        db_session,
        title="Recently reopened",
        severity_level_id=None,
        is_private=False,
        created_by=actor.id,
    )
    recent.closed_at = datetime.now(UTC)
    outsider = create_user(
        db_session,
        email="private@example.test",
        name="Private author",
        role=Role.read_only,
        password="pw-123456",
    )
    create_incident(
        db_session, title="Hidden", severity_level_id=None, is_private=True, created_by=outsider.id
    )
    db_session.flush()
    data = compute_insights(db_session, since=window_since(30), user=actor)
    assert data["total"] == 1
    assert data["current_open"] == 2
    assert data["current_open_followups"] == 1
    assert data["mttr"] is None  # A reopened incident is not a closed sample.
    assert data["mttr_count"] == 0


def test_map_fallback_contains_state_relations_and_authorized_links(client, db_session, actor):
    system = create_system(db_session, name="Billing")
    component = create_component(db_session, name="Invoice API", system_id=system.id)
    visible = create_incident(
        db_session,
        title="Public outage",
        severity_level_id=None,
        is_private=False,
        created_by=actor.id,
        system_id=system.id,
    )
    outsider = create_user(
        db_session,
        email="hidden@example.test",
        name="Hidden",
        role=Role.read_only,
        password="pw-123456",
    )
    hidden = create_incident(
        db_session,
        title="Private outage",
        severity_level_id=None,
        is_private=True,
        created_by=outsider.id,
        system_id=system.id,
    )
    db_session.flush()
    graph = build_graph(db_session, user=actor)
    node = next(n for n in graph["nodes"] if n["id"] == f"sys-{system.id}")
    assert node["url"] == f"/systems/{system.id}"
    assert [i["id"] for i in node["incidents"]] == [visible.id]
    page = client.get("/maps")
    assert f'href="/components/{component.id}"' in page.text
    assert "Contains" in page.text and "down" in page.text
    assert f'href="/incidents/{visible.id}"' in page.text
    assert hidden.title not in page.text
    response = client.get("/maps/graph.json")
    assert hidden.title not in response.text
    assert visible.title in response.text
    assert response.headers["cache-control"] == "no-store"
    detail = client.get(f"/systems/{system.id}")
    assert visible.title in detail.text
    assert hidden.title not in detail.text
    assert graph["updated_at"]


@pytest.mark.parametrize("path", ["/systems?q=&team_id=", "/components?q=&team_id=&system_id="])
def test_catalog_blank_filters_render_results(client, db_session, path):
    system = create_system(db_session, name="All systems result")
    create_component(db_session, name="All components result", system_id=system.id)
    response = client.get(path)
    assert response.status_code == 200
    assert "All systems result" in response.text


@pytest.mark.parametrize("kind", ["systems", "components"])
def test_catalog_edit_long_name_retains_all_fields(client, db_session, kind):
    system = create_system(db_session, name="Original system")
    component = create_component(db_session, name="Original component", system_id=system.id)
    item = system if kind == "systems" else component
    db_session.commit()
    long_name = "n" * 121
    response = client.post(
        f"/{kind}/{item.id}/edit",
        data={"name": long_name, "description": "Long edit draft", "system_id": system.id},
    )
    assert response.status_code == 422
    assert long_name in response.text
    assert "Long edit draft" in response.text
    assert "120 characters" in response.text
    db_session.refresh(item)
    assert item.name == ("Original system" if kind == "systems" else "Original component")


def test_component_without_parent_retains_rejected_draft(client):
    response = client.post(
        "/components",
        data={
            "name": "Draft component",
            "description": "Keep parentless draft",
            "system_id": "999",
        },
    )
    assert response.status_code == 422
    assert "Keep parentless draft" in response.text
    assert "Draft component" in response.text
    assert "Unknown system" in response.text


def test_insights_mttr_uses_valid_closed_cohort_and_current_system_owner(db_session, actor):
    statuses.seed_status_levels(db_session)
    closed = db_session.scalar(
        select(StatusLevel).where(StatusLevel.category == StatusCategory.closed)
    )
    old_owner = create_team(db_session, name="Previous owner")
    new_owner = create_team(db_session, name="Current owner")
    system = create_system(db_session, name="Team-owned system", owner_team_id=old_owner.id)
    now = datetime.now(UTC)
    eligible = create_incident(
        db_session,
        title="Valid closure",
        severity_level_id=None,
        is_private=False,
        created_by=actor.id,
        system_id=system.id,
    )
    eligible.created_at = now - timedelta(hours=4)
    eligible.closed_at = now - timedelta(hours=2)
    eligible.status_id = closed.id
    invalid = create_incident(
        db_session,
        title="Bad closure timestamp",
        severity_level_id=None,
        is_private=False,
        created_by=actor.id,
    )
    invalid.created_at = now - timedelta(hours=1)
    invalid.closed_at = now - timedelta(hours=3)
    invalid.status_id = closed.id
    old = create_incident(
        db_session,
        title="Outside cohort",
        severity_level_id=None,
        is_private=False,
        created_by=actor.id,
    )
    old.created_at = now - timedelta(days=120)
    old.closed_at = now
    old.status_id = closed.id
    create_followup(db_session, old, title="Work from closed incident", created_by=actor.id)
    system.owner_team_id = new_owner.id
    db_session.flush()
    db_session.expire_all()
    data = compute_insights(db_session, since=now - timedelta(days=30), user=actor)
    assert data["total"] == 2
    assert data["closed"] == 2
    assert data["mttr"] == "2h"
    assert data["mttr_count"] == 1
    assert data["current_open_followups"] == 1
    assert {r["label"]: r["count"] for r in data["by_team"]} == {"Current owner": 1, "—": 1}
