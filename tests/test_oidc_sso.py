from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.routers.oidc_auth as oidc_auth
from app.db import get_db
from app.main import create_app
from app.models import Role, User
from app.services.users import bootstrap_admin, create_user
from app.settings_store import sso_settings


@pytest.fixture
def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _configure_sso(db_session, *, allowed_domains="acme.io", issuer="https://idp"):
    s = sso_settings(db_session)
    s.sso_enabled = True
    s.issuer = issuer
    s.client_id = "cid"
    s.client_secret = "csec"
    s.display_name = "Acme ID"
    s.allowed_domains = allowed_domains
    s.auto_provision_role = Role.read_only
    db_session.flush()


def _mock_idp(monkeypatch, claims):
    monkeypatch.setattr(
        oidc_auth.oidc,
        "discover",
        lambda issuer: {
            "authorization_endpoint": "https://idp/a",
            "token_endpoint": "https://idp/t",
            "jwks_uri": "https://idp/jwks",
            "issuer": "https://idp",
        },
    )
    monkeypatch.setattr(oidc_auth.oidc, "exchange_code", lambda **k: {"id_token": "tok"})
    monkeypatch.setattr(oidc_auth.oidc, "verify_id_token", lambda **k: claims)


def _do_login(client):
    r0 = client.get("/login/oidc", follow_redirects=False)
    assert r0.status_code in (302, 307)
    state = parse_qs(urlparse(r0.headers["location"]).query)["state"][0]
    return client.get(
        "/auth/oidc/callback", params={"code": "c", "state": state}, follow_redirects=False
    )


def test_entra_style_no_hd_auto_provisions(client, db_session, monkeypatch):
    _configure_sso(db_session)
    _mock_idp(
        monkeypatch,
        {
            "iss": "https://idp",
            "sub": "u1",
            "email": "alice@acme.io",
            "email_verified": True,
            "name": "Alice",
        },
    )
    r = _do_login(client)
    assert r.status_code == 303
    u = db_session.scalar(select(User).where(User.email == "alice@acme.io"))
    assert u is not None and u.oidc_subject == "u1" and u.oidc_issuer == "https://idp"
    assert [g.role for g in u.groups] == [Role.read_only]


def test_email_verified_false_rejected(client, db_session, monkeypatch):
    _configure_sso(db_session)
    _mock_idp(
        monkeypatch,
        {"iss": "https://idp", "sub": "u2", "email": "bob@acme.io", "email_verified": False},
    )
    r = _do_login(client)
    assert r.status_code == 303
    assert db_session.scalar(select(User).where(User.email == "bob@acme.io")) is None


def test_domain_not_allowed_rejected(client, db_session, monkeypatch):
    _configure_sso(db_session, allowed_domains="acme.io")
    _mock_idp(
        monkeypatch,
        {"iss": "https://idp", "sub": "u3", "email": "eve@evil.io", "email_verified": True},
    )
    _do_login(client)
    assert db_session.scalar(select(User).where(User.email == "eve@evil.io")) is None


def test_empty_allowed_domains_allows_any(client, db_session, monkeypatch):
    _configure_sso(db_session, allowed_domains="")
    _mock_idp(
        monkeypatch,
        {"iss": "https://idp", "sub": "u4", "email": "x@whatever.io", "email_verified": True},
    )
    _do_login(client)
    assert db_session.scalar(select(User).where(User.email == "x@whatever.io")) is not None


def test_google_hd_gate(client, db_session, monkeypatch):
    _configure_sso(db_session, allowed_domains="acme.io", issuer="https://accounts.google.com")
    monkeypatch.setattr(
        oidc_auth.oidc,
        "discover",
        lambda issuer: {
            "authorization_endpoint": "https://g/a",
            "token_endpoint": "https://g/t",
            "jwks_uri": "https://g/jwks",
            "issuer": "https://accounts.google.com",
        },
    )
    monkeypatch.setattr(oidc_auth.oidc, "exchange_code", lambda **k: {"id_token": "tok"})
    # hd present but NOT in allowed → rejected even though email domain would pass
    monkeypatch.setattr(
        oidc_auth.oidc,
        "verify_id_token",
        lambda **k: {
            "iss": "https://accounts.google.com",
            "sub": "g1",
            "email": "u@acme.io",
            "email_verified": True,
            "hd": "other.io",
        },
    )
    _do_login(client)
    assert db_session.scalar(select(User).where(User.email == "u@acme.io")) is None


def test_no_takeover_of_password_account(client, db_session, monkeypatch):
    _configure_sso(db_session)
    create_user(
        db_session,
        email="alice@acme.io",
        name="Alice",
        role=Role.incident_commander,
        password="pw-123456",
    )
    db_session.flush()
    _mock_idp(
        monkeypatch,
        {"iss": "https://idp", "sub": "u9", "email": "alice@acme.io", "email_verified": True},
    )
    _do_login(client)
    u = db_session.scalar(select(User).where(User.email == "alice@acme.io"))
    assert u.oidc_subject is None  # not linked/taken over


def test_break_glass_local_login_for_protected_admin(client, db_session):
    _, pw = bootstrap_admin(db_session, "admin@localhost")
    s = sso_settings(db_session)
    s.sso_enabled, s.allow_local_login = True, False
    db_session.flush()
    r = client.post(
        "/login", data={"email": "admin@localhost", "password": pw}, follow_redirects=False
    )
    assert r.status_code == 303  # protected admin can always log in locally


def test_state_mismatch_rejected(client, db_session, monkeypatch):
    _configure_sso(db_session)
    called = []
    monkeypatch.setattr(
        oidc_auth.oidc,
        "discover",
        lambda issuer: {
            "authorization_endpoint": "https://idp/a",
            "token_endpoint": "https://idp/t",
            "jwks_uri": "https://idp/jwks",
            "issuer": "https://idp",
        },
    )
    monkeypatch.setattr(
        oidc_auth.oidc,
        "exchange_code",
        lambda **k: called.append("exchange") or {"id_token": "tok"},
    )
    monkeypatch.setattr(oidc_auth.oidc, "verify_id_token", lambda **k: {"sub": "x"})
    client.get("/login/oidc", follow_redirects=False)  # establishes the real session state
    r = client.get(
        "/auth/oidc/callback", params={"code": "c", "state": "FORGED"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert "exchange" not in called  # rejected before token exchange
