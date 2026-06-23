import pytest
import sqlalchemy as sa
from sqlalchemy import select

from app.models import IncidentEvent, SeverityLevel, StatusCategory, StatusLevel
from app.services import statuses, updates
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


def _incident(db):
    statuses.seed_status_levels(db)
    db.flush()
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db.add(lvl)
    db.flush()
    inc = create_incident(db, title="X", severity_level_id=lvl.id, is_private=False, created_by=1)
    db.flush()
    return inc


def _events(db, inc, entry_type):
    return list(
        db.scalars(
            select(IncidentEvent).where(
                IncidentEvent.incident_id == inc.id, IncidentEvent.entry_type == entry_type
            )
        )
    )


def test_post_update_rejects_empty(db_session):
    inc = _incident(db_session)
    with pytest.raises(ValueError):
        updates.post_update(db_session, inc, message="   ", by_user=1)


def test_post_update_logs_update_event(db_session):
    inc = _incident(db_session)
    kind = updates.post_update(db_session, inc, message="Mitigation deployed", by_user=1)
    assert kind is None
    ev = _events(db_session, inc, "update")
    assert len(ev) == 1 and ev[0].body == "Mitigation deployed"


def test_post_update_with_status_change(db_session):
    inc = _incident(db_session)
    monitoring = next(
        s
        for s in db_session.scalars(select(StatusLevel))
        if s.category == StatusCategory.active and s.label != inc.status.label
    )
    kind = updates.post_update(
        db_session, inc, message="Monitoring now", status_id=monitoring.id, by_user=1
    )
    assert kind == "changed"
    assert inc.status_id == monitoring.id
    assert len(_events(db_session, inc, "update")) == 1
    assert (
        len(_events(db_session, inc, "status_changed")) == 1
    )  # set_incident_status logged its own
