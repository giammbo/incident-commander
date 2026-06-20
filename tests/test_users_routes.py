import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import Group, Role, User
from app.services.users import ADMINS_GROUP, bootstrap_admin, create_user


@pytest.fixture
def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def test_non_admin_forbidden(client, db_session):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(
        db_session, email="ic@x.io", name="IC", role=Role.incident_commander, password="pw-123456"
    )
    db_session.flush()
    client.post("/login", data={"email": "ic@x.io", "password": "pw-123456"})
    assert client.get("/users").status_code == 403


def test_admin_creates_user(client, db_session):
    admin, pw = bootstrap_admin(db_session, "admin@localhost")
    db_session.flush()
    client.post("/login", data={"email": "admin@localhost", "password": pw})
    # admin starts with must_change_password — allow the page anyway for the test by changing it
    client.post("/account/password", data={"new_password": "Admin-123", "confirm": "Admin-123"})
    r = client.post(
        "/users",
        data={"email": "new@x.io", "name": "New", "role": "read_only"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert "new@x.io" in r.text


def test_admin_changes_user_groups(client, db_session):
    admin, pw = bootstrap_admin(db_session, "admin@localhost")
    target = create_user(
        db_session, email="t@x.io", name="T", role=Role.read_only, password="pw-123456"
    )
    db_session.flush()
    ic_group = Group(name="Incident Commanders", role=Role.incident_commander)
    db_session.add(ic_group)
    db_session.flush()
    client.post("/login", data={"email": "admin@localhost", "password": pw})
    client.post("/account/password", data={"new_password": "Admin-123", "confirm": "Admin-123"})

    r = client.post(
        f"/users/{target.id}/groups", data={"group_ids": [ic_group.id]}, follow_redirects=False
    )
    assert r.status_code == 303
    db_session.refresh(target)
    assert [g.id for g in target.groups] == [ic_group.id]


def test_cannot_strip_protected_admin_via_route(client, db_session):
    admin, pw = bootstrap_admin(db_session, "admin@localhost")
    # Commit so the route's rollback (on the guarded ValueError) does not discard
    # the admin; the savepoint recipe keeps the outer transaction active.
    db_session.commit()
    admin_id = admin.id
    client.post("/login", data={"email": "admin@localhost", "password": pw})
    client.post("/account/password", data={"new_password": "Admin-123", "confirm": "Admin-123"})

    # Try to set the protected admin to NO groups → guarded, graceful redirect (no 500).
    r = client.post(f"/users/{admin_id}/groups", data={"group_ids": []}, follow_redirects=False)
    assert r.status_code == 303  # graceful, not a 500
    # The route rolled back the guarded change, so the admin is still in Admins.
    db_session.expire_all()
    reloaded = db_session.scalar(select(User).where(User.id == admin_id))
    assert any(g.name == ADMINS_GROUP for g in reloaded.groups)


def test_readonly_forbidden_from_users(client, db_session):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(db_session, email="ro@x.io", name="RO", role=Role.read_only, password="pw-123456")
    db_session.flush()
    client.post("/login", data={"email": "ro@x.io", "password": "pw-123456"})
    # read_only must be cleared of the forced-change gate to reach /users, then still be denied
    client.post("/account/password", data={"new_password": "Reader-123", "confirm": "Reader-123"})
    assert client.get("/users").status_code == 403


def test_protected_admin_deactivate_route_is_graceful(client, db_session):
    admin, pw = bootstrap_admin(db_session, "admin@localhost")
    db_session.commit()
    admin_id = admin.id
    client.post("/login", data={"email": "admin@localhost", "password": pw})
    client.post("/account/password", data={"new_password": "Admin-123", "confirm": "Admin-123"})

    r = client.post(f"/users/{admin_id}/deactivate", follow_redirects=False)
    assert r.status_code == 303  # guarded, not a 500
    db_session.expire_all()
    reloaded = db_session.scalar(select(User).where(User.id == admin_id))
    assert reloaded.is_active is True


def test_create_user_emails_invite_when_smtp_configured(client, db_session, monkeypatch):
    import app.routers.users as users_router

    sent = {}

    def fake_send_email(s, *, to, subject, text_body, html_body):
        sent["to"] = to
        sent["html"] = html_body

    monkeypatch.setattr(users_router, "send_email", fake_send_email)

    admin, pw = bootstrap_admin(db_session, "admin@localhost")
    db_session.flush()
    from app.settings_store import smtp_settings

    s = smtp_settings(db_session)
    s.host, s.from_address = "smtp.x", "bot@x.io"
    db_session.flush()

    client.post("/login", data={"email": "admin@localhost", "password": pw})
    client.post("/account/password", data={"new_password": "Admin-123", "confirm": "Admin-123"})
    r = client.post(
        "/users",
        data={"email": "dev@x.io", "name": "Dev", "role": "read_only"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert sent["to"] == "dev@x.io"
    assert "/invite/accept?token=" in sent["html"]
    # invited user starts inactive until they accept
    from sqlalchemy import select

    from app.models import User

    u = db_session.scalar(select(User).where(User.email == "dev@x.io"))
    assert u.is_active is False


def test_create_user_keeps_user_when_invite_email_fails(client, db_session, monkeypatch):
    import app.routers.users as users_router

    def boom(*a, **k):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(users_router, "send_email", boom)

    admin, pw = bootstrap_admin(db_session, "admin@localhost")
    db_session.flush()
    from app.settings_store import smtp_settings

    s = smtp_settings(db_session)
    s.host, s.from_address = "smtp.x", "bot@x.io"
    db_session.flush()

    client.post("/login", data={"email": "admin@localhost", "password": pw})
    client.post("/account/password", data={"new_password": "Admin-123", "confirm": "Admin-123"})
    r = client.post(
        "/users",
        data={"email": "dev@x.io", "name": "Dev", "role": "read_only"},
        follow_redirects=True,
    )
    # email failed, but the user is kept (inactive) and the failure is surfaced, not a 500
    assert r.status_code == 200
    assert "failed" in r.text.lower()
    u = db_session.scalar(select(User).where(User.email == "dev@x.io"))
    assert u is not None and u.is_active is False


def test_create_duplicate_email_is_graceful(client, db_session):
    admin, pw = bootstrap_admin(db_session, "admin@localhost")
    create_user(db_session, email="dup@x.io", name="Dup", role=Role.read_only, password="pw-123456")
    db_session.flush()
    client.post("/login", data={"email": "admin@localhost", "password": pw})
    client.post("/account/password", data={"new_password": "Admin-123", "confirm": "Admin-123"})
    r = client.post(
        "/users",
        data={"email": "dup@x.io", "name": "Again", "role": "read_only"},
        follow_redirects=True,
    )
    assert r.status_code == 200  # graceful, not a 500
    assert "already exists" in r.text.lower()
    assert len(list(db_session.scalars(select(User).where(User.email == "dup@x.io")))) == 1
