from types import SimpleNamespace

import app.services.providers as providers


def test_parse_video_choice():
    assert providers.parse_video_choice("") is None
    assert providers.parse_video_choice("jitsi") is None
    assert providers.parse_video_choice("meet") == "meet"
    assert providers.parse_video_choice("unknown") is None


def test_registry_shapes():
    assert providers.VIDEO_PROVIDERS["meet"].needs_connection is False
    assert providers.CHAT_PROVIDERS["slack"].key == "slack"


def test_meet_provider_delegates(db_session, monkeypatch):
    import app.services.incident_actions as actions

    called = {}
    monkeypatch.setattr(
        actions,
        "open_incident_google",
        lambda db, inc: called.setdefault("meet", inc),
    )
    inc = SimpleNamespace()
    conn = SimpleNamespace()
    providers.VIDEO_PROVIDERS["meet"].create(db_session, inc, connection=conn)
    assert called["meet"] is inc


def test_slack_provider_delegates(db_session, monkeypatch):
    import app.services.incident_actions as actions

    seen = []
    for fn in (
        "open_incident_slack",
        "update_incident_slack",
        "close_incident_slack",
        "announce_meet_in_slack",
    ):
        monkeypatch.setattr(actions, fn, (lambda name: lambda db, inc, conn: seen.append(name))(fn))
    inc, conn = SimpleNamespace(), SimpleNamespace()
    p = providers.CHAT_PROVIDERS["slack"]
    p.open_room(db_session, inc, connection=conn)
    p.post_update(db_session, inc, connection=conn)
    p.post_closed(db_session, inc, connection=conn)
    p.announce_video(db_session, inc, connection=conn)
    assert seen == [
        "open_incident_slack",
        "update_incident_slack",
        "close_incident_slack",
        "announce_meet_in_slack",
    ]
