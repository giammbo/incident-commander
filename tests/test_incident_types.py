import pytest
import sqlalchemy as sa
from sqlalchemy import select

from app.models import IncidentType, SeverityLevel
from app.services import incident_types as itypes
from app.services.incidents import create_incident


@pytest.fixture(autouse=True)
def _seed_user(db_session):
    """Insert a user with id=1 so created_by FK is satisfied."""
    db_session.execute(
        sa.text(
            "INSERT INTO users (id, email, name, is_active, must_change_password, is_protected_admin) "
            "VALUES (1, 'seed@test.local', 'Seed', true, false, false) "
            "ON CONFLICT (id) DO NOTHING"
        )
    )
    db_session.flush()


def _sev(db, label="SEV1", default=False):
    lvl = SeverityLevel(label=label, color="#FF5D5D", rank=1, is_default=default)
    db.add(lvl)
    db.flush()
    return lvl


def test_seed_idempotent_with_default(db_session):
    itypes.seed_incident_types(db_session)
    itypes.seed_incident_types(db_session)
    rows = list(db_session.scalars(select(IncidentType)))
    assert {r.label for r in rows} >= {"Outage", "Degraded performance", "Maintenance", "Security"}
    assert sum(1 for r in rows if r.is_default) == 1


def test_create_rejects_duplicate(db_session):
    itypes.seed_incident_types(db_session)
    db_session.flush()
    with pytest.raises(ValueError):
        itypes.create_incident_type(db_session, label="Outage")


def test_delete_blocked_in_use(db_session):
    glob = _sev(db_session, default=True)
    itypes.seed_incident_types(db_session)
    db_session.flush()
    t = itypes.list_incident_types(db_session)[0]
    inc = create_incident(
        db_session,
        title="X",
        severity_level_id=glob.id,
        is_private=False,
        created_by=1,
        incident_type_id=t.id,
    )
    db_session.flush()
    assert inc.incident_type_id == t.id
    with pytest.raises(ValueError):
        itypes.delete_incident_type(db_session, t.id)


def test_create_incident_defaults_type_and_resolves_severity(db_session):
    glob = _sev(db_session, label="SEV-GLOBAL", default=True)
    sev_for_type = _sev(db_session, label="SEV-TYPE")
    itypes.seed_incident_types(db_session)
    db_session.flush()
    # give the default type a default severity
    dt = db_session.get(IncidentType, itypes.default_incident_type_id(db_session))
    dt.default_severity_level_id = sev_for_type.id
    db_session.flush()
    # no severity, no type -> default type applied, its default severity resolved
    inc = create_incident(
        db_session, title="A", severity_level_id=None, is_private=False, created_by=1
    )
    db_session.flush()
    assert inc.incident_type_id == dt.id and inc.severity_level_id == sev_for_type.id
    # explicit severity wins
    inc2 = create_incident(
        db_session, title="B", severity_level_id=glob.id, is_private=False, created_by=1
    )
    db_session.flush()
    assert inc2.severity_level_id == glob.id


def test_set_default_moves(db_session):
    itypes.seed_incident_types(db_session)
    db_session.flush()
    types = itypes.list_incident_types(db_session)
    other = next(t for t in types if not t.is_default)
    itypes.set_default_incident_type(db_session, other.id)
    db_session.refresh(other)
    assert (
        other.is_default
        and sum(1 for t in itypes.list_incident_types(db_session) if t.is_default) == 1
    )
