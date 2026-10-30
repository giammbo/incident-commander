"""Structural configuration checks. No network calls, no echoed credentials."""

from __future__ import annotations

import json
import re
import string
from urllib.parse import urlsplit

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key


def http_url(value):
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("Enter a complete HTTP or HTTPS URL without whitespace.")
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme in ("http", "https") and parsed.hostname and parsed.port != 0
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Enter a complete HTTP or HTTPS URL.")
    return value


def masked_destination(value):
    try:
        p = urlsplit(value)
        return (
            f"{p.scheme}://{p.hostname}/… (destination credentials hidden)"
            if p.hostname
            else "Destination configured (hidden)"
        )
    except ValueError:
        return "Destination configured (hidden)"


def email(value):
    return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value or ""))


def google_service_account(value):
    try:
        data = json.loads(value)
        if not isinstance(data, dict) or data.get("type") != "service_account":
            raise ValueError
        if not all(
            isinstance(data.get(k), str) and data[k].strip()
            for k in (
                "project_id",
                "private_key_id",
                "private_key",
                "client_email",
                "client_id",
                "token_uri",
            )
        ):
            raise ValueError
        if (
            not email(data["client_email"])
            or urlsplit(http_url(data["token_uri"])).scheme != "https"
        ):
            raise ValueError
        key = load_pem_private_key(data["private_key"].encode(), password=None)
        if not isinstance(key, RSAPrivateKey):
            raise ValueError
    except (ValueError, TypeError, KeyError, UnsupportedAlgorithm):
        raise ValueError(
            "Use a complete service-account JSON key with type, project_id, private_key_id, valid RSA private_key, client_email, client_id and HTTPS token_uri."
        ) from None
    return value


def channel_template(value):
    if not value or len(value) > 100:
        raise ValueError("Slack channel template is required and must be 100 characters or fewer.")
    try:
        for _, field, spec, conversion in string.Formatter().parse(value):
            if field is not None and (field not in ("date", "slug") or spec or conversion):
                raise ValueError
        rendered = value.format(date="2026-10-29", slug="sample-incident")
        if not re.fullmatch(r"[a-z0-9_-]{1,80}", rendered):
            raise ValueError
    except (ValueError, KeyError, IndexError, AttributeError):
        raise ValueError(
            "Use only {date} and {slug}, with lowercase letters, numbers, hyphens or underscores (80 characters maximum after substitution)."
        ) from None
    return value
