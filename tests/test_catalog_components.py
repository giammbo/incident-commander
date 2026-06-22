from app.services.catalog import create_component, create_system, set_component_dependencies


def test_create_and_set_dependencies(db_session):
    s = create_system(db_session, name="Core")
    a = create_component(db_session, name="Checkout", system_id=s.id)
    b = create_component(db_session, name="Payments", system_id=s.id)
    c = create_component(db_session, name="Database", system_id=s.id)
    db_session.flush()
    set_component_dependencies(db_session, a, [b.id, c.id, a.id])
    db_session.flush()
    db_session.refresh(a)
    assert sorted(s.name for s in a.depends_on) == ["Database", "Payments"]
