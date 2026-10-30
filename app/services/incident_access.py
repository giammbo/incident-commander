"""Shared incident visibility policy for browser views and notification recipients."""

from fastapi import HTTPException
from sqlalchemy import false, or_, select, true
from sqlalchemy.orm import Session

from app.models import Incident, IncidentRoleAssignment, Role, User, effective_role


def can_view_incident(user: User, incident: Incident) -> bool:
    if not user.is_active or user.id is None:
        return False
    return (
        not incident.is_private
        or effective_role(user) == Role.admin
        or incident.created_by == user.id
        or any(assignment.user_id == user.id for assignment in incident.role_assignments)
    )


def visible_incident_condition(user: User):
    """SQL equivalent of can_view_incident; an IC role grants no private bypass."""
    if not user.is_active or user.id is None:
        return false()
    if effective_role(user) == Role.admin:
        return true()
    return or_(
        Incident.is_private.is_(False),
        Incident.created_by == user.id,
        Incident.role_assignments.any(IncidentRoleAssignment.user_id == user.id),
    )


def visible_incident_ids(db: Session, user: User) -> list[int]:
    return list(db.scalars(select(Incident.id).where(visible_incident_condition(user))))


def get_visible_incident(db: Session, incident_id: int, user: User) -> Incident:
    incident = db.scalar(
        select(Incident).where(Incident.id == incident_id, visible_incident_condition(user))
    )
    if incident is None:
        raise HTTPException(status_code=404, detail="Not found")
    return incident
