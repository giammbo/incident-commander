import pytest
import sqlalchemy as sa
from sqlalchemy import select

from app.models import (
    IncidentEvent,
    SeverityLevel,
    StatusCategory,
    StatusLevel,
    User,
)
from app.services import roles, statuses, timeline
from app.services.incidents import create_incident, set_incident_role_assignees, set_incident_status


@pytest.fixture(autouse=True)
def _seed_user(db_session):
    """Insert a user with id=1 so created_by FK is satisfied, then advance the sequence."""
    db_session.execute(
        sa.text(
            "INSERT INTO users (id, email, name, is_active, must_change_password, is_protected_admin) "
            "VALUES (1, 'seed@test.local', 'Seed', true, false, false) "
            "ON CONFLICT (id) DO NOTHING"
        )
    )
    db_session.execute(sa.text("SELECT setval('users_id_seq', (SELECT MAX(id) FROM users))"))
    db_session.flush()


def _seed(db):
    statuses.seed_status_levels(db)
    roles.seed_incident_role_types(db)
    db.flush()
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db.add(lvl)
    db.flush()
    return lvl


def _events(db, inc, entry_type=None):
    q = select(IncidentEvent).where(IncidentEvent.incident_id == inc.id)
    if entry_type:
        q = q.where(IncidentEvent.entry_type == entry_type)
    return list(db.scalars(q))


def test_create_emits_opened(db_session):
    lvl = _seed(db_session)
    inc = create_incident(
        db_session, title="X", severity_level_id=lvl.id, is_private=False, created_by=1
    )
    db_session.flush()
    opened = _events(db_session, inc, "opened")
    assert len(opened) == 1 and opened[0].created_by == 1


def test_status_change_emits_events(db_session):
    lvl = _seed(db_session)
    inc = create_incident(
        db_session, title="X", severity_level_id=lvl.id, is_private=False, created_by=1
    )
    db_session.flush()
    active = next(
        s for s in db_session.scalars(select(StatusLevel)) if s.category == StatusCategory.active
    )
    closed = next(
        s for s in db_session.scalars(select(StatusLevel)) if s.category == StatusCategory.closed
    )
    set_incident_status(db_session, inc, status_id=active.id, by_user=1)
    set_incident_status(db_session, inc, status_id=closed.id, by_user=1)
    set_incident_status(db_session, inc, status_id=active.id, by_user=1)
    types = [e.entry_type for e in _events(db_session, inc) if e.entry_type != "opened"]
    assert types == ["status_changed", "closed", "reopened"]
    changed = _events(db_session, inc, "status_changed")[0]
    assert "from" in changed.body and "to" in changed.body


def test_roles_change_emits_event(db_session):
    lvl = _seed(db_session)
    u = User(email="a@x.io", name="Alice", is_active=True)
    db_session.add(u)
    db_session.flush()
    inc = create_incident(
        db_session, title="X", severity_level_id=lvl.id, is_private=False, created_by=1
    )
    db_session.flush()
    lead = roles.list_incident_role_types(db_session)[0]
    set_incident_role_assignees(db_session, inc, role_type_id=lead.id, user_ids=[u.id], by_user=1)
    ev = _events(db_session, inc, "roles_changed")
    assert len(ev) == 1 and "Alice" in ev[0].body and lead.label in ev[0].body


def test_add_note_rejects_empty_and_sets_type(db_session):
    lvl = _seed(db_session)
    inc = create_incident(
        db_session, title="X", severity_level_id=lvl.id, is_private=False, created_by=1
    )
    db_session.flush()
    with pytest.raises(ValueError):
        timeline.add_note(db_session, inc, body="   ", created_by=1)
    n = timeline.add_note(db_session, inc, body="hello", created_by=1)
    assert n.entry_type == "note" and n.body == "hello"


def test_toggle_pin_and_delete_note(db_session):
    lvl = _seed(db_session)
    inc = create_incident(
        db_session, title="X", severity_level_id=lvl.id, is_private=False, created_by=1
    )
    db_session.flush()
    n = timeline.add_note(db_session, inc, body="note", created_by=1)
    assert timeline.toggle_pin(db_session, n.id) is True
    assert timeline.toggle_pin(db_session, n.id) is False
    opened = _events(db_session, inc, "opened")[0]
    with pytest.raises(ValueError):
        timeline.delete_note(db_session, opened.id)  # auto-event not deletable
    timeline.delete_note(db_session, n.id)
    assert not _events(db_session, inc, "note")


def test_list_events_pinned_first_then_newest(db_session):
    lvl = _seed(db_session)
    inc = create_incident(
        db_session, title="X", severity_level_id=lvl.id, is_private=False, created_by=1
    )
    db_session.flush()
    a = timeline.add_note(db_session, inc, body="a", created_by=1)
    timeline.add_note(db_session, inc, body="b", created_by=1)
    timeline.toggle_pin(db_session, a.id)
    ordered = timeline.list_events(inc)
    assert ordered[0].id == a.id  # pinned floats to top
