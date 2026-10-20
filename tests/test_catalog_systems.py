import pytest
import sqlalchemy as sa

from app.services.catalog import (
    create_component,
    create_system,
    delete_system,
    set_component_dependencies,
    set_system_dependencies,
    update_component,
    update_system,
)


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


def test_create_system_and_component_in_it(db_session):
    s = create_system(db_session, name="Billing", description="Money")
    c = create_component(db_session, name="Invoicer", system_id=s.id)
    db_session.flush()
    assert c.system_id == s.id and c.system.name == "Billing"


def test_duplicate_system_name_rejected(db_session):
    create_system(db_session, name="Billing")
    db_session.flush()
    with pytest.raises(ValueError):
        create_system(db_session, name="Billing")


def test_create_component_requires_existing_system(db_session):
    with pytest.raises(ValueError):
        create_component(db_session, name="Orphan", system_id=999)


def test_component_dep_same_system_ok(db_session):
    s = create_system(db_session, name="Billing")
    a = create_component(db_session, name="A", system_id=s.id)
    b = create_component(db_session, name="B", system_id=s.id)
    db_session.flush()
    set_component_dependencies(db_session, a, [b.id])
    db_session.flush()
    db_session.refresh(a)
    assert [c.name for c in a.depends_on] == ["B"]


def test_component_dep_cross_system_rejected(db_session):
    s1 = create_system(db_session, name="S1")
    s2 = create_system(db_session, name="S2")
    a = create_component(db_session, name="A", system_id=s1.id)
    b = create_component(db_session, name="B", system_id=s2.id)
    db_session.flush()
    with pytest.raises(ValueError):
        set_component_dependencies(db_session, a, [b.id])


def test_moving_component_prunes_cross_system_edges(db_session):
    s1 = create_system(db_session, name="S1")
    s2 = create_system(db_session, name="S2")
    a = create_component(db_session, name="A", system_id=s1.id)
    b = create_component(db_session, name="B", system_id=s1.id)
    db_session.flush()
    set_component_dependencies(db_session, a, [b.id])
    db_session.flush()
    # move B to S2 → the A→B edge is now cross-system and must be pruned
    update_component(db_session, b, name="B", description=None, system_id=s2.id)
    db_session.flush()
    db_session.refresh(a)
    assert a.depends_on == []


def test_system_dep_cross_system_allowed_and_no_self(db_session):
    s1 = create_system(db_session, name="S1")
    s2 = create_system(db_session, name="S2")
    db_session.flush()
    set_system_dependencies(db_session, s1, [s2.id, s1.id])  # self dropped
    db_session.flush()
    db_session.refresh(s1)
    assert [s.name for s in s1.depends_on] == ["S2"]


def test_delete_system_blocked_while_it_has_components(db_session):
    s = create_system(db_session, name="S1")
    create_component(db_session, name="A", system_id=s.id)
    db_session.flush()
    with pytest.raises(ValueError):
        delete_system(db_session, s.id)


def test_moving_component_prunes_outgoing_cross_system_edges(db_session):
    s1 = create_system(db_session, name="S1")
    s2 = create_system(db_session, name="S2")
    a = create_component(db_session, name="A", system_id=s1.id)
    b = create_component(db_session, name="B", system_id=s1.id)
    db_session.flush()
    set_component_dependencies(db_session, a, [b.id])  # A depends on B, both in S1
    db_session.flush()
    update_component(db_session, a, name="A", description=None, system_id=s2.id)  # move A to S2
    db_session.flush()
    db_session.refresh(a)
    assert a.depends_on == []  # A's outgoing edge to B (now cross-system) pruned


def test_update_system_renames_and_rejects_duplicate(db_session):
    s1 = create_system(db_session, name="Alpha")
    s2 = create_system(db_session, name="Beta")
    db_session.flush()
    update_system(db_session, s1, name="Alpha2", description="x")  # rename ok
    db_session.flush()
    db_session.refresh(s1)
    assert s1.name == "Alpha2" and s1.description == "x"
    update_system(db_session, s1, name="Alpha2", description="y")  # same name as self: ok
    with pytest.raises(ValueError):
        update_system(db_session, s1, name=s2.name, description=None)  # clashes with s2


