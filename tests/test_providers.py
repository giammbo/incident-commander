from types import SimpleNamespace

import app.services.providers as providers


def test_parse_video_choice():
    assert providers.parse_video_choice("") == (None, None)
    assert providers.parse_video_choice("jitsi") == (None, None)
    assert providers.parse_video_choice("meet:5") == ("meet", 5)
    assert providers.parse_video_choice("meet:abc") == (None, None)  # non-numeric id
    assert providers.parse_video_choice("unknown") == (None, None)  # unknown provider


def test_registry_shapes():
    assert providers.VIDEO_PROVIDERS["meet"].needs_connection is True
    assert providers.CHAT_PROVIDERS["slack"].key == "slack"


def test_meet_provider_delegates(db_session, monkeypatch):
    import app.services.incident_actions as actions

    called = {}
    monkeypatch.setattr(
        actions,
        "open_incident_google",
        lambda db, inc, conn: called.setdefault("meet", (inc, conn)),
    )
    inc = SimpleNamespace()
    conn = SimpleNamespace()
    providers.VIDEO_PROVIDERS["meet"].create(db_session, inc, connection=conn)
    assert called["meet"] == (inc, conn)


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
