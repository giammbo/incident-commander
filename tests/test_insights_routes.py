import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.models import Role, SeverityLevel
from app.services import statuses
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


def test_insights_page_require_user(client, db_session):
    _login(client, db_session, "ro@x.io", Role.read_only)  # all roles can view
    statuses.seed_status_levels(db_session)
    db_session.flush()
    sev = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db_session.add(sev)
    db_session.flush()
    create_incident(
        db_session, title="Checkout down", severity_level_id=sev.id, is_private=False, created_by=1
    )
    db_session.commit()
    r = client.get("/insights")
    assert r.status_code == 200
    assert "Insights" in r.text and "By severity" in r.text and "SEV1" in r.text
    assert client.get("/insights?days=0").status_code == 200  # all-time window
