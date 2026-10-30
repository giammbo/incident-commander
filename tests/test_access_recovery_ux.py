"""Runtime regression coverage for account/contact recovery (UX02–04/29)."""

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import ContactMethod, Role, User
from app.routers import account, users
from app.security.passwords import verify_password
from app.services.invites import create_invite
from app.services.users import bootstrap_admin, create_user
from app.settings_store import smtp_settings


@pytest.fixture
def client(db_session, get_db_override):
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override
    return TestClient(app)


def _user(db, client, *, admin=False, forced=False):
    bootstrap_admin(db, "admin@localhost")
    user = create_user(
        db,
        email="user@x.io",
        name="User",
        role=Role.admin if admin else Role.read_only,
        password="Password-123",
        must_change_password=forced,
    )
    db.commit()
    client.post(
        "/login", data={"email": user.email, "password": "Password-123"}, follow_redirects=False
    )
    return user


def _contact(db, user):
    row = ContactMethod(
        user_id=user.id, kind="phone", value="+393331234567", verified_at=datetime.now(UTC)
    )
    db.add(row)
    db.commit()
    return row


def test_phone_replace_failure_keeps_verified_contact(client, db_session, monkeypatch):
    user = _user(db_session, client)
    old = _contact(db_session, user)

    def unavailable(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(account.oncall_twilio, "send_sms", unavailable)
    response = client.post("/account/notifications/phone", data={"phone": "+393339999999"})
    assert "Could not send" in response.text
    db_session.expire_all()
    active = db_session.scalar(
        select(ContactMethod).where(ContactMethod.user_id == user.id, ContactMethod.kind == "phone")
    )
    assert active is not None and active.id == old.id
    assert active.value == "+393331234567" and active.verified_at


def test_phone_wrong_expired_and_successful_replacement(client, db_session, monkeypatch):
    user = _user(db_session, client)
    old = _contact(db_session, user)
    codes = []
    monkeypatch.setattr(
        account.oncall_twilio, "send_sms", lambda settings, to, body: codes.append(body.split()[-1])
    )
    client.post("/account/notifications/phone", data={"phone": "+393339999999"})
    pending = db_session.scalar(select(ContactMethod).where(ContactMethod.kind == "phone_pending"))
    assert pending is not None
    response = client.get("/account/notifications")
    assert "+393331234567" in response.text and "+393339999999" in response.text
    client.post("/account/notifications/phone/verify", data={"code": "wrong"})
    assert db_session.get(ContactMethod, old.id).verified_at
    pending.verification_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    client.post("/account/notifications/phone/verify", data={"code": codes[0]})
    assert db_session.get(ContactMethod, old.id).verified_at
    client.post("/account/notifications/phone", data={"phone": "+393339999999"})
    client.post("/account/notifications/phone/verify", data={"code": codes[-1]})
    rows = list(db_session.scalars(select(ContactMethod).where(ContactMethod.user_id == user.id)))
    assert len(rows) == 1 and rows[0].kind == "phone"
    assert rows[0].value == "+393339999999" and rows[0].verified_at


def test_invite_delivery_failure_can_be_resent(client, db_session, monkeypatch):
    _user(db_session, client, admin=True)
    smtp = smtp_settings(db_session)
    smtp.host, smtp.from_address = "smtp.example", "bot@x.io"
    db_session.commit()

    def unavailable(*args, **kwargs):
        raise RuntimeError("mail unavailable")

    monkeypatch.setattr(users, "send_email", unavailable)
    client.post("/users", data={"email": "new@x.io", "name": "New", "role": "read_only"})
    target = db_session.scalar(select(User).where(User.email == "new@x.io"))
    assert target and not target.is_active
    delivered = []
    monkeypatch.setattr(
        users, "send_email", lambda settings, **kwargs: delivered.append(kwargs["text_body"])
    )
    response = client.post(f"/users/{target.id}/resend-invite")
    assert response.status_code == 200 and "Invitation emailed" in response.text
    assert len(delivered) == 1
    token = re.search(r"token=([\w-]+)", delivered[0]).group(1)
    response = client.post(
        "/invite/accept",
        data={"token": token, "new_password": "New-Password-123", "confirm": "New-Password-123"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    db_session.refresh(target)
    assert target.is_active and verify_password("New-Password-123", target.password_hash)
    assert client.get(f"/invite/accept?token={token}").text.count('name="new_password"') == 0


def test_failed_reset_preserves_existing_credentials_and_old_link(client, db_session, monkeypatch):
    admin = _user(db_session, client, admin=True)
    target = create_user(
        db_session, email="target@x.io", name="Target", role=Role.read_only, password="Existing-123"
    )
    _, token = create_invite(db_session, email=target.email, created_by=admin.id)
    smtp = smtp_settings(db_session)
    smtp.host, smtp.from_address = "smtp.example", "bot@x.io"
    db_session.commit()

    def unavailable(*args, **kwargs):
        raise RuntimeError("mail unavailable")

    monkeypatch.setattr(users, "send_email", unavailable)
    response = client.post(f"/users/{target.id}/reset-password")
    assert "failed" in response.text.lower()
    assert "Password setup link pending" in response.text
    db_session.refresh(target)
    assert target.is_active and verify_password("Existing-123", target.password_hash)
    assert 'name="new_password"' in client.get(f"/invite/accept?token={token}").text


def test_reset_without_smtp_and_reactivate_are_admin_actions(client, db_session):
    _user(db_session, client, admin=True)
    target = create_user(
        db_session, email="target@x.io", name="Target", role=Role.read_only, password="Existing-123"
    )
    target.is_active = False
    db_session.commit()
    response = client.post(f"/users/{target.id}/reactivate")
    db_session.refresh(target)
    assert response.status_code == 200 and target.is_active
    response = client.post(f"/users/{target.id}/reset-password")
    db_session.refresh(target)
    assert "Temporary password:" in response.text and target.must_change_password
    assert not verify_password("Existing-123", target.password_hash)
    client.get("/logout")
    client.post("/login", data={"email": target.email, "password": "Existing-123"})
    assert client.post(
        f"/users/{target.id}/reset-password", follow_redirects=False
    ).status_code in (303, 401, 403)


def test_login_preserves_safe_next_through_failed_attempt_and_password_change(client, db_session):
    _user(db_session, client, forced=True)
    client.get("/logout")
    response = client.get("/account/notifications?tab=phone", follow_redirects=False)
    parsed = urlsplit(response.headers["location"])
    assert parsed.path == "/login"
    assert parse_qs(parsed.query)["next"] == ["/account/notifications?tab=phone"]
    response = client.post(
        "/login",
        data={
            "email": "user@x.io",
            "password": "wrong",
            "next": "/account/notifications?tab=phone",
        },
    )
    assert response.status_code == 401 and 'name="next"' in response.text
    response = client.post(
        "/login",
        data={
            "email": "user@x.io",
            "password": "Password-123",
            "next": "/account/notifications?tab=phone",
        },
        follow_redirects=False,
    )
    assert response.headers["location"] == "/account/password"
    response = client.post(
        "/account/password",
        data={"new_password": "Changed-123", "confirm": "Changed-123"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/account/notifications?tab=phone"


@pytest.mark.parametrize(
    "next_path",
    [
        "https://evil.example/",
        "//evil.example/",
        "/\\evil.example",
        "/%2f%2fevil.example",
        "/%5cevil.example",
        "/login",
        "/logout",
    ],
)
def test_login_rejects_unsafe_next(client, db_session, next_path):
    _user(db_session, client)
    client.get("/logout")
    response = client.post(
        "/login",
        data={"email": "user@x.io", "password": "Password-123", "next": next_path},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/"


def test_expired_invite_page_does_not_offer_password_form(client, db_session):
    admin, _ = bootstrap_admin(db_session, "admin@localhost")
    create_user(db_session, email="invite@x.io", name="Invite", role=Role.read_only)
    invite, token = create_invite(db_session, email="invite@x.io", created_by=admin.id)
    invite.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    response = client.get(f"/invite/accept?token={token}")
    assert "expired" in response.text.lower()
    assert 'name="new_password"' not in response.text


def test_worker_available_at_root_without_login(client):
    response = client.get("/oncall-sw.js", follow_redirects=False)
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert response.headers["service-worker-allowed"] == "/"
    assert "notificationclick" in response.text


def test_successful_resend_revokes_previous_link_and_admin_can_revoke(
    client, db_session, monkeypatch
):
    admin = _user(db_session, client, admin=True)
    target = create_user(db_session, email="invite@x.io", name="Invite", role=Role.read_only)
    target.is_active = False
    _, old_token = create_invite(db_session, email=target.email, created_by=admin.id)
    smtp = smtp_settings(db_session)
    smtp.host, smtp.from_address = "smtp.example", "bot@x.io"
    db_session.commit()
    delivered = []
    monkeypatch.setattr(
        users, "send_email", lambda settings, **kwargs: delivered.append(kwargs["text_body"])
    )
    response = client.post(f"/users/{target.id}/resend-invite")
    assert response.status_code == 200 and delivered
    assert 'name="new_password"' not in client.get(f"/invite/accept?token={old_token}").text
    token = re.search(r"token=([\w-]+)", delivered[0]).group(1)
    assert 'name="new_password"' in client.get(f"/invite/accept?token={token}").text
    client.post(f"/users/{target.id}/revoke-invite")
    assert 'name="new_password"' not in client.get(f"/invite/accept?token={token}").text


def test_recovery_routes_forbid_readonly_user(client, db_session):
    user = _user(db_session, client)
    for action in ("resend-invite", "revoke-invite", "reactivate", "reset-password"):
        response = client.post(f"/users/{user.id}/{action}", follow_redirects=False)
        assert response.status_code == 403


def test_users_defaults_to_readonly_and_validation_preserves_fields(client, db_session):
    _user(db_session, client, admin=True)
    response = client.get("/users")
    assert re.search(r'<option[^>]*value="read_only"[^>]*selected', response.text)
    response = client.post(
        "/users", data={"email": "retain@x.io", "name": "   ", "role": "read_only"}
    )
    assert response.status_code == 400
    assert "retain@x.io" in response.text
    assert db_session.scalar(select(User).where(User.email == "retain@x.io")) is None


def test_sso_keeps_safe_next_and_state_check(client, db_session, monkeypatch):
    from app.routers import oidc_auth
    from app.settings_store import sso_settings

    sso = sso_settings(db_session)
    sso.sso_enabled, sso.issuer, sso.client_id, sso.client_secret = (
        True,
        "https://idp",
        "cid",
        "secret",
    )
    db_session.commit()
    monkeypatch.setattr(
        oidc_auth.oidc,
        "discover",
        lambda issuer: {
            "authorization_endpoint": "https://idp/a",
            "token_endpoint": "https://idp/t",
            "jwks_uri": "https://idp/jwks",
        },
    )
    monkeypatch.setattr(oidc_auth.oidc, "exchange_code", lambda **kw: {"id_token": "token"})
    monkeypatch.setattr(
        oidc_auth.oidc,
        "verify_id_token",
        lambda **kw: {"sub": "user", "email": "sso@x.io", "email_verified": True},
    )
    client.get("/login?next=%2Faccount%2Fnotifications%3Ftab%3Dpush")
    response = client.get("/login/oidc", follow_redirects=False)
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    response = client.get(
        "/auth/oidc/callback", params={"code": "c", "state": state}, follow_redirects=False
    )
    assert response.headers["location"] == "/account/notifications?tab=push"


def test_successful_password_reset_link_preserves_access_until_accepted(
    client, db_session, monkeypatch
):
    _user(db_session, client, admin=True)
    target = create_user(
        db_session, email="target@x.io", name="Target", role=Role.read_only, password="Existing-123"
    )
    smtp = smtp_settings(db_session)
    smtp.host, smtp.from_address = "smtp.example", "bot@x.io"
    db_session.commit()
    delivered = []
    monkeypatch.setattr(
        users, "send_email", lambda settings, **kwargs: delivered.append(kwargs["text_body"])
    )
    client.post(f"/users/{target.id}/reset-password")
    assert "password reset" in delivered[0].lower()
    db_session.refresh(target)
    assert target.is_active and verify_password("Existing-123", target.password_hash)
    token = re.search(r"token=([\w-]+)", delivered[0]).group(1)
    response = client.post(
        "/invite/accept",
        data={
            "token": token,
            "new_password": "Reset-Password-123",
            "confirm": "Reset-Password-123",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    db_session.refresh(target)
    assert verify_password("Reset-Password-123", target.password_hash)
    assert not verify_password("Existing-123", target.password_hash)


@pytest.mark.parametrize("linked", [True, False])
def test_sso_does_not_reactivate_disabled_existing_account(client, db_session, monkeypatch, linked):
    from app.routers import oidc_auth
    from app.settings_store import sso_settings

    target = create_user(db_session, email="disabled@x.io", name="Disabled", role=Role.read_only)
    target.is_active = False
    if linked:
        target.oidc_issuer, target.oidc_subject = "https://idp", "disabled-sub"
    sso = sso_settings(db_session)
    sso.sso_enabled, sso.issuer, sso.client_id, sso.client_secret = (
        True,
        "https://idp",
        "cid",
        "secret",
    )
    db_session.commit()
    monkeypatch.setattr(
        oidc_auth.oidc,
        "discover",
        lambda issuer: {
            "authorization_endpoint": "https://idp/a",
            "token_endpoint": "https://idp/t",
            "jwks_uri": "https://idp/jwks",
        },
    )
    monkeypatch.setattr(oidc_auth.oidc, "exchange_code", lambda **kw: {"id_token": "token"})
    monkeypatch.setattr(
        oidc_auth.oidc,
        "verify_id_token",
        lambda **kw: {"sub": "disabled-sub", "email": "disabled@x.io", "email_verified": True},
    )
    response = client.get("/login/oidc", follow_redirects=False)
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    response = client.get(
        "/auth/oidc/callback", params={"code": "c", "state": state}, follow_redirects=False
    )
    db_session.refresh(target)
    assert target.is_active is False
    assert target.oidc_subject == ("disabled-sub" if linked else None)
    assert response.headers["location"] == "/login"
    assert client.get("/account/notifications", follow_redirects=False).status_code == 303
