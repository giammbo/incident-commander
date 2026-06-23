from app.services.google import MEET_SCOPES


def test_meet_scopes_include_drive_readonly():
    assert "https://www.googleapis.com/auth/drive.readonly" in MEET_SCOPES


def test_incident_gemini_notes_column(db_session):
    import sqlalchemy as sa

    from app.models import Incident, SeverityLevel
    from app.services import statuses
    from app.services.incidents import create_incident

    statuses.seed_status_levels(db_session)
    db_session.execute(
        sa.text(
            "INSERT INTO users (id, email, name, is_active, must_change_password, is_protected_admin) "
            "VALUES (1, 'seed@test.local', 'Seed', true, false, false) ON CONFLICT (id) DO NOTHING"
        )
    )
    db_session.execute(sa.text("SELECT setval('users_id_seq', (SELECT MAX(id) FROM users))"))
    sev = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db_session.add(sev)
    db_session.flush()
    inc = create_incident(
        db_session, title="X", severity_level_id=sev.id, is_private=False, created_by=1
    )
    db_session.flush()
    assert inc.gemini_notes is None
    inc.gemini_notes = "notes"
    db_session.flush()
    assert db_session.get(Incident, inc.id).gemini_notes == "notes"
