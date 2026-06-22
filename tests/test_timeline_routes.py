import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import IncidentEvent, Role, SeverityLevel
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


def _notes(db_session, inc):
    return list(
        db_session.scalars(
            select(IncidentEvent).where(
                IncidentEvent.incident_id == inc.id, IncidentEvent.entry_type == "note"
            )
        )
    )


def test_readonly_cannot_add_note(client, db_session):
    _login(client, db_session, "ro@x.io", Role.read_only)
    assert (
        client.post("/incidents/1/notes", data={"body": "x"}, follow_redirects=False).status_code
        == 403
    )


def test_ic_adds_pins_deletes_note(client, db_session):
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    inc = _incident(db_session)
    r = client.post(f"/incidents/{inc.id}/notes", data={"body": "hello"}, follow_redirects=False)
    assert r.status_code == 303
    note = _notes(db_session, inc)[0]
    assert (
        client.post(f"/incidents/{inc.id}/events/{note.id}/pin", follow_redirects=False).status_code
        == 303
    )
    db_session.refresh(note)
    assert note.pinned is True
    assert (
        client.post(
            f"/incidents/{inc.id}/events/{note.id}/delete", follow_redirects=False
        ).status_code
        == 303
    )
    assert not _notes(db_session, inc)


def test_empty_note_flashes_not_500(client, db_session):
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    inc = _incident(db_session)
    r = client.post(f"/incidents/{inc.id}/notes", data={"body": "   "}, follow_redirects=False)
    assert r.status_code == 303
    assert not _notes(db_session, inc)


def test_cannot_delete_auto_event(client, db_session):
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    inc = _incident(db_session)
    db_session.commit()  # commit so rollback in route doesn't wipe the event
    opened = db_session.scalar(
        select(IncidentEvent).where(
            IncidentEvent.incident_id == inc.id, IncidentEvent.entry_type == "opened"
        )
    )
    opened_id = opened.id
    r = client.post(f"/incidents/{inc.id}/events/{opened_id}/delete", follow_redirects=False)
    assert r.status_code == 303  # flashed, not 500
    still = db_session.scalar(select(IncidentEvent).where(IncidentEvent.id == opened_id))
    assert still is not None
