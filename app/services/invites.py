from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import Invite, User
from app.security.passwords import hash_password


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def lock_invite_user(db: Session, email: str) -> User:
    """Serialize recovery changes on the account before locking any invitation."""
    user = db.scalar(
        select(User)
        .where(User.email == email.strip().lower())
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None:
        raise ValueError("No account for this invitation")
    return user


def create_invite(
    db: Session, *, email: str, created_by: int, ttl_hours: int = 72
) -> tuple[Invite, str]:
    token = secrets.token_urlsafe(32)
    invite = Invite(
        email=email.strip().lower(),
        token_hash=hash_token(token),
        expires_at=datetime.now(UTC) + timedelta(hours=ttl_hours),
        created_by=created_by,
    )
    db.add(invite)
    db.flush()
    return invite, token


def valid_invite(db: Session, token: str, *, lock: bool = False) -> Invite:
    query = select(Invite).where(Invite.token_hash == hash_token(token))
    invite = db.scalar(query) if token else None
    if lock and invite is not None:
        # Match send/revoke/deactivate ordering. Refresh after the wait: another
        # transaction may have revoked or consumed the link in the meantime.
        lock_invite_user(db, invite.email)
        invite = db.scalar(query.with_for_update().execution_options(populate_existing=True))
    if invite is None or invite.accepted_at is not None:
        raise ValueError("Invalid or already-used invitation")
    expires_at = invite.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC):
        raise ValueError("Invitation has expired")
    if db.scalar(select(User.id).where(User.email == invite.email)) is None:
        raise ValueError("No account for this invitation")
    return invite


def revoke_invites(db: Session, email: str, *, except_id: int | None = None) -> None:
    lock_invite_user(db, email)
    query = update(Invite).where(
        Invite.email == email.strip().lower(), Invite.accepted_at.is_(None)
    )
    if except_id is not None:
        query = query.where(Invite.id != except_id)
    db.execute(query.values(expires_at=datetime.now(UTC)))


def accept_invite(db: Session, *, token: str, new_password: str) -> User:
    invite = valid_invite(db, token, lock=True)
    if len(new_password) < 8:
        raise ValueError("Use at least 8 characters for your password")
    user = db.scalar(select(User).where(User.email == invite.email))
    if user is None:
        raise ValueError("No account for this invitation")
    user.password_hash = hash_password(new_password)
    user.is_active = True
    user.must_change_password = False
    invite.accepted_at = datetime.now(UTC)
    revoke_invites(db, invite.email, except_id=invite.id)
    db.flush()
    return user
