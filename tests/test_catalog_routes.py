import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.models import Role
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session, get_db_override):
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override
    return TestClient(app)


def _login(client, db_session, *, role):
    # admin branch uses the proven pattern from tests/test_settings_severity.py::_admin
    _, pw = bootstrap_admin(db_session, "admin@localhost")
    db_session.flush()
    if role == "admin":
        client.post("/login", data={"email": "admin@localhost", "password": pw})
        client.post("/account/password", data={"new_password": "Admin-123", "confirm": "Admin-123"})
        return
    email = {"ic": "ic@x.io", "ro": "ro@x.io"}[role]
    r = {"ic": Role.incident_commander, "ro": Role.read_only}[role]
    create_user(db_session, email=email, name=email, role=r, password="pw-123456")
    db_session.flush()
    client.post("/login", data={"email": email, "password": "pw-123456"})


def test_readonly_can_view_but_not_create_systems(client, db_session):
    _login(client, db_session, role="ro")
    assert client.get("/systems").status_code == 200
    assert client.post("/systems", data={"name": "Billing"}).status_code == 403


def test_ic_can_create_system_and_component(client, db_session):
    _login(client, db_session, role="ic")
    assert (
        client.post("/systems", data={"name": "Billing"}, follow_redirects=False).status_code == 303
    )
    from sqlalchemy import select

    from app.models import System

    s = db_session.scalar(select(System).where(System.name == "Billing"))
    assert s is not None
    r = client.post(
        "/components", data={"name": "Invoicer", "system_id": str(s.id)}, follow_redirects=False
    )
    assert r.status_code == 303


def test_ic_cannot_reach_connections(client, db_session):
    _login(client, db_session, role="ic")
    assert client.get("/connections").status_code == 403


def test_component_options_endpoint_filters_by_system(client, db_session):
    _login(client, db_session, role="ic")
    from app.services.catalog import create_component, create_system

    s1 = create_system(db_session, name="S1")
    s2 = create_system(db_session, name="S2")
    create_component(db_session, name="Alpha", system_id=s1.id)
    create_component(db_session, name="Beta", system_id=s2.id)
    db_session.flush()
    r = client.get(f"/incidents/component-options?system_id={s1.id}")
    assert "Alpha" in r.text and "Beta" not in r.text


def _ab_components(db_session):
    from app.services.catalog import create_component, create_system, set_component_dependencies

    s = create_system(db_session, name="Shop")
    a = create_component(db_session, name="api", system_id=s.id)
    b = create_component(db_session, name="db", system_id=s.id)
    set_component_dependencies(db_session, a, [b.id])
    db_session.flush()
    return a, b


def test_component_cycle_rejected_with_flash(client, db_session):
    _login(client, db_session, role="ic")
    a, b = _ab_components(db_session)
    r = client.post(
        f"/components/{b.id}/deps", data={"depends_on_ids": [str(a.id)]}, follow_redirects=False
    )
    assert r.status_code == 303
    from sqlalchemy import select

    from app.models import component_dependencies

    edges = db_session.execute(
        select(component_dependencies).where(component_dependencies.c.component_id == b.id)
    ).all()
    assert edges == []
    page = client.get("/components").text
    assert "dependency loop: db → api → db" in page


def test_components_page_disables_loop_options_and_shows_used_by(client, db_session):
    _login(client, db_session, role="ic")
    a, b = _ab_components(db_session)
    page = client.get("/components").text
    assert "<strong>db</strong> depends on:" in page
    assert f'value="{a.id}" disabled' in page  # db can't pick api: api already depends on db
    assert "already depends on db" in page
    assert "used by api" in page


def test_system_detail_dependency_picker(client, db_session):
    from app.services.catalog import create_system, set_system_dependencies

    _login(client, db_session, role="ic")
    s1 = create_system(db_session, name="Front")
    s2 = create_system(db_session, name="Back")
    set_system_dependencies(db_session, s1, [s2.id])
    db_session.flush()
    page = client.get(f"/systems/{s2.id}").text
    assert "used by Front" in page
    assert f'value="{s1.id}" disabled' in page
