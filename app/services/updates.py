from __future__ import annotations

from sqlalchemy.orm import Session


def post_update(db: Session, incident, *, message, status_id=None, by_user) -> str | None:
    from app.services.incidents import set_incident_status
    from app.services.timeline import log_event

    message = (message or "").strip()
    if not message:
        raise ValueError("Update message cannot be empty")
    kind = None
    if status_id is not None and status_id != incident.status_id:
        kind = set_incident_status(db, incident, status_id=status_id, by_user=by_user)
    log_event(db, incident, entry_type="update", body=message, created_by=by_user)
    return kind
