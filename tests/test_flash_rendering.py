"""Flash messages set by any route must render on the next full page (via base.html)."""

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


def _incident(db_session):
    statuses.seed_status_levels(db_session)
    db_session.flush()
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db_session.add(lvl)
    db_session.flush()
    inc = create_incident(
        db_session, title="X", severity_level_id=lvl.id, is_private=False, created_by=1
    )
    db_session.flush()
    return inc


def test_incident_route_flash_renders_on_detail(client, db_session):
    # An empty note flashes "Note cannot be empty" and 303-redirects to the detail page;
    # following the redirect, base.html must render the flash (previously it was invisible).
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    inc = _incident(db_session)
    db_session.commit()
    r = client.post(
        f"/incidents/{inc.id}/notes", data={"body": "   "}
    )  # follows the 303 by default
    assert r.status_code == 200
    assert "Note cannot be empty" in r.text


def test_flash_is_consumed_after_one_render(client, db_session):
    # The flash shows once, then a fresh page load no longer shows it.
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    inc = _incident(db_session)
    db_session.commit()
    client.post(
        f"/incidents/{inc.id}/notes", data={"body": "   "}
    )  # consumes the flash on the redirect render
    r2 = client.get(f"/incidents/{inc.id}")
    assert "Note cannot be empty" not in r2.text
