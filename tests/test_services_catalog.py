from app.services.catalog import create_service, set_service_dependencies


def test_create_and_set_dependencies(db_session):
    a = create_service(db_session, name="Checkout", description=None, created_by=None)
    b = create_service(db_session, name="Payments", description=None, created_by=None)
    c = create_service(db_session, name="Database", description=None, created_by=None)
    db_session.flush()
    set_service_dependencies(db_session, a, [b.id, c.id, a.id])  # self is ignored
    db_session.flush()
    db_session.refresh(a)
    assert sorted(s.name for s in a.depends_on) == ["Database", "Payments"]
