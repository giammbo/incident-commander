import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import CustomFieldDef, IncidentType
from app.services import incident_types
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


def test_admin_creates_field(client, db_session):
    _admin(client, db_session)
    client.post(
        "/settings/custom-fields",
        data={"label": "Root cause", "field_type": "textarea", "rank": "5"},
    )
    fields = list(
        db_session.scalars(select(CustomFieldDef).where(CustomFieldDef.label == "Root cause"))
    )
    assert len(fields) == 1 and fields[0].field_type == "textarea"


def test_create_with_type_binding(client, db_session):
    _admin(client, db_session)
    incident_types.seed_incident_types(db_session)
    db_session.flush()
    itype = db_session.scalars(select(IncidentType)).first()
    assert itype is not None, "seed_incident_types produced no rows"
    tid = itype.id
    client.post(
        "/settings/custom-fields",
        data={"label": "X", "field_type": "text", "incident_type_ids": str(tid)},
    )
    field = db_session.scalars(select(CustomFieldDef).where(CustomFieldDef.label == "X")).first()
    assert field is not None
    assert any(t.id == tid for t in field.incident_types)


def test_bad_field_type_flashes(client, db_session):
    _admin(client, db_session)
    r = client.post(
        "/settings/custom-fields",
        data={"label": "X", "field_type": "bogus"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    fields = list(db_session.scalars(select(CustomFieldDef).where(CustomFieldDef.label == "X")))
    assert fields == []


def test_admin_deletes_field(client, db_session):
    _admin(client, db_session)
    fd = CustomFieldDef(label="Tmp", field_type="text", rank=9)
    db_session.add(fd)
    db_session.flush()
    fid = fd.id
    r = client.post(f"/settings/custom-fields/{fid}/delete", follow_redirects=False)
    assert r.status_code == 303
    remaining = list(db_session.scalars(select(CustomFieldDef).where(CustomFieldDef.id == fid)))
    assert remaining == []


def test_non_admin_forbidden(client, db_session):
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
    r = client.post(
        "/settings/custom-fields",
        data={"label": "Foo", "field_type": "text"},
    )
    assert r.status_code == 403
