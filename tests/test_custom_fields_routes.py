import json
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.models import Role, SeverityLevel
from app.services import custom_fields as cf
from app.services.incidents import create_incident, list_incidents
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


def _setup(db_session):
    from app.services import incident_types, statuses

    statuses.seed_status_levels(db_session)
    incident_types.seed_incident_types(db_session)
    db_session.flush()
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db_session.add(lvl)
    db_session.flush()
    return lvl


def test_type_options_requires_ic(client, db_session):
    _login(client, db_session, "ro@x.io", Role.read_only)
    assert client.get("/incidents/type-options").status_code == 403


def test_create_persists_custom_values(client, db_session):
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    lvl = _setup(db_session)
    fd = cf.create_field_def(db_session, label="Root cause", field_type="text", required=True)
    db_session.commit()
    r = client.post(
        "/incidents",
        data={"title": "T", "severity_level_id": str(lvl.id), f"cf_{fd.id}": "bad deploy"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    inc = list_incidents(db_session)[0]
    assert cf.values_for_incident(inc)[fd.id] == "bad deploy"


def test_edit_custom_fields_route(client, db_session):
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    lvl = _setup(db_session)
    fd = cf.create_field_def(db_session, label="Tags", field_type="multi_select", options="a\nb\nc")
    inc = create_incident(
        db_session, title="T", severity_level_id=lvl.id, is_private=False, created_by=1
    )
    db_session.commit()
    body = urllib.parse.urlencode([(f"cf_{fd.id}", "a"), (f"cf_{fd.id}", "c")])
    r = client.post(
        f"/incidents/{inc.id}/custom-fields",
        content=body.encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db_session.expire(inc, ["custom_values"])
    assert json.loads(cf.values_for_incident(inc)[fd.id]) == ["a", "c"]


def test_readonly_cannot_edit(client, db_session):
    _login(client, db_session, "ro@x.io", Role.read_only)
    assert (
        client.post("/incidents/1/custom-fields", data={}, follow_redirects=False).status_code
        == 403
    )
