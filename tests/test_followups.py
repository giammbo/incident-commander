from datetime import date

import pytest
import sqlalchemy as sa
from sqlalchemy import select

from app.models import FollowUp, IncidentEvent, SeverityLevel, User
from app.services import followups, statuses
from app.services.incidents import create_incident


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
    db.flush()
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db.add(lvl)
    db.flush()
    return lvl


def _user(db, email, active=True):
    u = User(email=email, name=email.split("@")[0], is_active=active)
    db.add(u)
    db.flush()
    return u


def _incident(db):
    lvl = _seed(db)
    inc = create_incident(db, title="X", severity_level_id=lvl.id, is_private=False, created_by=1)
    db.flush()
    return inc


def test_create_requires_title_and_drops_inactive_assignee(db_session):
    inc = _incident(db_session)
    dead = _user(db_session, "d@x.io", active=False)
    with pytest.raises(ValueError):
        followups.create_followup(db_session, inc, title="  ", created_by=1)
    fu = followups.create_followup(
        db_session, inc, title="Patch the bug", assignee_id=dead.id, created_by=1
    )
    assert fu.title == "Patch the bug" and fu.assignee_id is None and fu.status == "open"


def test_create_emits_timeline_event(db_session):
    inc = _incident(db_session)
    followups.create_followup(db_session, inc, title="Rotate keys", created_by=1)
    ev = list(
        db_session.scalars(
            select(IncidentEvent).where(
                IncidentEvent.incident_id == inc.id, IncidentEvent.entry_type == "followup"
            )
        )
    )
    assert len(ev) == 1 and "Rotate keys" in ev[0].body


def test_set_status_transitions(db_session):
    inc = _incident(db_session)
    u = _user(db_session, "a@x.io")
    fu = followups.create_followup(db_session, inc, title="T", assignee_id=u.id, created_by=1)
    assert (
        followups.set_followup_status(db_session, fu, status="completed", by_user=u.id)
        == "completed"
    )
    assert fu.resolved_at is not None and fu.resolved_by == u.id
    followups.set_followup_status(db_session, fu, status="open", by_user=u.id)
    assert fu.resolved_at is None and fu.resolved_by is None
    with pytest.raises(ValueError):
        followups.set_followup_status(db_session, fu, status="bogus", by_user=u.id)


def test_update_and_delete(db_session):
    inc = _incident(db_session)
    fu = followups.create_followup(db_session, inc, title="T", created_by=1)
    followups.update_followup(
        db_session, fu, title="T2", description="d", assignee_id=None, due_on=date(2026, 7, 1)
    )
    assert fu.title == "T2" and fu.due_on == date(2026, 7, 1)
    fu_id = fu.id
    followups.delete_followup(db_session, fu_id)
    assert db_session.get(FollowUp, fu_id) is None


def test_list_and_open_count(db_session):
    inc = _incident(db_session)
    a = followups.create_followup(db_session, inc, title="A", created_by=1)
    followups.create_followup(db_session, inc, title="B", created_by=1)
    followups.set_followup_status(db_session, a, status="completed", by_user=1)
    db_session.expire(inc, ["follow_ups"])
    assert followups.open_count(inc) == 1
    listed = followups.list_followups(inc)
    assert listed[0].status == "open"  # open first
    assert {f.id for f in followups.list_open_followups(db_session)} == {
        f.id for f in listed if f.is_open
    }
