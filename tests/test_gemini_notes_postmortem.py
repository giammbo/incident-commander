import pytest
import sqlalchemy as sa

from app.models import SeverityLevel
from app.services import postmortems as pm
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


def _incident(db):
    statuses.seed_status_levels(db)
    db.flush()
    sev = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db.add(sev)
    db.flush()
    inc = create_incident(db, title="X", severity_level_id=sev.id, is_private=False, created_by=1)
    db.flush()
    return inc


def _enable_google_sa(db):
    g = google_settings(db)
    g.service_account_json = '{"type": "service_account"}'
    g.impersonate_email = "bot@example.com"
    db.flush()


def test_upsert_appends_then_replaces():
    body = "## Summary\n\nstuff"
    out = pm._upsert_notes_section(body, "first notes")
    assert (
        "## Meeting notes (Gemini)" in out and "first notes" in out and out.startswith("## Summary")
    )
    out2 = pm._upsert_notes_section(out, "second notes")
    assert "second notes" in out2 and "first notes" not in out2  # replaced, not duplicated
    assert out2.count("gemini-notes:start") == 1


def test_upsert_idempotent_when_notes_contain_end_marker():
    # a note that contains our literal end-marker must not corrupt the section / break re-upsert
    nasty = "talked about X <!-- gemini-notes:end --> and then Y"
    out = pm._upsert_notes_section("## Summary\n\nstuff", nasty)
    assert out.count("gemini-notes:start") == 1 and out.count("gemini-notes:end") == 1
    out2 = pm._upsert_notes_section(out, "clean second notes")
    assert out2.count("gemini-notes:start") == 1 and out2.count("gemini-notes:end") == 1
    assert "clean second notes" in out2 and "talked about X" not in out2  # cleanly replaced


def test_render_template_includes_notes_when_set(db_session):
    inc = _incident(db_session)
    inc.gemini_notes = "what we discussed"
    body = pm.render_template(inc, events=[], follow_ups=[], roles_by_type={}, role_types=[])
    assert "Meeting notes (Gemini)" in body and "what we discussed" in body


def test_maybe_pull_noop_without_space_name(db_session):
    inc = _incident(db_session)
    assert pm.maybe_pull_gemini_notes(db_session, inc) is False  # no meet_space_name


def test_maybe_pull_fetches_and_upserts(db_session, monkeypatch):
    inc = _incident(db_session)
    _enable_google_sa(db_session)
    inc.meet_space_name = "spaces/abc-def"
    existing = pm.generate(db_session, inc, by_user=1)  # postmortem already exists
    db_session.flush()
    from app.services import google

    monkeypatch.setattr(google, "fetch_gemini_notes_text", lambda **kw: "GEMINI NOTES")
    assert pm.maybe_pull_gemini_notes(db_session, inc) is True
    assert inc.gemini_notes == "GEMINI NOTES"
    assert "GEMINI NOTES" in existing.body  # upserted into the existing postmortem


def test_maybe_pull_throttled_and_swallows_errors(db_session, monkeypatch):
    inc = _incident(db_session)
    _enable_google_sa(db_session)
    inc.meet_space_name = "spaces/abc-def"
    from app.services import google

    def boom(**kw):
        raise RuntimeError("drive down")

    monkeypatch.setattr(google, "fetch_gemini_notes_text", boom)
    assert pm.maybe_pull_gemini_notes(db_session, inc) is False  # error swallowed
    assert inc.gemini_notes is None
    # the throttle timestamp is written even on the failure path (so the caller can persist
    # it and the next request won't re-hit Google)
    assert (inc.creation_state or {}).get("gemini_notes_try")
    # second immediate call is throttled (no second attempt within 120s)
    calls = []
    monkeypatch.setattr(google, "fetch_gemini_notes_text", lambda **kw: calls.append(1) or "x")
    assert pm.maybe_pull_gemini_notes(db_session, inc) is False
    assert calls == []  # throttled, fetch not called again
