from __future__ import annotations

import base64
import json
from urllib.parse import urlparse

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.x509 import load_pem_x509_certificate

_NOTIF_KEYS = ["Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"]
_SUB_KEYS = ["Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"]
_cert_cache: dict[str, bytes] = {}


def _default_fetch_cert(url: str) -> bytes:
    return httpx.get(url, timeout=5).content


def _canonical(env: dict) -> bytes:
    keys = (
        _SUB_KEYS
        if env.get("Type") in ("SubscriptionConfirmation", "UnsubscribeConfirmation")
        else _NOTIF_KEYS
    )
    parts: list[str] = []
    for k in keys:
        if k in env and env[k] is not None:
            parts.append(k)
            parts.append(str(env[k]))
    return ("\n".join(parts) + "\n").encode("utf-8")


def verify_sns_signature(env: dict, *, fetch_cert=None) -> bool:
    cert_url = env.get("SigningCertURL", "")
    host = urlparse(cert_url).hostname or ""
    if not host.endswith(".amazonaws.com"):
        return False
    fetch = fetch_cert or _default_fetch_cert
    try:
        if cert_url not in _cert_cache:
            raw = fetch(cert_url)
            load_pem_x509_certificate(raw)  # validate before caching so a transient
            _cert_cache[cert_url] = raw  # bad fetch can't poison the cache (fail-closed forever)
        cert = load_pem_x509_certificate(_cert_cache[cert_url])
        algo = hashes.SHA256() if str(env.get("SignatureVersion")) == "2" else hashes.SHA1()
        cert.public_key().verify(
            base64.b64decode(env["Signature"]), _canonical(env), padding.PKCS1v15(), algo
        )
        return True
    except (InvalidSignature, ValueError, KeyError):
        return False


def _cloudwatch_normalized(msg: dict) -> dict:
    region = (msg.get("AlarmArn", "").split(":") + [""] * 6)[3] or "us-east-1"
    name = msg.get("AlarmName", "alarm")
    state = msg.get("NewStateValue", "ALARM")
    url = (
        f"https://{region}.console.aws.amazon.com/cloudwatch/home"
        f"?region={region}#alarmsV2:alarm/{name}"
    )
    return {
        "source": "cloudwatch",
        "dedup_key": msg.get("AlarmArn") or name,
        "title": f"{name} — {msg.get('NewStateReason', state)}",
        "description": msg.get("NewStateReason"),
        "severity_raw": None,
        "status": "firing" if state in ("ALARM", "INSUFFICIENT_DATA") else "resolved",
        "links": [{"label": "CloudWatch", "url": url}],
        "payload": msg,
    }


def _eventbridge_normalized(msg: dict) -> dict:
    return {
        "source": "eventbridge" if "detail-type" in msg else "generic",
        "dedup_key": str(msg.get("id") or msg.get("detail-type") or "sns-message"),
        "title": str(msg.get("detail-type") or "SNS message"),
        "description": json.dumps(msg.get("detail", {}))[:1000] or None,
        "severity_raw": None,
        "status": "firing",
        "links": [],
        "payload": msg,
    }


def _safe_url(url) -> str | None:
    """Only http(s) links may reach a rendered href — blocks javascript:/data: schemes
    in provider-supplied URLs (the alerts inbox renders these in an <a href>)."""
    return url if isinstance(url, str) and url.startswith(("http://", "https://")) else None


def parse_sns(envelope: dict, *, verify: bool, fetch_cert=None):
    mtype = envelope.get("Type")
    if mtype == "SubscriptionConfirmation":
        url = envelope.get("SubscribeURL")
        if not url:
            raise ValueError("SubscriptionConfirmation missing SubscribeURL")
        return {"confirm_url": url}
    if mtype != "Notification":
        return []
    if verify and not verify_sns_signature(envelope, fetch_cert=fetch_cert):
        raise ValueError("SNS signature verification failed")
    try:
        msg = json.loads(envelope.get("Message", "{}"))
    except (json.JSONDecodeError, TypeError):
        msg = {"raw": envelope.get("Message")}
    if isinstance(msg, dict) and "AlarmArn" in msg and "NewStateValue" in msg:
        return [_cloudwatch_normalized(msg)]
    return [_eventbridge_normalized(msg if isinstance(msg, dict) else {"detail": msg})]


def parse_alertmanager(body: dict) -> list[dict]:
    out = []
    for a in body.get("alerts", []):
        labels = a.get("labels", {}) or {}
        ann = a.get("annotations", {}) or {}
        name = labels.get("alertname", "alert")
        title = f"{name}: {ann['summary']}" if ann.get("summary") else name
        links = []
        if _safe_url(ann.get("runbook_url")):
            links.append({"label": "Runbook", "url": ann["runbook_url"]})
        if _safe_url(a.get("generatorURL")):
            links.append({"label": "Source", "url": a["generatorURL"]})
        out.append(
            {
                "source": "alertmanager",
                "dedup_key": a.get("fingerprint") or name,
                "title": title,
                "description": ann.get("description"),
                "severity_raw": labels.get("severity"),
                "status": "resolved" if a.get("status") == "resolved" else "firing",
                "links": links,
                "payload": a,
            }
        )
    return out


def _dotted(body: dict, path: str | None):
    if not path:
        return None
    cur = body
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def parse_generic(body: dict, settings: dict) -> list[dict]:
    dedup = _dotted(body, settings.get("dedup_key"))
    if dedup is None or dedup == "":
        raise ValueError("generic alert: dedup_key not found at configured path")
    status_val = _dotted(body, settings.get("status"))
    resolved = status_val is not None and str(status_val) == str(
        settings.get("resolved_value", "resolved")
    )
    return [
        {
            "source": "generic",
            "dedup_key": str(dedup),
            "title": str(_dotted(body, settings.get("title")) or "Alert"),
            "description": None,
            "severity_raw": (lambda s: str(s) if s is not None else None)(
                _dotted(body, settings.get("severity"))
            ),
            "status": "resolved" if resolved else "firing",
            "links": [],
            "payload": body,
        }
    ]
