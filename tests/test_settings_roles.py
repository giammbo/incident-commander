import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import IncidentRoleType, SeverityLevel, User
from app.services.users import bootstrap_admin


@pytest.fixture
def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _admin(client, db_session):
    _, pw = bootstrap_admin(db_session, "admin@localhost")
    db_session.flush()
    client.post("/login", data={"email": "admin@localhost", "password": pw})
    client.post("/account/password", data={"new_password": "Admin-123", "confirm": "Admin-123"})


def test_admin_creates_role(client, db_session):
    _admin(client, db_session)
    client.post("/settings/roles", data={"label": "Operations", "rank": "5"})
    roles = list(
        db_session.scalars(select(IncidentRoleType).where(IncidentRoleType.label == "Operations"))
    )
    assert len(roles) == 1 and roles[0].rank == 5


def test_admin_deletes_unused_role(client, db_session):
    _admin(client, db_session)
    rt = IncidentRoleType(label="Temp", rank=9)
    db_session.add(rt)
    db_session.flush()
    r = client.post(f"/settings/roles/{rt.id}/delete", follow_redirects=False)
    assert r.status_code == 303
    remaining = list(
        db_session.scalars(select(IncidentRoleType).where(IncidentRoleType.label == "Temp"))
    )
    assert remaining == []


def test_delete_in_use_flashes(client, db_session):
    from app.services import roles, statuses
    from app.services.incidents import create_incident, set_incident_role_assignees

    _admin(client, db_session)
    roles.seed_incident_role_types(db_session)
    statuses.seed_status_levels(db_session)
    db_session.flush()

    lvl = SeverityLevel(label="High", color="#ff0000", rank=1, is_default=True)
    db_session.add(lvl)
    db_session.flush()

    inc = create_incident(
        db_session,
        title="X",
        severity_level_id=lvl.id,
        is_private=False,
        created_by=1,
    )

    admin = db_session.scalar(select(User).where(User.email == "admin@localhost"))
    role_types = roles.list_incident_role_types(db_session)
    role_type_id = role_types[0].id

    set_incident_role_assignees(
        db_session,
        inc,
        role_type_id=role_type_id,
        user_ids=[admin.id],
        by_user=admin.id,
    )
    # Commit so the route's rollback (on ValueError) doesn't erase the setup data
    db_session.commit()

    r = client.post(f"/settings/roles/{role_type_id}/delete", follow_redirects=False)
    assert r.status_code == 303

    still_there = db_session.get(IncidentRoleType, role_type_id)
    assert still_there is not None


def test_non_admin_forbidden_roles(client, db_session):
    from app.models import Role
    from app.services.users import create_user

    bootstrap_admin(db_session, "admin@localhost")
    create_user(
        db_session,
        email="ic@x.io",
        name="IC",
        role=Role.incident_commander,
        password="pw-123456",
    )
    db_session.flush()
    client.post("/login", data={"email": "ic@x.io", "password": "pw-123456"})
    assert (
        client.post(
            "/settings/roles",
            data={"label": "Ops", "rank": "1"},
        ).status_code
        == 403
    )
