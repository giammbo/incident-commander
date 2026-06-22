from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FollowUp, User

_STATES = ("open", "completed", "cancelled")


def _resolve_assignee(db: Session, assignee_id):
    if assignee_id is None:
        return None
    u = db.scalar(select(User).where(User.id == assignee_id, User.is_active.is_(True)))
    return u.id if u else None


def create_followup(
    db, incident, *, title, description=None, assignee_id=None, due_on=None, created_by
):
    from app.services.timeline import log_event

    title = (title or "").strip()
    if not title:
        raise ValueError("Title is required")
    fu = FollowUp(
        incident_id=incident.id,
        title=title,
        description=description,
        assignee_id=_resolve_assignee(db, assignee_id),
        due_on=due_on,
        status="open",
        created_by=created_by,
    )
    db.add(fu)
    db.flush()
    log_event(
        db, incident, entry_type="followup", body=f"Follow-up added: {title}", created_by=created_by
    )
    return fu


def update_followup(db, followup, *, title, description, assignee_id, due_on):
    title = (title or "").strip()
    if not title:
        raise ValueError("Title is required")
    followup.title = title
    followup.description = description
    followup.assignee_id = _resolve_assignee(db, assignee_id)
    followup.due_on = due_on
    db.flush()
    return followup


def set_followup_status(db, followup, *, status, by_user) -> str:
    from app.services.timeline import log_event

    if status not in _STATES:
        raise ValueError("Invalid status")
    followup.status = status
    if status == "open":
        followup.resolved_at = None
        followup.resolved_by = None
    else:
        followup.resolved_at = datetime.now(UTC)
        followup.resolved_by = by_user
    verb = "reopened" if status == "open" else status
    log_event(
        db,
        followup.incident,
        entry_type="followup",
        body=f"Follow-up {verb}: {followup.title}",
        created_by=by_user,
    )
    db.flush()
    return status


def delete_followup(db, followup_id) -> None:
    fu = db.get(FollowUp, followup_id)
    if fu is None:
        raise ValueError("Follow-up not found")
    db.delete(fu)
    db.flush()


def list_followups(incident) -> list[FollowUp]:
    return sorted(incident.follow_ups, key=lambda f: (f.status != "open", f.created_at))


def list_open_followups(db) -> list[FollowUp]:
    return list(
        db.scalars(
            select(FollowUp)
            .where(FollowUp.status == "open")
            .order_by(FollowUp.incident_id, FollowUp.created_at)
        )
    )


def open_count(incident) -> int:
    return sum(1 for f in incident.follow_ups if f.status == "open")
