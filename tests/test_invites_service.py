from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import Invite, Role
from app.security.passwords import verify_password
from app.services.invites import accept_invite, create_invite, hash_token
from app.services.users import bootstrap_admin, create_user


def test_create_and_accept(db_session):
    bootstrap_admin(db_session, "admin@localhost")
    u = create_user(db_session, email="dev@x.io", name="Dev", role=Role.read_only)
    u.is_active = False
    db_session.flush()
    invite, token = create_invite(db_session, email="dev@x.io", created_by=1)
    db_session.flush()
    assert invite.token_hash == hash_token(token)
    accepted = accept_invite(db_session, token=token, new_password="Brand-New-1")
    db_session.flush()
    assert accepted.id == u.id
    assert accepted.is_active is True
    assert accepted.must_change_password is False
    assert verify_password("Brand-New-1", accepted.password_hash)


def test_reused_token_rejected(db_session):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(db_session, email="dev@x.io", name="Dev", role=Role.read_only)
    db_session.flush()
    _, token = create_invite(db_session, email="dev@x.io", created_by=1)
    db_session.flush()
    accept_invite(db_session, token=token, new_password="Brand-New-1")
    db_session.flush()
    with pytest.raises(ValueError):
        accept_invite(db_session, token=token, new_password="Another-1")


def test_expired_token_rejected(db_session):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(db_session, email="dev@x.io", name="Dev", role=Role.read_only)
    db_session.flush()
    _, token = create_invite(db_session, email="dev@x.io", created_by=1, ttl_hours=72)
    inv = db_session.scalar(select(Invite).where(Invite.email == "dev@x.io"))
    inv.expires_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.flush()
    with pytest.raises(ValueError):
        accept_invite(db_session, token=token, new_password="Another-1")


def test_unknown_token_rejected(db_session):
    with pytest.raises(ValueError):
        accept_invite(db_session, token="nope", new_password="Another-1")
