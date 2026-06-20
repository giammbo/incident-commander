from app.models import Group, Role, User, effective_role


def test_effective_role_is_highest(db_session):
    u = User(email="a@x.io", name="A", password_hash="h")
    g_ic = Group(name="Responders", role=Role.incident_commander)
    g_ro = Group(name="Viewers", role=Role.read_only)
    u.groups = [g_ro, g_ic]
    db_session.add(u)
    db_session.flush()
    assert effective_role(u) == Role.incident_commander


def test_effective_role_none_without_groups(db_session):
    u = User(email="b@x.io", name="B", password_hash="h")
    db_session.add(u)
    db_session.flush()
    assert effective_role(u) is None
