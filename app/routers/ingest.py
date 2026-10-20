from __future__ import annotations

import json
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Alert, InboundIntegration
from app.services import alert_adapters as adapters
from app.services import alerts as alerts_service
from app.services import automation, webhooks

router = APIRouter()

_MAX_BODY_BYTES = 262_144  # 256 KB cap on untrusted public-endpoint payloads


def _confirm_subscription(url: str) -> None:
    host = urlparse(url).hostname or ""
    if not host.endswith(".amazonaws.com"):
        return
    httpx.get(url, timeout=5)


@router.post("/ingest/{token}")
async def ingest(token: str, request: Request, db: Session = Depends(get_db)):
    integ = db.scalar(
        select(InboundIntegration).where(
            InboundIntegration.token == token, InboundIntegration.enabled.is_(True)
        )
    )
    if integ is None:
        return JSONResponse({"error": "unknown integration"}, status_code=401)
    raw = await request.body()
    if len(raw) > _MAX_BODY_BYTES:  # cap untrusted public-endpoint payloads
        return JSONResponse({"error": "payload too large"}, status_code=413)
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    try:
        if integ.kind == "sns":
            result = adapters.parse_sns(body, verify=True)
            if isinstance(result, dict) and "confirm_url" in result:
                _confirm_subscription(result["confirm_url"])
                return JSONResponse({"ok": "subscription confirmed"})
            normalized = result
        elif integ.kind == "alertmanager":
            normalized = adapters.parse_alertmanager(body)
        else:
            normalized = adapters.parse_generic(body, integ.settings or {})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    created = []
    for n in normalized:
        prior_status = db.scalar(
            select(Alert.status).where(
                Alert.integration_id == integ.id, Alert.dedup_key == n["dedup_key"]
            )
        )
        alert = alerts_service.ingest_alert(db, integ, n)
        if alert.status == "firing" and prior_status in (None, "resolved"):
            created += automation.run_rules(db, trigger="alert.received", alert=alert, by_user=None)
    db.commit()
    # "opened" webhook for incidents auto-created by alert rules, after the commit
    # (as POST /incidents does); notify is best-effort, so ingest never 500s on it.
    for inc in created:
        webhooks.notify(db, inc, "opened", base_url=get_settings().base_url)
    return JSONResponse({"ok": "ingested", "count": len(normalized)})
