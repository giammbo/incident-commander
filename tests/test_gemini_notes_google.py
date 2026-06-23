from app.services import google


class _Exec:
    def __init__(self, val):
        self._val = val

    def execute(self):
        return self._val


class _Events:
    def __init__(self, event):
        self._event = event

    def get(self, **kwargs):
        return _Exec(self._event)


class _Files:
    def __init__(self, exported):
        self._exported = exported

    def export(self, **kwargs):
        return _Exec(self._exported)


class _Cal:
    def __init__(self, event):
        self._event = event

    def events(self):
        return _Events(self._event)


class _Drv:
    def __init__(self, exported):
        self._exported = exported

    def files(self):
        return _Files(self._exported)


def _patch(monkeypatch, event, exported=b"Gemini summary text"):
    monkeypatch.setattr(google, "_google_services", lambda **kw: (_Cal(event), _Drv(exported)))


def test_fetch_returns_notes_text(monkeypatch):
    event = {
        "attachments": [
            {
                "fileId": "doc1",
                "title": "Incident: X - Notes by Gemini",
                "mimeType": "application/vnd.google-apps.document",
            },
        ]
    }
    _patch(monkeypatch, event)
    text = google.fetch_gemini_notes_text(
        client_id="c", client_secret="s", refresh_token="r", calendar_id="primary", event_id="e1"
    )
    assert text == "Gemini summary text"


def test_fetch_none_when_no_doc_attachment(monkeypatch):
    event = {"attachments": [{"fileId": "v1", "title": "recording", "mimeType": "video/mp4"}]}
    _patch(monkeypatch, event)
    assert (
        google.fetch_gemini_notes_text(
            client_id="c",
            client_secret="s",
            refresh_token="r",
            calendar_id="primary",
            event_id="e1",
        )
        is None
    )


def test_fetch_none_when_no_attachments(monkeypatch):
    _patch(monkeypatch, {})
    assert (
        google.fetch_gemini_notes_text(
            client_id="c",
            client_secret="s",
            refresh_token="r",
            calendar_id="primary",
            event_id="e1",
        )
        is None
    )
