import app.services.slack as slack
from app.services.slack import (
    authorize_url,
    build_channel_name,
    channel_url,
    sanitize_channel_name,
)


def test_sanitize_channel_name():
    assert sanitize_channel_name("Checkout DOWN!! 5xx") == "checkout-down-5xx"
    assert len(sanitize_channel_name("x" * 200)) == 80


def test_build_channel_name():
    assert (
        build_channel_name("inc-{date}-{slug}", title="Checkout 5xx", date_str="20260620")
        == "inc-20260620-checkout-5xx"
    )


def test_authorize_url_has_scopes_and_state():
    url = authorize_url(
        client_id="cid",
        redirect_uri="https://x/cb",
        state="st",
        scopes=["channels:manage", "chat:write"],
    )
    assert url.startswith("https://slack.com/oauth/v2/authorize?")
    assert "client_id=cid" in url and "state=st" in url
    assert "channels%3Amanage" in url or "channels:manage" in url


def test_channel_url():
    assert "C123" in channel_url("T999", "C123")


class FakeResp:
    def __init__(self, data):
        self.data = data


class FakeClient:
    last = {}

    def __init__(self, token=None):
        self.token = token

    def conversations_create(self, name, is_private):
        FakeClient.last["create"] = (self.token, name, is_private)
        return FakeResp({"channel": {"id": "C123", "name": name}})

    def conversations_setTopic(self, channel, topic):
        FakeClient.last["topic"] = (channel, topic)
        return FakeResp({"ok": True})

    def conversations_setPurpose(self, channel, purpose):
        FakeClient.last["purpose"] = (channel, purpose)
        return FakeResp({"ok": True})

    def chat_postMessage(self, channel, text):
        FakeClient.last["post"] = (channel, text)
        return FakeResp({"ok": True})


def test_create_channel_and_post(monkeypatch):
    monkeypatch.setattr(slack, "WebClient", FakeClient)
    res = slack.create_channel("xoxb-1", name="inc-x", is_private=False)
    assert res == {"id": "C123", "name": "inc-x"}
    assert FakeClient.last["create"] == ("xoxb-1", "inc-x", False)
    slack.set_topic_purpose("xoxb-1", channel_id="C123", topic="SEV1", purpose="Incident")
    assert FakeClient.last["topic"] == ("C123", "SEV1")
    slack.post_message("xoxb-1", channel_id="C123", text="opened")
    assert FakeClient.last["post"] == ("C123", "opened")
