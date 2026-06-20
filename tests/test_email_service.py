import smtplib

import pytest

from app.models import SmtpSettings
from app.services.email import is_smtp_configured, send_email


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.started_tls = False
        self.logged_in = None
        self.sent = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, u, p):
        self.logged_in = (u, p)

    def send_message(self, msg):
        self.sent = msg


def test_is_smtp_configured():
    assert is_smtp_configured(SmtpSettings(id=1, host="smtp.x", from_address="a@x.io")) is True
    assert is_smtp_configured(SmtpSettings(id=1)) is False


def test_send_email_uses_tls_and_login(monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    s = SmtpSettings(
        id=1,
        host="smtp.x",
        port=587,
        username="u",
        password="p",
        from_address="bot@x.io",
        use_tls=True,
    )
    send_email(s, to="dev@x.io", subject="Hi", text_body="t", html_body="<b>t</b>")
    sent = FakeSMTP.instances[-1]
    assert sent.started_tls is True
    assert sent.logged_in == ("u", "p")
    assert sent.sent["To"] == "dev@x.io"
    assert sent.sent["From"] == "bot@x.io"
    assert sent.sent["Subject"] == "Hi"


def test_send_email_unconfigured_raises():
    with pytest.raises(RuntimeError):
        send_email(SmtpSettings(id=1), to="x@x.io", subject="s", text_body="t", html_body="h")
