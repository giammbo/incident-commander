from __future__ import annotations

import smtplib
from email.message import EmailMessage

from app.models import SmtpSettings


def is_smtp_configured(s: SmtpSettings) -> bool:
    return bool(s.host and s.from_address)


def send_email(s: SmtpSettings, *, to: str, subject: str, text_body: str, html_body: str) -> None:
    if not is_smtp_configured(s):
        raise RuntimeError("SMTP is not configured")

    msg = EmailMessage()
    msg["From"] = s.from_address
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(s.host, s.port or 25, timeout=10) as client:
        if s.use_tls:
            client.starttls()
        if s.username and s.password:
            client.login(s.username, s.password)
        client.send_message(msg)
