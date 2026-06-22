import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.models import Role, SeverityLevel
from app.services import statuses
from app.services.followups import create_followup, set_followup_status
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


def test_follow_ups_page_lists_only_open(client, db_session):
    _login(client, db_session, "ro@x.io", Role.read_only)  # all roles can view
    statuses.seed_status_levels(db_session)
    db_session.flush()
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db_session.add(lvl)
    db_session.flush()
    inc = create_incident(
        db_session, title="Checkout down", severity_level_id=lvl.id, is_private=False, created_by=1
    )
    db_session.flush()
    _ = create_followup(db_session, inc, title="Open item", created_by=1)
    done = create_followup(db_session, inc, title="Done item", created_by=1)
    set_followup_status(db_session, done, status="completed", by_user=1)
    db_session.commit()
    r = client.get("/follow-ups")
    assert r.status_code == 200
    assert "Open item" in r.text and "Done item" not in r.text
    assert "Checkout down" in r.text  # grouped/linked by incident
