import pytest
import sqlalchemy

from app.models import SeverityLevel, User
from app.services import roles, statuses
from app.services.incidents import create_incident, set_incident_role_assignees


@pytest.fixture(autouse=True)
def _seed_user(db_session):
    """Insert a user with id=1 so created_by FK is satisfied, advance sequence."""
    db_session.execute(
        sqlalchemy.text(
            "INSERT INTO users (id, email, name, is_active, must_change_password, is_protected_admin) "
            "VALUES (1, 'seed@test.local', 'Seed', true, false, false) "
            "ON CONFLICT (id) DO NOTHING"
        )
    )
    db_session.execute(sqlalchemy.text("SELECT setval('users_id_seq', 1, true)"))
    db_session.flush()


def _incident(db):
    statuses.seed_status_levels(db)
    roles.seed_incident_role_types(db)
    db.flush()
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db.add(lvl)
    db.flush()
    inc = create_incident(db, title="X", severity_level_id=lvl.id, is_private=False, created_by=1)
    db.flush()
    return inc


def _user(db, email, active=True):
    u = User(email=email, name=email.split("@")[0], is_active=active)
    db.add(u)
    db.flush()
    return u


def test_seed_role_types_idempotent(db_session):
    roles.seed_incident_role_types(db_session)
    roles.seed_incident_role_types(db_session)
    labels = [r.label for r in roles.list_incident_role_types(db_session)]
    assert labels == ["Incident Lead", "Communications", "Scribe"]


def test_create_rejects_duplicate(db_session):
    roles.seed_incident_role_types(db_session)
    db_session.flush()
    with pytest.raises(ValueError):
        roles.create_incident_role_type(db_session, label="Scribe", rank=9)


def test_delete_blocked_when_in_use(db_session):
    inc = _incident(db_session)
    u = _user(db_session, "a@x.io")
    lead = roles.list_incident_role_types(db_session)[0]
    set_incident_role_assignees(db_session, inc, role_type_id=lead.id, user_ids=[u.id], by_user=1)
    with pytest.raises(ValueError):
        roles.delete_incident_role_type(db_session, lead.id)


def test_set_assignees_add_replace_clear_dedup_inactive(db_session):
    inc = _incident(db_session)
    a = _user(db_session, "a@x.io")
    b = _user(db_session, "b@x.io")
    dead = _user(db_session, "d@x.io", active=False)
    lead = roles.list_incident_role_types(db_session)[0]
    comms = roles.list_incident_role_types(db_session)[1]
    # add a,b (with a dup of a and an inactive user that gets dropped)
    out = set_incident_role_assignees(
        db_session, inc, role_type_id=lead.id, user_ids=[a.id, a.id, b.id, dead.id], by_user=1
    )
    assert sorted(u.id for u in out) == sorted([a.id, b.id])
    assert (
        roles.assignments_by_role(inc)[lead.id]
        and len(roles.assignments_by_role(inc)[lead.id]) == 2
    )
    # replace lead with just b
    set_incident_role_assignees(db_session, inc, role_type_id=lead.id, user_ids=[b.id], by_user=1)
    assert [u.id for u in roles.assignments_by_role(inc)[lead.id]] == [b.id]
    # a user can hold a second role
    set_incident_role_assignees(db_session, inc, role_type_id=comms.id, user_ids=[b.id], by_user=1)
    assert b.id in [u.id for u in roles.assignments_by_role(inc)[comms.id]]
    # clear lead
    set_incident_role_assignees(db_session, inc, role_type_id=lead.id, user_ids=[], by_user=1)
    assert (
        lead.id not in roles.assignments_by_role(inc) or not roles.assignments_by_role(inc)[lead.id]
    )


def test_set_assignees_unknown_role_raises(db_session):
    inc = _incident(db_session)
    with pytest.raises(ValueError):
        set_incident_role_assignees(db_session, inc, role_type_id=999999, user_ids=[], by_user=1)
