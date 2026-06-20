import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.routers.google_auth as gauth
from app.db import get_db
from app.main import create_app
from app.models import Role, User, effective_role
from app.services.users import bootstrap_admin, create_user
from app.settings_store import google_settings, sso_settings


@pytest.fixture
def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _enable_sso(db_session, domains="acme.io"):
    g = google_settings(db_session)
    g.client_id, g.client_secret, g.enabled = "gid", "gsec", True
    s = sso_settings(db_session)
    s.sso_enabled, s.allowed_domains = True, domains
    db_session.flush()


def _login_via_google(client, gauth, monkeypatch, *, email, hd, sub="sub-1", verified=True):
    monkeypatch.setattr(gauth, "exchange_code", lambda **k: {"id_token": "fake"})
    monkeypatch.setattr(
        gauth,
        "verify_id_token",
        lambda tok, **k: {"sub": sub, "email": email, "email_verified": verified, "hd": hd},
    )
    r0 = client.get("/login/google", follow_redirects=False)
    from urllib.parse import parse_qs, urlparse

    state = parse_qs(urlparse(r0.headers["location"]).query)["state"][0]
    return client.get(
        "/auth/google/callback", params={"code": "c", "state": state}, follow_redirects=False
    )


def test_sso_autoprovisions_domain_user(client, db_session, monkeypatch):
    bootstrap_admin(db_session, "admin@localhost")
    _enable_sso(db_session)
    r = _login_via_google(client, gauth, monkeypatch, email="dev@acme.io", hd="acme.io")
    assert r.status_code == 303 and r.headers["location"] == "/"
    u = db_session.scalar(select(User).where(User.email == "dev@acme.io"))
    assert u is not None and u.google_sub == "sub-1" and effective_role(u) == Role.read_only


def test_sso_rejects_wrong_domain(client, db_session, monkeypatch):
    bootstrap_admin(db_session, "admin@localhost")
    _enable_sso(db_session, domains="acme.io")
    r = _login_via_google(client, gauth, monkeypatch, email="evil@other.io", hd="other.io")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert db_session.scalar(select(User).where(User.email == "evil@other.io")) is None


def test_break_glass_blocks_local_but_allows_admin(client, db_session):
    admin, pw = bootstrap_admin(db_session, "admin@localhost")
    create_user(
        db_session, email="ic@x.io", name="IC", role=Role.incident_commander, password="pw-123456"
    )
    s = sso_settings(db_session)
    g = google_settings(db_session)
    g.client_id, g.client_secret, g.enabled = "gid", "gsec", True
    s.sso_enabled, s.allow_local_login = True, False
    db_session.flush()
    # non-admin local login blocked
    r = client.post(
        "/login", data={"email": "ic@x.io", "password": "pw-123456"}, follow_redirects=False
    )
    assert r.status_code == 401
    # protected admin can still log in locally (break-glass)
    r2 = client.post(
        "/login", data={"email": "admin@localhost", "password": pw}, follow_redirects=False
    )
    assert r2.status_code == 303


def test_sso_does_not_take_over_password_account(client, db_session, monkeypatch):
    # A pre-existing local-password account must NOT be silently linked/taken over by an SSO
    # login that merely matches its email within the trusted domain.
    bootstrap_admin(db_session, "admin@localhost")
    _enable_sso(db_session, domains="acme.io")
    existing = create_user(
        db_session,
        email="dev@acme.io",
        name="Dev",
        role=Role.incident_commander,
        password="local-pw-123",
    )
    db_session.flush()
    r = _login_via_google(
        client, gauth, monkeypatch, email="dev@acme.io", hd="acme.io", sub="attacker"
    )
    assert r.status_code == 303 and r.headers["location"] == "/login"
    db_session.refresh(existing)
    assert existing.google_sub is None  # not linked to the attacker's Google sub
