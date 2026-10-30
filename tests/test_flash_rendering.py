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
def client(db_session, get_db_override):
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override
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


def test_empty_note_renders_request_local_error_on_detail(client, db_session):
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    inc = _incident(db_session)
    db_session.commit()
    response = client.post(
        f"/incidents/{inc.id}/notes", data={"body": "   "}, follow_redirects=False
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("text/html")
    assert "Note cannot be empty" in response.text
    assert "Note cannot be empty" not in client.get(f"/incidents/{inc.id}").text


def test_flash_is_consumed_after_one_render(client, db_session):
    # An actual flash must first be visible, then consumed by the first full render.
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    inc = _incident(db_session)
    db_session.commit()
    response = client.post(
        f"/incidents/{inc.id}/add-meet",
        data={"video": "unknown"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/incidents/{inc.id}"
    first_render = client.get(response.headers["location"])
    assert first_render.status_code == 200
    assert "Unknown video provider." in first_render.text
    second_render = client.get(f"/incidents/{inc.id}")
    assert second_render.status_code == 200
    assert "Unknown video provider." not in second_render.text
