import pytest
from fastapi.testclient import TestClient

import app.services.incident_actions as actions
from app.db import get_db
from app.main import create_app
from app.models import GoogleConnection, Role, SeverityLevel, SlackConnection
from app.services import statuses
from app.services.incidents import create_incident
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _login(client, db_session, *, role=Role.incident_commander, email="ic@x.io"):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(db_session, email=email, name="U", role=role, password="pw-123456")
    db_session.flush()
    client.post("/login", data={"email": email, "password": "pw-123456"})


def _open_incident(db_session, **kw):
    statuses.seed_status_levels(db_session)
    db_session.flush()
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db_session.add(lvl)
    db_session.flush()
    inc = create_incident(
        db_session, title="X", severity_level_id=lvl.id, is_private=False, created_by=1, **kw
    )
    db_session.flush()
    return inc


def test_add_meet_creates_meet(client, db_session, monkeypatch):
    _login(client, db_session)
    g = GoogleConnection(account_email="ops@x.io", refresh_token="r", created_by=1)
    db_session.add(g)
    inc = _open_incident(db_session)
    db_session.flush()
    monkeypatch.setattr(
        actions.google, "create_meet", lambda **k: ("https://meet.google.com/abc-defg-hij", "evt-1")
    )
    r = client.post(
        f"/incidents/{inc.id}/add-meet",
        data={"video": f"meet:{g.id}"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db_session.refresh(inc)
    assert inc.meet_url == "https://meet.google.com/abc-defg-hij"


def test_add_meet_posts_link_to_existing_channel(client, db_session, monkeypatch):
    _login(client, db_session)
    g = GoogleConnection(account_email="ops@x.io", refresh_token="r", created_by=1)
    s = SlackConnection(team_id="T1", team_name="Acme", bot_token="xoxb-1", created_by=1)
    db_session.add_all([g, s])
    inc = _open_incident(db_session)
    inc.slack_connection_id = s.id
    inc.slack_channel_id = "C1"  # channel already exists
    db_session.flush()
    posts = []
    monkeypatch.setattr(
        actions.google, "create_meet", lambda **k: ("https://meet.google.com/xyz", "evt-1")
    )
    monkeypatch.setattr(actions.slack, "post_message", lambda token, **k: posts.append(k["text"]))
    client.post(
        f"/incidents/{inc.id}/add-meet",
        data={"video": f"meet:{g.id}"},
        follow_redirects=False,
    )
    assert any("meet.google.com/xyz" in p for p in posts)


def test_add_meet_rejected_when_meet_exists(client, db_session, monkeypatch):
    _login(client, db_session)
    g = GoogleConnection(account_email="ops@x.io", refresh_token="r", created_by=1)
    db_session.add(g)
    inc = _open_incident(db_session)
    inc.meet_url = "https://meet.google.com/existing"
    db_session.flush()
    called = []
    monkeypatch.setattr(
        actions.google, "create_meet", lambda **k: (called.append(1) or "x", "evt-1")
    )
    r = client.post(
        f"/incidents/{inc.id}/add-meet",
        data={"video": f"meet:{g.id}"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and not called
    db_session.refresh(inc)
    assert inc.meet_url == "https://meet.google.com/existing"


def test_add_meet_rejected_when_closed(client, db_session, monkeypatch):
    from app.services.incidents import close_incident

    _login(client, db_session)
    g = GoogleConnection(account_email="ops@x.io", refresh_token="r", created_by=1)
    db_session.add(g)
    inc = _open_incident(db_session)
    close_incident(db_session, inc, closed_by=1)
    db_session.flush()
    called = []
    monkeypatch.setattr(
        actions.google, "create_meet", lambda **k: (called.append(1) or "x", "evt-1")
    )
    r = client.post(
        f"/incidents/{inc.id}/add-meet",
        data={"video": f"meet:{g.id}"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and not called
    db_session.refresh(inc)
    assert inc.meet_url is None


def test_open_slack_creates_channel(client, db_session, monkeypatch):
    _login(client, db_session)
    s = SlackConnection(team_id="T1", team_name="Acme", bot_token="xoxb-1", created_by=1)
    db_session.add(s)
    inc = _open_incident(db_session)
    db_session.flush()
    monkeypatch.setattr(
        actions.slack, "create_channel", lambda token, **k: {"id": "C9", "name": "inc-x"}
    )
    monkeypatch.setattr(
        actions.slack, "channel_url", lambda team_id, channel_id: "https://slack/C9"
    )
    monkeypatch.setattr(actions.slack, "set_topic_purpose", lambda token, **k: None)
    monkeypatch.setattr(actions.slack, "post_message", lambda token, **k: None)
    r = client.post(
        f"/incidents/{inc.id}/open-slack",
        data={"slack_connection_id": str(s.id)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db_session.refresh(inc)
    assert inc.slack_channel_id == "C9" and inc.slack_connection_id == s.id


def test_open_slack_rejected_when_channel_exists(client, db_session, monkeypatch):
    _login(client, db_session)
    s = SlackConnection(team_id="T1", team_name="Acme", bot_token="xoxb-1", created_by=1)
    db_session.add(s)
    inc = _open_incident(db_session)
    inc.slack_connection_id = s.id
    inc.slack_channel_id = "C1"
    db_session.flush()
    called = []
    monkeypatch.setattr(actions.slack, "create_channel", lambda token, **k: called.append(1) or {})
    r = client.post(
        f"/incidents/{inc.id}/open-slack",
        data={"slack_connection_id": str(s.id)},
        follow_redirects=False,
    )
    assert r.status_code == 303 and not called


def test_readonly_forbidden_on_both(client, db_session):
    _login(client, db_session, role=Role.read_only, email="ro@x.io")
    inc = _open_incident(db_session)
    db_session.flush()
    assert client.post(f"/incidents/{inc.id}/add-meet", data={"video": "meet:1"}).status_code == 403
    assert (
        client.post(
            f"/incidents/{inc.id}/open-slack", data={"slack_connection_id": "1"}
        ).status_code
        == 403
    )


def _enable_slack(db_session):
    from app.settings_store import slack_settings

    s = slack_settings(db_session)
    s.enabled = True
    s.client_id = "cid"
    s.client_secret = "csecret"
    db_session.flush()


def _enable_google(db_session):
    from app.settings_store import google_settings

    g = google_settings(db_session)
    g.enabled = True
    g.client_id = "cid"
    g.client_secret = "csecret"
    db_session.flush()


def test_detail_shows_add_controls_when_missing(client, db_session):
    _login(client, db_session)
    _enable_slack(db_session)
    _enable_google(db_session)
    db_session.add(GoogleConnection(account_email="ops@x.io", refresh_token="r", created_by=1))
    db_session.add(
        SlackConnection(team_id="T1", team_name="Acme", bot_token="xoxb-1", created_by=1)
    )
    inc = _open_incident(db_session)
    db_session.flush()
    html = client.get(f"/incidents/{inc.id}").text
    assert f"/incidents/{inc.id}/add-meet" in html and "Add video" in html
    assert f"/incidents/{inc.id}/open-slack" in html and "Open channel" in html


def test_detail_hides_add_meet_when_present(client, db_session):
    _login(client, db_session)
    _enable_google(db_session)
    db_session.add(GoogleConnection(account_email="ops@x.io", refresh_token="r", created_by=1))
    inc = _open_incident(db_session)
    inc.meet_url = "https://meet.google.com/existing"
    db_session.flush()
    html = client.get(f"/incidents/{inc.id}").text
    assert "/add-meet" not in html and "join" in html


def test_detail_no_add_meet_for_readonly(client, db_session):
    _login(client, db_session, role=Role.read_only, email="ro@x.io")
    _enable_google(db_session)
    db_session.add(GoogleConnection(account_email="ops@x.io", refresh_token="r", created_by=1))
    inc = _open_incident(db_session)
    db_session.flush()
    html = client.get(f"/incidents/{inc.id}").text
    assert "/add-meet" not in html


def test_detail_hides_add_controls_when_closed(client, db_session):
    from app.services.incidents import close_incident

    _login(client, db_session)
    _enable_slack(db_session)
    _enable_google(db_session)
    db_session.add(GoogleConnection(account_email="ops@x.io", refresh_token="r", created_by=1))
    db_session.add(
        SlackConnection(team_id="T1", team_name="Acme", bot_token="xoxb-1", created_by=1)
    )
    inc = _open_incident(db_session)
    close_incident(db_session, inc, closed_by=1)
    db_session.flush()
    html = client.get(f"/incidents/{inc.id}").text
    assert "/add-meet" not in html and "/open-slack" not in html


def test_detail_hides_add_controls_without_connections(client, db_session):
    _login(client, db_session)
    _enable_slack(db_session)
    _enable_google(db_session)
    # integrations enabled but NO connections created
    inc = _open_incident(db_session)
    db_session.flush()
    html = client.get(f"/incidents/{inc.id}").text
    # No Slack connection → open-channel hidden; no Google connection → video add-control also hidden
    assert "/open-slack" not in html
    assert f"/incidents/{inc.id}/add-meet" not in html
