import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.models import Role, SeverityLevel
from app.services import incident_types as itypes
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


def test_type_options_htmx_reflects_type_default(client, db_session):
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    sev = SeverityLevel(label="SEV-TYPE", color="#FF5D5D", rank=1, is_default=True)
    db_session.add(sev)
    db_session.flush()
    itypes.seed_incident_types(db_session)
    db_session.flush()
    t = itypes.list_incident_types(db_session)[0]
    t.default_severity_level_id = sev.id
    db_session.commit()
    r = client.get(f"/incidents/type-options?incident_type_id={t.id}")
    assert r.status_code == 200
    assert f'value="{sev.id}" selected' in r.text


def test_type_options_requires_ic(client, db_session):
    _login(client, db_session, "ro@x.io", Role.read_only)
    assert client.get("/incidents/type-options").status_code == 403


def test_create_with_type(client, db_session):
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    sev = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db_session.add(sev)
    db_session.flush()
    itypes.seed_incident_types(db_session)
    db_session.flush()
    t = itypes.list_incident_types(db_session)[1]
    r = client.post(
        "/incidents",
        data={"title": "Typed", "severity_level_id": str(sev.id), "incident_type_id": str(t.id)},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    from app.services.incidents import list_incidents

    assert list_incidents(db_session)[0].incident_type_id == t.id
