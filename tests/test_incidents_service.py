import pytest

from app.models import SeverityLevel
from app.services import statuses
from app.services.incidents import close_incident, create_incident, list_incidents


@pytest.fixture(autouse=True)
def _seed_user(db_session):
    """Insert a user with id=1 so created_by FK is satisfied."""
    import sqlalchemy as sa

    db_session.execute(
        sa.text(
            "INSERT INTO users (id, email, name, is_active, must_change_password, is_protected_admin) "
            "VALUES (1, 'seed@test.local', 'Seed', true, false, false) "
            "ON CONFLICT (id) DO NOTHING"
        )
    )
    db_session.flush()


def _sev(db):
    lvl = SeverityLevel(label="SEV2", color="#F4B740", rank=2, is_default=True)
    db.add(lvl)
    db.flush()
    return lvl.id


def test_create_and_list(db_session):
    statuses.seed_status_levels(db_session)
    db_session.flush()
    sev_id = _sev(db_session)
    a = create_incident(
        db_session, title="A", severity_level_id=sev_id, is_private=False, created_by=1
    )
    b = create_incident(
        db_session, title="B", severity_level_id=sev_id, is_private=True, created_by=1
    )
    db_session.flush()
    ids = [i.id for i in list_incidents(db_session)]
    assert ids[0] == b.id  # newest first
    assert a.creation_state == {"channel": "skipped", "meet": "skipped", "announce": "skipped"}


def test_close_incident(db_session):
    statuses.seed_status_levels(db_session)
    db_session.flush()
    inc = create_incident(
        db_session, title="A", severity_level_id=_sev(db_session), is_private=False, created_by=1
    )
    db_session.flush()
    close_incident(db_session, inc, closed_by=1)
    assert inc.is_closed
    assert inc.closed_at is not None
    assert inc.closed_at.tzinfo is not None  # timezone-aware UTC, not naive
    with pytest.raises(ValueError):
        close_incident(db_session, inc, closed_by=1)


def test_filter_by_status(db_session):
    statuses.seed_status_levels(db_session)
    db_session.flush()
    sev_id = _sev(db_session)
    inc = create_incident(
        db_session, title="A", severity_level_id=sev_id, is_private=False, created_by=1
    )
    create_incident(db_session, title="B", severity_level_id=sev_id, is_private=False, created_by=1)
    db_session.flush()
    close_incident(db_session, inc, closed_by=1)
    db_session.flush()
    all_incidents = list_incidents(db_session)
    open_count = sum(1 for i in all_incidents if not i.is_closed)
    closed_count = sum(1 for i in all_incidents if i.is_closed)
    assert open_count == 1
    assert closed_count == 1
