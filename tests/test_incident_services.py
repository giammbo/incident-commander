import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import Incident, Role, Service, SeverityLevel
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def test_create_incident_links_services_and_detail_shows_deps(client, db_session):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(
        db_session, email="ic@x.io", name="IC", role=Role.incident_commander, password="pw-123456"
    )
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    a = Service(name="Checkout")
    b = Service(name="Payments")
    db_session.add_all([lvl, a, b])
    db_session.flush()
    a.depends_on = [b]
    db_session.flush()
    client.post("/login", data={"email": "ic@x.io", "password": "pw-123456"})
    client.post(
        "/incidents",
        data={"title": "X", "severity_level_id": str(lvl.id), "service_ids": [str(a.id)]},
        headers={"HX-Request": "true"},
    )
    inc = db_session.scalar(select(Incident).order_by(Incident.id.desc()))
    assert [s.name for s in inc.services] == ["Checkout"]
    r = client.get(f"/incidents/{inc.id}")
    assert "Checkout" in r.text and "Payments" in r.text  # affected service + its dependency
