import pytest

from app.models import Team
from app.services import teams
from app.services.catalog import create_component, create_system


def test_create_rejects_empty_and_dup(db_session):
    teams.create_team(db_session, name="Platform")
    db_session.flush()
    with pytest.raises(ValueError):
        teams.create_team(db_session, name="  ")
    with pytest.raises(ValueError):
        teams.create_team(db_session, name="Platform")


def test_create_system_with_owner(db_session):
    t = teams.create_team(db_session, name="Payments")
    db_session.flush()
    s = create_system(db_session, name="Checkout", owner_team_id=t.id)
    db_session.flush()
    assert s.owner_team_id == t.id and s.owner_team.name == "Payments"


def test_create_component_with_owner(db_session):
    t = teams.create_team(db_session, name="Core")
    db_session.flush()
    s = create_system(db_session, name="Backend")
    db_session.flush()
    c = create_component(db_session, name="API", system_id=s.id, owner_team_id=t.id)
    db_session.flush()
    assert c.owner_team_id == t.id


def test_delete_blocked_in_use_by_system_or_component(db_session):
    t = teams.create_team(db_session, name="Owners")
    db_session.flush()
    s = create_system(db_session, name="Sys", owner_team_id=t.id)
    db_session.flush()
    with pytest.raises(ValueError):
        teams.delete_team(db_session, t.id)
    # reassign the system, then block via a component
    s.owner_team_id = None
    c = create_component(db_session, name="Comp", system_id=s.id, owner_team_id=t.id)
    db_session.flush()
    with pytest.raises(ValueError):
        teams.delete_team(db_session, t.id)
    c.owner_team_id = None
    db_session.flush()
    teams.delete_team(db_session, t.id)
    assert db_session.get(Team, t.id) is None
