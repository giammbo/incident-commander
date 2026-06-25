import pytest
import sqlalchemy as sa

from app.models import SeverityLevel
from app.services import incident_actions as actions
from app.services import statuses
from app.services.incidents import create_incident
from app.settings_store import google_settings


@pytest.fixture(autouse=True)
def _seed_user(db_session):
    db_session.execute(
        sa.text(
            "INSERT INTO users (id, email, name, is_active, must_change_password, is_protected_admin) "
            "VALUES (1, 'seed@test.local', 'Seed', true, false, false) ON CONFLICT (id) DO NOTHING"
        )
    )
    db_session.execute(sa.text("SELECT setval('users_id_seq', (SELECT MAX(id) FROM users))"))
    db_session.flush()


def _setup(db):
    statuses.seed_status_levels(db)
    db.flush()
    sev = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db.add(sev)
    db.flush()
    inc = create_incident(db, title="X", severity_level_id=sev.id, is_private=False, created_by=1)
    db.flush()
    g = google_settings(db)
    g.enabled = True
    g.service_account_json = '{"type": "service_account"}'
    g.impersonate_email = "bot@example.com"
    db.flush()
    return inc


def test_open_google_creates_meet_space(db_session, monkeypatch):
    inc = _setup(db_session)
    monkeypatch.setattr(
        actions.google,
        "create_meet_space",
        lambda **k: ("https://meet.google.com/abc-defg-hij", "spaces/abc-defg-hij"),
    )
    actions.open_incident_google(db_session, inc)
    assert inc.meet_url == "https://meet.google.com/abc-defg-hij"
    assert inc.meet_space_name == "spaces/abc-defg-hij"
    assert inc.creation_state["meet"] == "ok"
    assert inc.creation_state["smart_notes"] == "ok"


def test_meet_space_failure_does_not_break_incident(db_session, monkeypatch):
    inc = _setup(db_session)

    def boom(**k):
        raise RuntimeError("meet api down")

    monkeypatch.setattr(actions.google, "create_meet_space", boom)
    actions.open_incident_google(db_session, inc)  # must not raise
    assert inc.meet_url is None
    assert inc.creation_state["meet"] == "failed"
    assert inc.creation_state["smart_notes"] == "failed"


def test_open_google_skipped_when_not_configured(db_session, monkeypatch):
    inc = _setup(db_session)
    g = google_settings(db_session)
    g.enabled = False
    db_session.flush()
    called = []
    monkeypatch.setattr(
        actions.google,
        "create_meet_space",
        lambda **k: called.append(1) or ("https://meet.google.com/x", "spaces/x"),
    )
    actions.open_incident_google(db_session, inc)
    assert not called
    assert inc.creation_state["meet"] == "skipped"
