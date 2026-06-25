from app.settings_store import google_settings


def test_google_settings_sa_roundtrip(db_session):
    g = google_settings(db_session)
    g.service_account_json = '{"type":"service_account","x":1}'
    g.impersonate_email = "bot@example.com"
    db_session.flush()
    db_session.expire(g)
    g2 = google_settings(db_session)
    assert g2.impersonate_email == "bot@example.com"
    assert '"service_account"' in g2.service_account_json  # decrypted transparently
