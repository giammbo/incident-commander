import pytest
import sqlalchemy as sa

from app.models import SeverityLevel, System
from app.services import statuses
from app.services.catalog import update_system
from app.services.incidents import create_incident, update_incident


@pytest.fixture(autouse=True)
def _seed_user(db_session):
    db_session.execute(
        sa.text(
            "INSERT INTO users (id, email, name, is_active, must_change_password, is_protected_admin) "
            "VALUES (1, 'seed@test.local', 'Seed', true, false, false) ON CONFLICT (id) DO NOTHING"
        )
    )
    db_session.execute(sa.text("SELECT setval('users_id_seq', (SELECT MAX(id) FROM users))"))
    db_session.flush()


def _incident(db):
    statuses.seed_status_levels(db)
    db.flush()
    sev = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db.add(sev)
    db.flush()
    inc = create_incident(
        db,
        title="Orig",
        severity_level_id=sev.id,
        is_private=False,
        description="keep me",
        created_by=1,
    )
    db.flush()
    return inc


def test_update_incident_omitted_field_unchanged(db_session):
    inc = _incident(db_session)
    update_incident(db_session, inc, title="New title")  # description omitted
    assert inc.title == "New title"
    assert inc.description == "keep me"  # NOT clobbered to None


def test_update_incident_explicit_none_clears(db_session):
    inc = _incident(db_session)
    update_incident(db_session, inc, description=None)  # explicit clear
    assert inc.description is None


def test_update_system_omitted_owner_unchanged(db_session):
    from app.services.teams import create_team

    team = create_team(db_session, name="Platform")
    db_session.flush()
    sysm = System(name="Backend", owner_team_id=team.id)
    db_session.add(sysm)
    db_session.flush()
    update_system(db_session, sysm, name="Backend v2")  # owner_team_id omitted
    assert sysm.name == "Backend v2"
    assert sysm.owner_team_id == team.id  # NOT clobbered to None
