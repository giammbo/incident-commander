import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.models import Component, Role, SeverityLevel, System
from app.services.catalog import create_component, create_system
from app.services.incidents import create_incident, update_incident
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


@pytest.fixture
def _seed_user(db_session):
    """Insert a user with id=1 so created_by FK is satisfied (for service-level tests)."""
    import sqlalchemy as sa

    db_session.execute(
        sa.text(
            "INSERT INTO users (id, email, name, is_active, must_change_password, is_protected_admin) "
            "VALUES (1, 'seed@test.local', 'Seed', true, false, false) "
            "ON CONFLICT (id) DO NOTHING"
        )
    )
    db_session.flush()


def test_create_incident_links_components_and_detail_shows_deps(client, db_session):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(
        db_session, email="ic@x.io", name="IC", role=Role.incident_commander, password="pw-123456"
    )
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    sysm = System(name="Core")
    db_session.add_all([lvl, sysm])
    db_session.flush()
    a = Component(name="Checkout", system_id=sysm.id)
    b = Component(name="Payments", system_id=sysm.id)
    db_session.add_all([a, b])
    db_session.flush()
    a.depends_on = [b]
    db_session.flush()
    client.post("/login", data={"email": "ic@x.io", "password": "pw-123456"})
    # Route doesn't wire system_id yet (Task 6), so create incident via service directly
    inc = create_incident(
        db_session,
        title="X",
        severity_level_id=lvl.id,
        is_private=False,
        created_by=1,
        system_id=sysm.id,
        component_ids=[a.id],
    )
    db_session.commit()
    assert [s.name for s in inc.components] == ["Checkout"]
    r = client.get(f"/incidents/{inc.id}")
    assert "Checkout" in r.text and "Payments" in r.text  # affected component + its dependency


# ---------------------------------------------------------------------------
# Task 3: scoping tests
# ---------------------------------------------------------------------------


def _sev(db_session):
    from app.models import SeverityLevel

    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db_session.add(lvl)
    db_session.flush()
    return lvl


def test_incident_scoped_to_system_with_components(db_session, _seed_user):
    lvl = _sev(db_session)
    s = create_system(db_session, name="Billing")
    a = create_component(db_session, name="Invoicer", system_id=s.id)
    db_session.flush()
    inc = create_incident(
        db_session,
        title="X",
        severity_level_id=lvl.id,
        is_private=False,
        created_by=1,
        system_id=s.id,
        component_ids=[a.id],
    )
    db_session.flush()
    assert inc.system_id == s.id and [c.name for c in inc.components] == ["Invoicer"]


def test_incident_whole_system_when_no_components(db_session, _seed_user):
    lvl = _sev(db_session)
    s = create_system(db_session, name="Billing")
    db_session.flush()
    inc = create_incident(
        db_session,
        title="X",
        severity_level_id=lvl.id,
        is_private=False,
        created_by=1,
        system_id=s.id,
        component_ids=[],
    )
    db_session.flush()
    assert inc.system_id == s.id and inc.components == []


def test_incident_components_must_match_system(db_session, _seed_user):
    lvl = _sev(db_session)
    s1 = create_system(db_session, name="S1")
    s2 = create_system(db_session, name="S2")
    other = create_component(db_session, name="Other", system_id=s2.id)
    db_session.flush()
    with pytest.raises(ValueError):
        create_incident(
            db_session,
            title="X",
            severity_level_id=lvl.id,
            is_private=False,
            created_by=1,
            system_id=s1.id,
            component_ids=[other.id],
        )


def test_components_without_system_rejected(db_session, _seed_user):
    lvl = _sev(db_session)
    s = create_system(db_session, name="S1")
    a = create_component(db_session, name="A", system_id=s.id)
    db_session.flush()
    with pytest.raises(ValueError):
        create_incident(
            db_session,
            title="X",
            severity_level_id=lvl.id,
            is_private=False,
            created_by=1,
            system_id=None,
            component_ids=[a.id],
        )


def test_update_clearing_system_clears_components(db_session, _seed_user):
    lvl = _sev(db_session)
    s = create_system(db_session, name="S1")
    a = create_component(db_session, name="A", system_id=s.id)
    db_session.flush()
    inc = create_incident(
        db_session,
        title="X",
        severity_level_id=lvl.id,
        is_private=False,
        created_by=1,
        system_id=s.id,
        component_ids=[a.id],
    )
    db_session.flush()
    update_incident(
        db_session,
        inc,
        title="X",
        description=None,
        severity_level_id=lvl.id,
        system_id=None,
        component_ids=[a.id],
    )
    db_session.flush()
    assert inc.system_id is None and inc.components == []
