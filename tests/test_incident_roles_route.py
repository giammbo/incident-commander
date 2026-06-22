import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import Role, SeverityLevel, User
from app.services import roles, statuses
from app.services.incidents import create_incident
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _login(client, db_session, email, role):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(db_session, email=email, name="U", role=role, password="pw-123456")
    db_session.flush()
    client.post("/login", data={"email": email, "password": "pw-123456"})


def _incident(db_session):
    statuses.seed_status_levels(db_session)
    roles.seed_incident_role_types(db_session)
    db_session.flush()
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db_session.add(lvl)
    db_session.flush()
    inc = create_incident(
        db_session, title="X", severity_level_id=lvl.id, is_private=False, created_by=1
    )
    db_session.flush()
    return inc


def test_readonly_cannot_set_roles(client, db_session):
    _login(client, db_session, "ro@x.io", Role.read_only)
    assert (
        client.post(
            "/incidents/1/roles", data={"role_type_id": "1"}, follow_redirects=False
        ).status_code
        == 403
    )


def test_ic_assigns_and_fires_updated(client, db_session, monkeypatch):
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    inc = _incident(db_session)
    lead = roles.list_incident_role_types(db_session)[0]
    me = db_session.scalar(select(User).where(User.email == "ic@x.io"))
    events = []
    monkeypatch.setattr(
        "app.routers.incidents.webhooks.notify", lambda *a, **k: events.append(a[2])
    )
    r = client.post(
        f"/incidents/{inc.id}/roles",
        data={"role_type_id": str(lead.id), "user_ids": [str(me.id)]},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert events == ["updated"]
    assert me.id in [u.id for u in roles.assignments_by_role(inc).get(lead.id, [])]


def test_unknown_role_flashes_not_500(client, db_session):
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    inc = _incident(db_session)
    r = client.post(
        f"/incidents/{inc.id}/roles", data={"role_type_id": "999999"}, follow_redirects=False
    )
    assert r.status_code == 303
