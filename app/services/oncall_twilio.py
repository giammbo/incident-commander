"""Twilio REST helpers (SMS, voice call) and webhook signature validation.

``post`` is injectable so tests never touch the network. Errors never include
the auth token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Callable
from typing import Any

import httpx

from app.models import OnCallSettings

_BASE = "https://api.twilio.com/2010-04-01/Accounts"


def _request(settings: OnCallSettings, resource: str, data: dict[str, str], post: Callable) -> str:
    sid, token = settings.twilio_account_sid, settings.twilio_auth_token
    if not (sid and token and settings.twilio_from_number):
        raise RuntimeError("Twilio is not configured")
    resp = post(f"{_BASE}/{sid}/{resource}.json", auth=(sid, token), data=data, timeout=10.0)
    body: dict[str, Any] = {}
    try:
        body = resp.json() or {}
    except Exception:  # noqa: BLE001 — non-JSON error body
        body = {}
    if not 200 <= resp.status_code < 300:
        raise RuntimeError(
            f"Twilio error {body.get('code') or resp.status_code}: "
            f"{body.get('message') or 'request failed'}"
        )
    return str(body.get("sid", ""))


def send_sms(settings: OnCallSettings, to: str, body: str, *, post=httpx.post) -> str:
    return _request(
        settings,
        "Messages",
        {"To": to, "From": settings.twilio_from_number or "", "Body": body},
        post,
    )


def place_call(settings: OnCallSettings, to: str, twiml_url: str, *, post=httpx.post) -> str:
    return _request(
        settings,
        "Calls",
        {"To": to, "From": settings.twilio_from_number or "", "Url": twiml_url},
        post,
    )


def valid_signature(auth_token: str, url: str, params: dict[str, str], signature: str) -> bool:
    data = url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature or "")
