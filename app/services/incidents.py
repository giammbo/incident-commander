from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Incident, StatusCategory, StatusLevel

_UNSET = object()


def _scoped_components(db, system_id, component_ids):
    """Validate and resolve component_ids against system_id; return Component list."""
    from app.models import Component

    ids = list(component_ids or [])
    if not ids:
        return []
    if system_id is None:
        raise ValueError("Pick a system before selecting components")
    comps = list(db.scalars(select(Component).where(Component.id.in_(ids))))
    if any(c.system_id != system_id for c in comps):
        raise ValueError("Selected components must belong to the chosen system")
    return comps


def create_incident(
    db: Session,
    *,
    title: str,
    severity_level_id: int | None,
    is_private: bool,
    created_by: int,
    description: str | None = None,
    slack_connection_id: int | None = None,
    system_id: int | None = None,
    component_ids: list[int] | None = None,
    status_id: int | None = None,
    incident_type_id: int | None = None,
) -> Incident:
    from app.models import IncidentType
    from app.services.catalog import default_severity_level_id
    from app.services.incident_types import default_incident_type_id
    from app.services.statuses import default_status_level_id

    if incident_type_id is None:
        incident_type_id = default_incident_type_id(db)
    if severity_level_id is None:
        itype = db.get(IncidentType, incident_type_id) if incident_type_id else None
        severity_level_id = (
            itype.default_severity_level_id if itype else None
        ) or default_severity_level_id(db)
    components = _scoped_components(db, system_id, component_ids)
    inc = Incident(
        title=title.strip(),
        description=description,
        severity_level_id=severity_level_id,
        status_id=status_id if status_id is not None else default_status_level_id(db),
        is_private=is_private,
        created_by=created_by,
        slack_connection_id=slack_connection_id,
        system_id=system_id,
        creation_state={"channel": "skipped", "meet": "skipped", "announce": "skipped"},
        incident_type_id=incident_type_id,
    )
    inc.components = components
    db.add(inc)
    db.flush()
    from app.services.timeline import log_event

    log_event(db, inc, entry_type="opened", body="Incident declared.", created_by=created_by)
    return inc


def list_incidents(db: Session) -> list[Incident]:
    stmt = select(Incident).order_by(Incident.created_at.desc(), Incident.id.desc())
    return list(db.scalars(stmt))


def update_incident(
    db,
    incident,
    *,
    title=_UNSET,
    description=_UNSET,
    severity_level_id=_UNSET,
    system_id=_UNSET,
    component_ids=_UNSET,
    incident_type_id=_UNSET,
):
    if title is not _UNSET:
        incident.title = title.strip()
    if description is not _UNSET:
        incident.description = description
    if severity_level_id is not _UNSET:
        incident.severity_level_id = severity_level_id
    if system_id is not _UNSET:
        incident.system_id = system_id
        # clearing the system always discards stale components
        if system_id is None:
            component_ids = []
    if component_ids is not _UNSET:
        incident.components = _scoped_components(db, incident.system_id, component_ids)
    if incident_type_id is not _UNSET:
        incident.incident_type_id = incident_type_id
    db.flush()
    return incident


def set_incident_status(db: Session, incident, *, status_id: int, by_user: int) -> str:
    from datetime import UTC, datetime

    from app.services.timeline import log_event

    target = db.get(StatusLevel, status_id)
    if target is None:
        raise ValueError("Unknown status")
    old_label = incident.status.label if incident.status else "—"
    was_closed = incident.is_closed
    incident.status_id = target.id
    incident.status = target
    now_closed = target.category == StatusCategory.closed
    if now_closed and not was_closed:
        incident.closed_at = datetime.now(UTC)
        incident.closed_by = by_user
        kind = "closed"
        log_event(db, incident, entry_type="closed", body="Incident closed.", created_by=by_user)
    elif was_closed and not now_closed:
        incident.closed_at = None
        incident.closed_by = None
        kind = "reopened"
        log_event(
            db, incident, entry_type="reopened", body="Incident reopened.", created_by=by_user
        )
    else:
        kind = "changed"
        log_event(
            db,
            incident,
            entry_type="status_changed",
            body=f"Status changed from {old_label} to {target.label}.",
            created_by=by_user,
        )
    db.flush()
    return kind


def close_incident(db: Session, incident, closed_by: int):
    """Back-compat shortcut: move to the (lowest-rank) closed status."""
    closed = db.scalar(
        select(StatusLevel)
        .where(StatusLevel.category == StatusCategory.closed)
        .order_by(StatusLevel.rank)
    )
    if closed is None:
        raise ValueError("No closed status configured")
    if incident.is_closed:
        raise ValueError("Incident already closed")
    set_incident_status(db, incident, status_id=closed.id, by_user=closed_by)
    return incident


def set_incident_role_assignees(
    db: Session, incident, *, role_type_id: int, user_ids, by_user: int
):
    from app.models import IncidentRoleAssignment, IncidentRoleType, User

    role = db.get(IncidentRoleType, role_type_id)
    if role is None:
        raise ValueError("Unknown role")
    wanted = list(dict.fromkeys(user_ids or []))  # dedup, preserve order
    users = (
        list(db.scalars(select(User).where(User.id.in_(wanted), User.is_active.is_(True))))
        if wanted
        else []
    )
    valid_ids = {u.id for u in users}
    current = list(
        db.scalars(
            select(IncidentRoleAssignment).where(
                IncidentRoleAssignment.incident_id == incident.id,
                IncidentRoleAssignment.role_type_id == role_type_id,
            )
        )
    )
    current_ids = {a.user_id for a in current}
    for a in current:
        if a.user_id not in valid_ids:
            db.delete(a)
    for u in users:
        if u.id not in current_ids:
            db.add(
                IncidentRoleAssignment(
                    incident_id=incident.id,
                    role_type_id=role_type_id,
                    user_id=u.id,
                    assigned_by=by_user,
                )
            )
    from app.services.timeline import log_event

    names = [u.name for u in users]
    log_event(
        db,
        incident,
        entry_type="roles_changed",
        body=f"{role.label}: {', '.join(names) or 'unassigned'}.",
        created_by=by_user,
    )
    db.flush()
    db.expire(incident, ["role_assignments"])
    return users
