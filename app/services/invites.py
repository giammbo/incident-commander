from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Invite, User
from app.security.passwords import hash_password


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


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


def accept_invite(db: Session, *, token: str, new_password: str) -> User:
    invite = db.scalar(select(Invite).where(Invite.token_hash == hash_token(token)))
    if invite is None or invite.accepted_at is not None:
        raise ValueError("Invalid or already-used invitation")
    expires_at = invite.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at < datetime.now(UTC):
        raise ValueError("Invitation has expired")
    user = db.scalar(select(User).where(User.email == invite.email))
    if user is None:
        raise ValueError("No account for this invitation")
    user.password_hash = hash_password(new_password)
    user.is_active = True
    user.must_change_password = False
    invite.accepted_at = datetime.now(UTC)
    db.flush()
    return user
