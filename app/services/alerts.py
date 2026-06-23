from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Alert, InboundIntegration


def ingest_alert(db: Session, integration: InboundIntegration, n: dict) -> Alert:
    alert = db.scalar(
        select(Alert).where(
            Alert.integration_id == integration.id, Alert.dedup_key == n["dedup_key"]
        )
    )
    now = datetime.now(UTC)
    if alert is None:
        alert = Alert(
            integration_id=integration.id,
            source=n["source"],
            dedup_key=n["dedup_key"],
            first_seen_at=now,
        )
        db.add(alert)
    else:
        alert.occurrence_count += 1
    alert.title = n["title"]
    alert.description = n.get("description")
    alert.severity_raw = n.get("severity_raw")
    alert.links = n.get("links", [])
    alert.payload = n.get("payload", {})
    alert.last_seen_at = now
    if n["status"] == "resolved":
        alert.status = "resolved"
        alert.resolved_at = now
    else:
        alert.status = "firing"
        alert.resolved_at = None
    db.flush()
    return alert


def list_alerts(db: Session, *, status: str | None = None) -> list[Alert]:
    stmt = select(Alert).order_by(Alert.last_seen_at.desc())
    if status is not None:
        stmt = stmt.where(Alert.status == status)
    return list(db.scalars(stmt))


def attach_alert_to_incident(db: Session, alert: Alert, incident, *, by_user) -> None:
    from app.services.timeline import log_event

    alert.incident_id = incident.id
    log_event(
        db, incident, entry_type="alert", body=f"Linked alert: {alert.title}", created_by=by_user
    )
    db.flush()


def resolve_alert(db: Session, alert: Alert) -> None:
    alert.status = "resolved"
    alert.resolved_at = datetime.now(UTC)
    db.flush()
