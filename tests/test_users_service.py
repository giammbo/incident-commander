import pytest

from app.models import Role, effective_role
from app.services.users import (
    authenticate_local,
    bootstrap_admin,
    create_user,
    deactivate_user,
    set_user_groups,
)


def test_bootstrap_creates_protected_admin_once(db_session):
    user, pw = bootstrap_admin(db_session, "admin@localhost")
    db_session.flush()
    assert user.is_protected_admin is True
    assert user.must_change_password is True
    assert pw is not None and len(pw) > 0
    assert effective_role(user) == Role.admin
    # idempotent
    user2, pw2 = bootstrap_admin(db_session, "admin@localhost")
    assert pw2 is None
    assert user2.id == user.id


def test_create_user_assigns_role(db_session):
    bootstrap_admin(db_session, "admin@localhost")
    u = create_user(
        db_session,
        email="IC@Example.com",
        name="Ic",
        role=Role.incident_commander,
        password="pw123456",
    )
    db_session.flush()
    assert u.email == "ic@example.com"
    assert effective_role(u) == Role.incident_commander


def test_cannot_strip_protected_admin(db_session):
    admin, _ = bootstrap_admin(db_session, "admin@localhost")
    db_session.flush()
    with pytest.raises(ValueError):
        set_user_groups(db_session, admin, [])
    with pytest.raises(ValueError):
        deactivate_user(db_session, admin)


def test_authenticate_local(db_session):
    bootstrap_admin(db_session, "admin@localhost")
    u = create_user(db_session, email="a@x.io", name="A", role=Role.read_only, password="hunter2!")
    db_session.flush()
    assert authenticate_local(db_session, "nonexistent@x.io", "whatever") is None
    assert authenticate_local(db_session, "a@x.io", "hunter2!").id == u.id
    assert authenticate_local(db_session, "a@x.io", "nope") is None
    u.is_active = False
    db_session.flush()
    assert authenticate_local(db_session, "a@x.io", "hunter2!") is None
