from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Incident, IncidentStatus


def create_incident(
    db: Session,
    *,
    title: str,
    severity_level_id: int | None,
    is_private: bool,
    created_by: int,
    description: str | None = None,
    slack_connection_id: int | None = None,
    google_connection_id: int | None = None,
    service_ids: list[int] | None = None,
) -> Incident:
    inc = Incident(
        title=title.strip(),
        description=description,
        severity_level_id=severity_level_id,
        is_private=is_private,
        created_by=created_by,
        slack_connection_id=slack_connection_id,
        google_connection_id=google_connection_id,
        creation_state={"channel": "skipped", "meet": "skipped", "announce": "skipped"},
    )
    if service_ids:
        from app.models import Service

        inc.services = list(db.scalars(select(Service).where(Service.id.in_(service_ids))))
    db.add(inc)
    db.flush()
    return inc


def list_incidents(db: Session, status: IncidentStatus | None = None) -> list[Incident]:
    stmt = select(Incident).order_by(Incident.created_at.desc(), Incident.id.desc())
    if status is not None:
        stmt = stmt.where(Incident.status == status)
    return list(db.scalars(stmt))


def update_incident(db, incident, *, title, description, severity_level_id, service_ids):
    from app.models import Service

    incident.title = title.strip()
    incident.description = description
    incident.severity_level_id = severity_level_id
    incident.services = (
        list(db.scalars(select(Service).where(Service.id.in_(service_ids)))) if service_ids else []
    )
    db.flush()
    return incident


def close_incident(db: Session, incident: Incident, closed_by: int) -> Incident:
    if incident.status == IncidentStatus.closed:
        raise ValueError("Incident already closed")
    incident.status = IncidentStatus.closed
    incident.closed_by = closed_by
    incident.closed_at = datetime.now(UTC)
    db.flush()
    return incident