def test_delete_system_blocked_when_referenced_by_incident(db_session):
    from app.models import SeverityLevel
    from app.services.incidents import create_incident

    s = create_system(db_session, name="S1")
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db_session.add(lvl)
    db_session.flush()
    create_incident(
        db_session,
        title="X",
        severity_level_id=lvl.id,
        is_private=False,
        created_by=1,
        system_id=s.id,
        component_ids=[],
    )
    db_session.flush()
    with pytest.raises(ValueError):
        delete_system(db_session, s.id)


def test_component_direct_cycle_rejected(db_session):
    s = create_system(db_session, name="S1")
    a = create_component(db_session, name="A", system_id=s.id)
    b = create_component(db_session, name="B", system_id=s.id)
    set_component_dependencies(db_session, a, [b.id])
    with pytest.raises(ValueError, match="B → A → B"):
        set_component_dependencies(db_session, b, [a.id])
    assert b.depends_on == []


def test_component_indirect_cycle_rejected(db_session):
    s = create_system(db_session, name="S1")
    a = create_component(db_session, name="A", system_id=s.id)
    b = create_component(db_session, name="B", system_id=s.id)
    c = create_component(db_session, name="C", system_id=s.id)
    set_component_dependencies(db_session, a, [b.id])
    set_component_dependencies(db_session, b, [c.id])
    with pytest.raises(ValueError, match="C → A → B → C"):
        set_component_dependencies(db_session, c, [a.id])


def test_replacing_edges_is_judged_on_the_new_set(db_session):
    # A -> B. Replacing A's deps with [] and then B -> A is fine (no loop any more).
    s = create_system(db_session, name="S1")
    a = create_component(db_session, name="A", system_id=s.id)
    b = create_component(db_session, name="B", system_id=s.id)
    set_component_dependencies(db_session, a, [b.id])
    set_component_dependencies(db_session, a, [])
    set_component_dependencies(db_session, b, [a.id])
    assert [x.name for x in b.depends_on] == ["A"]


def test_diamond_is_not_a_cycle(db_session):
    s = create_system(db_session, name="S1")
    a, b, c, d = (create_component(db_session, name=n, system_id=s.id) for n in "ABCD")
    set_component_dependencies(db_session, a, [b.id, c.id])
    set_component_dependencies(db_session, b, [d.id])
    set_component_dependencies(db_session, c, [d.id])
    assert sorted(x.name for x in a.depends_on) == ["B", "C"]


def test_system_cycle_rejected(db_session):
    s1 = create_system(db_session, name="S1")
    s2 = create_system(db_session, name="S2")
    s3 = create_system(db_session, name="S3")
    set_system_dependencies(db_session, s1, [s2.id])
    set_system_dependencies(db_session, s2, [s3.id])
    with pytest.raises(ValueError, match="dependency loop"):
        set_system_dependencies(db_session, s3, [s1.id])
    assert s3.depends_on == []


def test_dependency_view_used_by_and_blocked(db_session):
    from app.services.catalog import dependency_view

    s = create_system(db_session, name="S1")
    a, b, c, d = (create_component(db_session, name=n, system_id=s.id) for n in "ABCD")
    set_component_dependencies(db_session, a, [b.id])
    set_component_dependencies(db_session, b, [c.id])
    view = dependency_view([a, b, c, d])
    assert [x.name for x in view["used_by"][c.id]] == ["B"]
    assert view["used_by"][a.id] == []
    # C can't depend on A or B (both already depend on C); D is free.
    assert view["blocked"][c.id] == {a.id, b.id}
    assert view["blocked"][a.id] == set()
    assert view["blocked"][d.id] == set()
