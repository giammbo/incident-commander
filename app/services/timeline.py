from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import IncidentEvent


def log_event(
    db: Session,
    incident,
    *,
    entry_type: str,
    body: str,
    created_by: int | None = None,
    pinned: bool = False,
) -> IncidentEvent:
    ev = IncidentEvent(
        incident_id=incident.id,
        entry_type=entry_type,
        body=body,
        created_by=created_by,
        pinned=pinned,
    )
    db.add(ev)
    db.flush()
    return ev


def add_note(db: Session, incident, *, body: str, created_by: int) -> IncidentEvent:
    body = (body or "").strip()
    if not body:
        raise ValueError("Note cannot be empty")
    return log_event(db, incident, entry_type="note", body=body, created_by=created_by)


def toggle_pin(db: Session, event_id: int) -> bool:
    ev = db.get(IncidentEvent, event_id)
    if ev is None:
        raise ValueError("Event not found")
    ev.pinned = not ev.pinned
    db.flush()
    return ev.pinned


def delete_note(db: Session, event_id: int) -> None:
    ev = db.get(IncidentEvent, event_id)
    if ev is None:
        raise ValueError("Event not found")
    if ev.entry_type != "note":
        raise ValueError("Only notes can be deleted")
    db.delete(ev)
    db.flush()


def list_events(incident) -> list[IncidentEvent]:
    return sorted(incident.events, key=lambda e: (e.pinned, e.created_at, e.id), reverse=True)
