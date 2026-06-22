import pytest
import sqlalchemy

from app.models import SeverityLevel, StatusCategory, StatusLevel
from app.services import statuses
from app.services.incidents import create_incident, set_incident_status


@pytest.fixture(autouse=True)
def _seed_user(db_session):
    """Insert a user with id=1 so created_by FK is satisfied."""
    db_session.execute(
        sqlalchemy.text(
            "INSERT INTO users (id, email, name, is_active, must_change_password, is_protected_admin) "
            "VALUES (1, 'seed@test.local', 'Seed', true, false, false) "
            "ON CONFLICT (id) DO NOTHING"
        )
    )
    db_session.flush()


def _seeded(db):
    statuses.seed_status_levels(db)
    db.flush()


def test_seed_is_idempotent_with_categories(db_session):
    _seeded(db_session)
    _seeded(db_session)
    rows = list(db_session.scalars(__import__("sqlalchemy").select(StatusLevel)))
    labels = {r.label: r.category for r in rows}
    assert labels["Triage"] == StatusCategory.triage
    assert labels["Closed"] == StatusCategory.closed
    assert sum(1 for r in rows if r.is_default) == 1


def test_default_is_triage_and_create_uses_it(db_session):
    _seeded(db_session)
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db_session.add(lvl)
    db_session.flush()
    inc = create_incident(
        db_session, title="X", severity_level_id=lvl.id, is_private=False, created_by=1
    )
    db_session.flush()
    assert inc.status.label == "Triage" and not inc.is_closed


def test_set_status_closed_then_reopen(db_session):
    _seeded(db_session)
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db_session.add(lvl)
    db_session.flush()
    inc = create_incident(
        db_session, title="X", severity_level_id=lvl.id, is_private=False, created_by=1
    )
    db_session.flush()
    closed = next(
        s
        for s in db_session.scalars(__import__("sqlalchemy").select(StatusLevel))
        if s.category == StatusCategory.closed
    )
    active = next(
        s
        for s in db_session.scalars(__import__("sqlalchemy").select(StatusLevel))
        if s.category == StatusCategory.active
    )
    assert set_incident_status(db_session, inc, status_id=closed.id, by_user=1) == "closed"
    assert inc.is_closed and inc.closed_at is not None and inc.closed_by == 1
    assert set_incident_status(db_session, inc, status_id=active.id, by_user=1) == "reopened"
    assert not inc.is_closed and inc.closed_at is None


def test_set_status_between_active_is_changed(db_session):
    _seeded(db_session)
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db_session.add(lvl)
    db_session.flush()
    inc = create_incident(
        db_session, title="X", severity_level_id=lvl.id, is_private=False, created_by=1
    )
    db_session.flush()
    actives = [
        s
        for s in db_session.scalars(__import__("sqlalchemy").select(StatusLevel))
        if s.category == StatusCategory.active
    ]
    target = actives[0]
    assert set_incident_status(db_session, inc, status_id=target.id, by_user=1) == "changed"
    assert not inc.is_closed and inc.closed_at is None and inc.closed_by is None


def test_delete_status_in_use_blocked(db_session):
    _seeded(db_session)
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db_session.add(lvl)
    db_session.flush()
    inc = create_incident(
        db_session, title="X", severity_level_id=lvl.id, is_private=False, created_by=1
    )
    db_session.flush()
    with pytest.raises(ValueError):
        statuses.delete_status_level(db_session, inc.status_id)
