import app.services.google as g


def test_meet_scopes_include_space_created():
    assert "https://www.googleapis.com/auth/meetings.space.created" in g.MEET_SCOPES
