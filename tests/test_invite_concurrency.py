"""Real PostgreSQL interleavings; email is captured locally, never delivered."""

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event
from time import monotonic

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models import Invite, Role, User
from app.routers import users
from app.security.passwords import verify_password
from app.services.invites import accept_invite, create_invite, hash_token
from app.services.users import bootstrap_admin, create_user
from app.settings_store import smtp_settings


def _account(db):
    admin, _ = bootstrap_admin(db, "admin@localhost")
    target = create_user(
        db, email="race@example.test", name="Race", role=Role.read_only, password="Existing-123"
    )
    smtp = smtp_settings(db)
    smtp.host, smtp.from_address = "smtp.example.test", "bot@example.test"
    db.commit()
    return admin.id, target.id


def _wait_for_lock_or_completion(engine, pid, future):
    """Release the competing transaction only once PostgreSQL confirms the wait."""
    deadline = monotonic() + 10
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        while monotonic() < deadline:
            if future.done():
                future.result()
                return
            waiting = connection.scalar(
                text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": pid},
            )
            if waiting:
                return
            Event().wait(0.01)
    raise AssertionError("Competing transaction neither completed nor waited on a PostgreSQL lock")


@pytest.mark.parametrize("second_fails", [False, True])
def test_overlapping_resend_reset_preserves_last_successful_delivery(
    db_session, pg_engine, monkeypatch, second_fails
):
    admin_id, target_id = _account(db_session)
    first_sending, release_first, second_started = Event(), Event(), Event()
    delivered = []
    second_pid = []

    def fake_email(smtp, **kwargs):
        reset = kwargs["subject"].startswith("Reset")
        if not reset:
            first_sending.set()
            assert release_first.wait(15), "Test did not release the first fake delivery"
        if reset and second_fails:
            raise RuntimeError("fake provider unavailable")
        delivered.append(re.search(r"token=([\w-]+)", kwargs["text_body"]).group(1))

    monkeypatch.setattr(users, "send_email", fake_email)

    def send(*, reset):
        with Session(pg_engine) as db:
            if reset:
                second_pid.append(db.scalar(text("SELECT pg_backend_pid()")))
                second_started.set()
            return users._send_invitation(
                db, db.get(User, target_id), db.get(User, admin_id), reset=reset
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(send, reset=False)
        try:
            assert first_sending.wait(10)
            second = pool.submit(send, reset=True)
            assert second_started.wait(10)
            _wait_for_lock_or_completion(pg_engine, second_pid[0], second)
        finally:
            release_first.set()
        assert "emailed" in first.result(timeout=15)
        second_result = second.result(timeout=15)
        assert ("failed" if second_fails else "emailed") in second_result

    db_session.expire_all()
    valid_hashes = list(
        db_session.scalars(
            select(Invite.token_hash).where(
                Invite.email == "race@example.test",
                Invite.accepted_at.is_(None),
                Invite.expires_at > datetime.now(UTC),
            )
        )
    )
    assert len(delivered) == (1 if second_fails else 2)
    assert valid_hashes == [hash_token(delivered[-1])]
    target = db_session.get(User, target_id)
    assert target.is_active and verify_password("Existing-123", target.password_hash)
    accepted = accept_invite(db_session, token=delivered[-1], new_password="Recovered-123")
    db_session.commit()
    assert verify_password("Recovered-123", accepted.password_hash)


def test_deactivation_wins_over_waiting_invite_acceptance(db_session, pg_engine, monkeypatch):
    admin_id, target_id = _account(db_session)
    _, token = create_invite(db_session, email="race@example.test", created_by=admin_id)
    db_session.commit()
    deactivating, release_deactivate, accept_started = Event(), Event(), Event()
    accept_pid = []
    real_deactivate = users.deactivate_user

    def paused_deactivate(db, target):
        real_deactivate(db, target)
        deactivating.set()
        assert release_deactivate.wait(15), "Test did not release deactivation"

    monkeypatch.setattr(users, "deactivate_user", paused_deactivate)

    def deactivate():
        with Session(pg_engine) as db:
            request = Request({"type": "http", "session": {}})
            return users.users_deactivate(request, target_id, db.get(User, admin_id), db)

    def accept():
        with Session(pg_engine) as db:
            accept_pid.append(db.scalar(text("SELECT pg_backend_pid()")))
            accept_started.set()
            try:
                accept_invite(db, token=token, new_password="Recovered-123")
                db.commit()
            except ValueError as exc:
                db.rollback()
                return str(exc)
            return "accepted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        deactivation = pool.submit(deactivate)
        try:
            assert deactivating.wait(10)
            acceptance = pool.submit(accept)
            assert accept_started.wait(10)
            _wait_for_lock_or_completion(pg_engine, accept_pid[0], acceptance)
        finally:
            release_deactivate.set()
        assert deactivation.result(timeout=15).status_code == 303
        assert "expired" in acceptance.result(timeout=15).lower()

    db_session.expire_all()
    target = db_session.get(User, target_id)
    assert not target.is_active
    assert verify_password("Existing-123", target.password_hash)
