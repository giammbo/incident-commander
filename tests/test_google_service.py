import app.services.google as g
from app.services.google import authorize_url


def test_authorize_url_offline_has_consent():
    url = authorize_url(
        client_id="cid", redirect_uri="https://x/cb", state="st", scopes=g.MEET_SCOPES, offline=True
    )
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "access_type=offline" in url and "prompt=consent" in url and "state=st" in url


def test_authorize_url_login_no_offline():
    url = authorize_url(
        client_id="cid", redirect_uri="https://x/cb", state="st", scopes=g.SSO_SCOPES
    )
    assert "access_type=offline" not in url
    assert "scope=openid" in url or "scope=openid+email+profile" in url or "openid" in url


class FakeEvents:
    def __init__(self, store):
        self.store = store

    def insert(self, calendarId, conferenceDataVersion, body):
        self.store["inserted"] = {
            "calendarId": calendarId,
            "cdv": conferenceDataVersion,
            "body": body,
        }
        return _Exec({"id": "ev1", "hangoutLink": "https://meet.google.com/abc-defg-hij"})

    def get(self, calendarId, eventId):
        return _Exec({"id": eventId, "hangoutLink": "https://meet.google.com/abc-defg-hij"})


class _Exec:
    def __init__(self, data):
        self._data = data

    def execute(self):
        return self._data


class FakeService:
    def __init__(self, store):
        self._events = FakeEvents(store)

    def events(self):
        return self._events


def test_create_meet_returns_link(monkeypatch):
    store = {}
    monkeypatch.setattr(g, "_calendar_service", lambda **kw: FakeService(store))
    link, event_id = g.create_meet(
        client_id="c",
        client_secret="s",
        refresh_token="r",
        calendar_id="primary",
        summary="Incident: X",
        now_iso="2026-06-20T10:00:00Z",
        end_iso="2026-06-20T11:00:00Z",
        attempts=3,
        sleep=lambda *_: None,
    )
    assert link == "https://meet.google.com/abc-defg-hij"
    assert event_id == "ev1"
    body = store["inserted"]["body"]
    assert (
        body["conferenceData"]["createRequest"]["conferenceSolutionKey"]["type"] == "hangoutsMeet"
    )
    assert store["inserted"]["cdv"] == 1
