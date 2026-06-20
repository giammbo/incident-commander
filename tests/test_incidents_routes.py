import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.models import Role, SeverityLevel
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


def _sev(db_session):
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=False)
    db_session.add(lvl)
    db_session.flush()
    return lvl.id


def test_readonly_cannot_create(client, db_session):
    _login(client, db_session, "ro@x.io", Role.read_only)
    sev_id = _sev(db_session)
    r = client.post(
        "/incidents",
        data={"title": "X", "severity_level_id": str(sev_id)},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_ic_can_create_and_close(client, db_session):
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    sev_id = _sev(db_session)
    r = client.post(
        "/incidents",
        data={"title": "Checkout down", "severity_level_id": str(sev_id)},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "Checkout down" in r.text
    # list shows it
    assert "Checkout down" in client.get("/").text
    # close it (find id via detail listing)
    from app.services.incidents import list_incidents

    inc = list_incidents(db_session)[0]
    r2 = client.post(f"/incidents/{inc.id}/close", headers={"HX-Request": "true"})
    assert r2.status_code == 200
    assert "closed" in r2.text.lower()
