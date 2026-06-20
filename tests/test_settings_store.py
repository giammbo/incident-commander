from app.settings_store import app_settings, slack_settings


def test_singleton_get_or_create(db_session):
    s1 = app_settings(db_session)
    s1.slack_channel_name_template = "inc-{slug}"
    db_session.flush()
    s2 = app_settings(db_session)
    assert s1.id == s2.id == 1
    assert s2.slack_channel_name_template == "inc-{slug}"


def test_secret_is_encrypted_at_rest(db_session):
    s = slack_settings(db_session)
    s.client_secret = "shhh"
    db_session.flush()
    from sqlalchemy import text

    raw = db_session.execute(text("SELECT client_secret FROM slack_settings WHERE id=1")).scalar()
    assert raw is not None and raw != "shhh"  # ciphertext on disk
    assert slack_settings(db_session).client_secret == "shhh"  # decrypted on read
